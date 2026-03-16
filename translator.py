"""
Complete Anthropic ↔ OpenAI translation layer.

Handles:
  • Message array conversion (user/assistant/tool roles, mixed content)
  • Tool use / function calling (request + response + streaming)
  • Streaming SSE: full Anthropic event sequence with correct block indices
  • Vision / image blocks (base64 and URL)
  • System prompt extraction (string or content-block array)
  • Prompt cache_control stripping (silently ignored)
  • Thinking / extended-reasoning blocks (silently skipped for non-Anthropic upstreams)
  • Token usage normalisation (0 rather than omitting fields)
  • Model alias echo (alias name always echoed, never the upstream real name)
  • Consecutive same-role deduplication (merge user-user, insert placeholder for asst-asst)

FIXES vs. previous version:
  • content = msg.get("content") or "" was msg.get("content", "") which passes None
    through unchanged when the key exists with value None.
  • Added _normalize_roles() to prevent upstream rejections for consecutive
    same-role messages that can occur in agentic multi-tool loops.
  • Explicitly handle "thinking" content block type (skip cleanly, no fallthrough noise).
"""
from __future__ import annotations

import json
import uuid
from typing import Any, AsyncIterator, Optional

# ── Small helpers ─────────────────────────────────────────────────────────────

def _new_msg_id() -> str:
    return "msg_" + uuid.uuid4().hex[:24]


def _new_tool_id() -> str:
    return "toolu_" + uuid.uuid4().hex[:24]


_FINISH_REASON: dict[str, str] = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "content_filter": "stop_sequence",
}


# ── Content block → OpenAI parts ─────────────────────────────────────────────

def _blocks_to_oai_parts(blocks: list) -> list[dict]:
    """Convert a list of Anthropic content blocks to OpenAI content-part objects."""
    out: list[dict] = []
    for b in blocks:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text":
            out.append({"type": "text", "text": b.get("text") or ""})
        elif t == "image":
            src = b.get("source") or {}
            src_type = src.get("type")
            if src_type == "base64":
                mt = src.get("media_type", "image/jpeg")
                data = src.get("data", "")
                out.append({
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{mt};base64,{data}",
                        "detail": "auto",
                    },
                })
            elif src_type == "url":
                out.append({
                    "type": "image_url",
                    "image_url": {"url": src.get("url", ""), "detail": "auto"},
                })
        # Explicitly ignored: tool_use, tool_result, thinking, cache_control, document
    return out


def _extract_system(system: Any) -> str:
    """Normalise Anthropic system field (str | list[block] | None) → plain string."""
    if not system:
        return ""
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        # Concatenate all text blocks; ignore cache_control, non-text blocks
        return "\n".join(
            b.get("text") or ""
            for b in system
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return str(system)


# ── Consecutive same-role normalisation ──────────────────────────────────────

def _merge_content(existing: Any, incoming: Any) -> Any:
    """Merge two message content values into one."""
    def to_list(c: Any) -> list:
        if c is None or c == "":
            return []
        if isinstance(c, str):
            return [{"type": "text", "text": c}]
        if isinstance(c, list):
            return c
        return [{"type": "text", "text": str(c)}]

    a = to_list(existing)
    b = to_list(incoming)
    merged = a + b
    # If all parts are text, collapse to a plain string for cleanliness
    if all(p.get("type") == "text" for p in merged):
        return "\n\n".join(p.get("text", "") for p in merged)
    return merged


def _normalize_roles(messages: list[dict]) -> list[dict]:
    """
    Ensure no consecutive user-user or assistant-assistant messages.
    Some upstreams (e.g., older OpenAI-compatible APIs) reject them.

    - Consecutive user messages   → merge content into one user message.
    - Consecutive assistant msgs  → insert a minimal placeholder user turn between them.
    - tool role is transparent (not counted for consecutive-role purposes).
    """
    result: list[dict] = []
    for msg in messages:
        role = msg.get("role", "user")
        if role == "tool":
            result.append(msg)
            continue

        # Find last non-tool message
        last = next(
            (m for m in reversed(result) if m.get("role") != "tool"),
            None,
        )
        if last and last["role"] == "user" and role == "user":
            last["content"] = _merge_content(last["content"], msg["content"])
        elif last and last["role"] == "assistant" and role == "assistant":
            result.append({"role": "user", "content": "[continue]"})
            result.append(msg)
        else:
            result.append(msg)
    return result


# ── Anthropic messages → OpenAI messages ─────────────────────────────────────

def _anthropic_to_oai_messages(
    messages: list[dict],
    system_str: str,
) -> list[dict]:
    result: list[dict] = []

    if system_str:
        result.append({"role": "system", "content": system_str})

    for msg in messages:
        role = msg.get("role", "user")

        # FIX: msg.get("content", "") returns None if the key exists with value None
        content = msg.get("content")
        if content is None:
            content = ""

        # ── Simple string content ─────────────────────────────────────────────
        if isinstance(content, str):
            result.append({"role": role, "content": content})
            continue

        # ── Non-list, non-string (edge case) ─────────────────────────────────
        if not isinstance(content, list):
            result.append({"role": role, "content": str(content)})
            continue

        # ── Block array ───────────────────────────────────────────────────────
        if role == "user":
            tool_results = [
                b for b in content
                if isinstance(b, dict) and b.get("type") == "tool_result"
            ]
            other = [
                b for b in content
                if not (isinstance(b, dict) and b.get("type") == "tool_result")
            ]

            # Each tool_result → a separate role=tool message
            for tr in tool_results:
                tc = tr.get("content")
                if isinstance(tc, list):
                    tc = " ".join(
                        b.get("text") or ""
                        for b in tc
                        if isinstance(b, dict) and b.get("type") == "text"
                    )
                elif tc is None:
                    tc = ""
                result.append({
                    "role": "tool",
                    "tool_call_id": tr.get("tool_use_id") or "",
                    "content": str(tc),
                })

            # Remaining user content (text + images, skip thinking/cache blocks)
            if other:
                parts = _blocks_to_oai_parts(other)
                if parts:
                    if len(parts) == 1 and parts[0].get("type") == "text":
                        result.append({"role": "user", "content": parts[0]["text"]})
                    else:
                        result.append({"role": "user", "content": parts})

        elif role == "assistant":
            tool_uses = [
                b for b in content
                if isinstance(b, dict) and b.get("type") == "tool_use"
            ]
            text_blocks = [
                b for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            ]
            # "thinking" blocks are silently dropped for non-Anthropic upstreams

            if tool_uses:
                text = " ".join(
                    b.get("text") or "" for b in text_blocks
                ).strip() or None

                calls = []
                for tu in tool_uses:
                    args = tu.get("input") or {}
                    calls.append({
                        "id": tu.get("id") or f"call_{uuid.uuid4().hex[:8]}",
                        "type": "function",
                        "function": {
                            "name": tu.get("name") or "",
                            "arguments": (
                                json.dumps(args)
                                if isinstance(args, dict)
                                else str(args)
                            ),
                        },
                    })
                result.append({
                    "role": "assistant",
                    "content": text,
                    "tool_calls": calls,
                })
            else:
                parts = _blocks_to_oai_parts(content)
                text = " ".join(
                    p.get("text") or "" for p in parts if p.get("type") == "text"
                )
                result.append({"role": "assistant", "content": text})

    return _normalize_roles(result)


# ── Build OpenAI request ──────────────────────────────────────────────────────

def build_openai_request(anthropic_req: dict, upstream_model: str) -> dict:
    """Translate a complete Anthropic /v1/messages request body to OpenAI format."""
    system_str = _extract_system(anthropic_req.get("system"))
    messages = _anthropic_to_oai_messages(
        anthropic_req.get("messages") or [], system_str
    )

    oai: dict[str, Any] = {
        "model": upstream_model,
        "messages": messages,
    }

    for anth_field, oai_field in [
        ("max_tokens", "max_tokens"),
        ("temperature", "temperature"),
        ("top_p", "top_p"),
    ]:
        if anth_field in anthropic_req:
            oai[oai_field] = anthropic_req[anth_field]

    if "stop_sequences" in anthropic_req:
        oai["stop"] = anthropic_req["stop_sequences"]

    if anthropic_req.get("stream"):
        oai["stream"] = True
        oai["stream_options"] = {"include_usage": True}

    if anthropic_req.get("tools"):
        oai["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": t.get("name") or "",
                    "description": t.get("description") or "",
                    "parameters": t.get("input_schema") or {},
                },
            }
            for t in anthropic_req["tools"]
        ]
        tc = anthropic_req.get("tool_choice")
        if tc:
            tc_type = tc.get("type", "auto")
            if tc_type == "auto":
                oai["tool_choice"] = "auto"
            elif tc_type == "any":
                oai["tool_choice"] = "required"
            elif tc_type == "tool":
                oai["tool_choice"] = {
                    "type": "function",
                    "function": {"name": tc.get("name") or ""},
                }

    # Silently drop: thinking, betas, cache_control, metadata, top_k
    return oai


# ── OpenAI response → Anthropic (non-streaming) ───────────────────────────────

def openai_response_to_anthropic(oai: dict, model_alias: str) -> dict:
    """Convert a complete OpenAI chat completion response to Anthropic format."""
    choice = ((oai.get("choices") or [{}])[0])
    message = choice.get("message") or {}

    content: list[dict] = []

    text = message.get("content") or ""
    if text:
        content.append({"type": "text", "text": text})

    for tc in message.get("tool_calls") or []:
        func = tc.get("function") or {}
        args_str = func.get("arguments") or "{}"
        try:
            args = json.loads(args_str)
        except (json.JSONDecodeError, ValueError):
            args = {}
        content.append({
            "type": "tool_use",
            "id": tc.get("id") or _new_tool_id(),
            "name": func.get("name") or "",
            "input": args,
        })

    finish = choice.get("finish_reason") or "stop"
    stop_reason = _FINISH_REASON.get(finish, "end_turn")

    usage = oai.get("usage") or {}
    return {
        "id": oai.get("id") or _new_msg_id(),
        "type": "message",
        "role": "assistant",
        "content": content,
        "model": model_alias,   # ALWAYS echo the alias name, never the upstream real name
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens") or 0,
            "output_tokens": usage.get("completion_tokens") or 0,
        },
    }


# ── OpenAI SSE stream → Anthropic SSE stream ─────────────────────────────────

async def stream_openai_to_anthropic(
    raw: AsyncIterator[bytes],
    model_alias: str,
    msg_id: str,
) -> AsyncIterator[str]:
    """
    Consume an OpenAI SSE byte stream and yield Anthropic SSE event strings.

    Content block index allocation:
      index 0   = text block (opened on first text delta, closed when tool arrives)
      index N   = tool_use block per distinct OAI tool-call index

    Guarantees:
      • Exact Anthropic event sequence including ping after message_start
      • Model field always echoes the alias name
      • Both text AND tool_call blocks handled in same stream
      • input_json_delta emitted for tool argument chunks
      • Correct output_tokens count from upstream stream_options usage
    """

    def sse(event: str, data: dict) -> str:
        return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"

    # ── message_start ─────────────────────────────────────────────────────────
    yield sse("message_start", {
        "type": "message_start",
        "message": {
            "id": msg_id,
            "type": "message",
            "role": "assistant",
            "content": [],
            "model": model_alias,
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        },
    })

    yield sse("ping", {"type": "ping"})

    # ── State ─────────────────────────────────────────────────────────────────
    next_idx: int = 0
    text_idx: Optional[int] = None          # index of open text block, or None
    tool_blocks: dict[int, dict] = {}       # oai_tool_index → {block_idx, id, name}

    stop_reason = "end_turn"
    input_tokens = 0
    output_tokens = 0
    buf = ""

    # ── Consume upstream SSE ──────────────────────────────────────────────────
    async for raw_chunk in raw:
        if not raw_chunk:
            continue
        buf += raw_chunk.decode("utf-8", errors="replace")

        while True:
            nl = buf.find("\n")
            if nl == -1:
                break
            line = buf[:nl].strip()
            buf = buf[nl + 1:]

            if not line or line.startswith(":"):
                continue
            if not line.startswith("data: "):
                continue

            payload = line[6:]
            if payload == "[DONE]":
                continue

            try:
                chunk = json.loads(payload)
            except (json.JSONDecodeError, ValueError):
                continue

            # Capture usage when stream_options are present
            u = chunk.get("usage") or {}
            if u.get("prompt_tokens"):
                input_tokens = u["prompt_tokens"]
            if u.get("completion_tokens"):
                output_tokens = u["completion_tokens"]

            choices = chunk.get("choices") or []
            if not choices:
                continue

            choice = choices[0]
            delta = choice.get("delta") or {}
            finish = choice.get("finish_reason")

            if finish:
                stop_reason = _FINISH_REASON.get(finish, "end_turn")

            # ── Text delta ────────────────────────────────────────────────────
            text_chunk = delta.get("content") or ""
            if text_chunk:
                if text_idx is None:
                    text_idx = next_idx
                    next_idx += 1
                    yield sse("content_block_start", {
                        "type": "content_block_start",
                        "index": text_idx,
                        "content_block": {"type": "text", "text": ""},
                    })
                yield sse("content_block_delta", {
                    "type": "content_block_delta",
                    "index": text_idx,
                    "delta": {"type": "text_delta", "text": text_chunk},
                })

            # ── Tool call deltas ──────────────────────────────────────────────
            for tc_delta in delta.get("tool_calls") or []:
                oai_idx = tc_delta.get("index", 0)
                tc_id = tc_delta.get("id")
                func = tc_delta.get("function") or {}

                if oai_idx not in tool_blocks:
                    # Close the text block if still open
                    if text_idx is not None:
                        yield sse("content_block_stop", {
                            "type": "content_block_stop",
                            "index": text_idx,
                        })
                        text_idx = None

                    block_idx = next_idx
                    next_idx += 1
                    resolved_id = tc_id or _new_tool_id()
                    resolved_name = func.get("name") or ""
                    tool_blocks[oai_idx] = {
                        "block_idx": block_idx,
                        "id": resolved_id,
                        "name": resolved_name,
                    }
                    yield sse("content_block_start", {
                        "type": "content_block_start",
                        "index": block_idx,
                        "content_block": {
                            "type": "tool_use",
                            "id": resolved_id,
                            "name": resolved_name,
                            "input": {},
                        },
                    })
                else:
                    # Name may arrive in a later delta
                    if func.get("name"):
                        tool_blocks[oai_idx]["name"] = func["name"]

                args_chunk = func.get("arguments") or ""
                if args_chunk:
                    yield sse("content_block_delta", {
                        "type": "content_block_delta",
                        "index": tool_blocks[oai_idx]["block_idx"],
                        "delta": {
                            "type": "input_json_delta",
                            "partial_json": args_chunk,
                        },
                    })

    # ── Close any still-open blocks ───────────────────────────────────────────
    if text_idx is not None:
        yield sse("content_block_stop", {
            "type": "content_block_stop",
            "index": text_idx,
        })

    for oai_idx in sorted(tool_blocks.keys()):
        yield sse("content_block_stop", {
            "type": "content_block_stop",
            "index": tool_blocks[oai_idx]["block_idx"],
        })

    # ── Closing events ────────────────────────────────────────────────────────
    yield sse("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": stop_reason, "stop_sequence": None},
        "usage": {"output_tokens": output_tokens},
    })

    yield sse("message_stop", {"type": "message_stop"})
