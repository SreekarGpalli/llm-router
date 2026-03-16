"""
LLM Router — FastAPI application.

Proxy endpoints  (require virtual API key via x-api-key or Authorization: Bearer):
  POST /v1/messages
  GET  /v1/models
  POST /v1/messages/count_tokens

Management API  (require UI session cookie):
  GET  /api/config                  — public endpoint URL + domain
  GET  /api/stats
  GET  /api/key/current             — plaintext key (decrypted from stored enc copy)
  POST /api/key/regenerate
  GET  /api/providers
  GET  /api/providers/{id}
  POST /api/providers
  PUT  /api/providers/{id}
  DELETE /api/providers/{id}
  POST /api/providers/{id}/toggle
  POST /api/test-connection         — ping an upstream /models endpoint
  POST /api/fetch-models            — list models available on an upstream

UI:
  GET  /           — dashboard SPA (session required)
  GET  /login      — login page
  POST /login      — process login
  GET  /logout     — clear session

Public:
  GET  /health
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from auth import (
    clear_session,
    create_session,
    get_bearer_key,
    limiter,
    verify_session,
)
from crypto import decrypt, encrypt
from db import (
    delete_provider,
    generate_and_store_virtual_key,
    get_provider,
    get_stats,
    get_virtual_key_plaintext,
    init_db,
    list_aliases,
    list_all_aliases,
    list_providers,
    set_provider_aliases,
    store_virtual_key_hash,
    store_virtual_key_with_plaintext,
    toggle_provider,
    update_provider,
    create_provider,
    verify_virtual_key,
    virtual_key_exists,
)
from router import RouterError, get_route
from translator import (
    _new_msg_id,
    build_openai_request,
    openai_response_to_anthropic,
    stream_openai_to_anthropic,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
)
log = logging.getLogger("llm_router")

UI_PASSWORD = os.getenv("UI_PASSWORD", "")
BASE_DIR = Path(__file__).parent


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001
    init_db()

    # Install script may pass the full plaintext key via env so we can store
    # the encrypted copy (allowing the UI to display it later).
    initial_key = os.getenv("INITIAL_VIRTUAL_KEY", "").strip()
    initial_hash = os.getenv("INITIAL_VIRTUAL_KEY_HASH", "").strip()

    if initial_key and not virtual_key_exists():
        store_virtual_key_with_plaintext(initial_key)
        log.info("Virtual API key initialised from INITIAL_VIRTUAL_KEY env.")
    elif initial_hash and not virtual_key_exists():
        # Legacy: hash-only path (install script didn't provide plaintext)
        store_virtual_key_hash(initial_hash)
        log.info("Virtual API key hash initialised from INITIAL_VIRTUAL_KEY_HASH env.")
    elif not virtual_key_exists():
        key = generate_and_store_virtual_key()
        try:
            one_time = BASE_DIR / "data" / ".initial_key"
            one_time.write_text(key)
            log.info("New virtual API key written to %s", one_time)
        except OSError:
            log.info("Generated virtual API key (could not write one-time file).")
    yield


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.state.limiter = limiter


# FIX: override slowapi's default handler which returns plain text.
# Anthropic clients expect JSON {"type":"error","error":{...}} on 429.
async def _anthropic_rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return JSONResponse(
        status_code=429,
        content={
            "type": "error",
            "error": {
                "type": "rate_limit_error",
                "message": f"Rate limit exceeded: {exc.detail}. "
                           "Slow down and retry after a moment.",
            },
        },
    )


app.add_exception_handler(RateLimitExceeded, _anthropic_rate_limit_handler)


# ── Response helpers ──────────────────────────────────────────────────────────

def _err(error_type: str, message: str, status: int = 400) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"type": "error", "error": {"type": error_type, "message": message}},
    )


def _require_session(request: Request):
    if not verify_session(request):
        return RedirectResponse(url="/login", status_code=302)
    return None


def _require_session_api(request: Request):
    if not verify_session(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return None


def _require_api_key(request: Request):
    key = get_bearer_key(request)
    if not key or not verify_virtual_key(key):
        return _err("authentication_error", "Invalid or missing API key.", 401)
    return None


def _upstream_error(status: int, body: bytes, nickname: str) -> JSONResponse:
    try:
        data = json.loads(body)
        msg = (
            (data.get("error") or {}).get("message")
            or data.get("message")
            or str(data)
        )
    except Exception:
        msg = body.decode("utf-8", errors="replace")[:300]

    if status == 401:
        return _err("authentication_error", f"Upstream authentication failed: {msg}", 401)
    if status == 429:
        return _err("rate_limit_error", f"Upstream rate limit exceeded: {msg}", 429)
    if status == 404:
        return _err("not_found_error", f"Model not found on '{nickname}': {msg}", 404)
    if status == 400:
        return _err("invalid_request_error", f"Upstream bad request: {msg}", 400)
    return _err("api_error", f"Upstream error ({status}): {msg}", 500)


# ── Health (public, no auth) ──────────────────────────────────────────────────

@app.get("/health")
async def health():
    s = get_stats()
    return {"status": "ok", "providers": s["providers"], "aliases": s["aliases"]}


# ── /v1/models ────────────────────────────────────────────────────────────────

@app.get("/v1/models")
async def v1_models(request: Request):
    guard = _require_api_key(request)
    if guard:
        return guard
    aliases = list_all_aliases()
    return {
        "object": "list",
        "data": [
            {
                "id": a["anthropic_name"],
                "object": "model",
                "created": 1_700_000_000,
                "owned_by": a["provider_nickname"],
            }
            for a in aliases
        ],
    }


# ── /v1/messages/count_tokens ─────────────────────────────────────────────────

@app.post("/v1/messages/count_tokens")
async def v1_count_tokens(request: Request):
    guard = _require_api_key(request)
    if guard:
        return guard
    try:
        body = await request.json()
    except Exception:
        return _err("invalid_request_error", "Invalid JSON body.", 400)

    messages = body.get("messages") or []
    chars = sum(len(str(m.get("content") or "")) for m in messages)
    chars += len(str(body.get("system") or ""))
    return {"input_tokens": max(1, chars // 4)}


# ── /v1/messages ─────────────────────────────────────────────────────────────

@app.post("/v1/messages")
@limiter.limit("60/minute")
async def v1_messages(request: Request):
    guard = _require_api_key(request)
    if guard:
        return guard

    try:
        body = await request.json()
    except Exception:
        return _err("invalid_request_error", "Invalid JSON body.", 400)

    model = (body.get("model") or "").strip()
    if not model:
        return _err("invalid_request_error", "Missing required field: 'model'.", 400)

    try:
        provider, upstream_model = get_route(model)
    except RouterError as exc:
        return _err(exc.error_type, exc.message, exc.http_status)

    try:
        oai_req = build_openai_request(body, upstream_model)
    except Exception as exc:
        log.exception("Request translation error")
        return _err("invalid_request_error", f"Request translation failed: {exc}", 400)

    url = f"{provider['base_url']}/chat/completions"
    headers: dict = {"Content-Type": "application/json"}
    if provider["api_key"]:
        headers["Authorization"] = f"Bearer {provider['api_key']}"

    msg_id = _new_msg_id()

    if body.get("stream"):
        return StreamingResponse(
            _do_stream(url, headers, oai_req, model, msg_id, provider["nickname"]),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    return await _do_sync(url, headers, oai_req, model, provider["nickname"])


async def _do_sync(
    url: str,
    headers: dict,
    oai_req: dict,
    model_alias: str,
    nickname: str,
) -> JSONResponse:
    timeout = httpx.Timeout(connect=30.0, read=120.0, write=30.0, pool=5.0)
    try:
        async with httpx.AsyncClient(timeout=timeout, http2=True) as client:
            resp = await client.post(url, json=oai_req, headers=headers)
            if resp.status_code != 200:
                return _upstream_error(resp.status_code, resp.content, nickname)
            return JSONResponse(openai_response_to_anthropic(resp.json(), model_alias))
    except httpx.TimeoutException:
        return _err("api_error", "Upstream request timed out.", 504)
    except httpx.RequestError as exc:
        return _err("api_error", f"Network error reaching upstream: {exc}", 502)


async def _do_stream(
    url: str,
    headers: dict,
    oai_req: dict,
    model_alias: str,
    msg_id: str,
    nickname: str,
):
    timeout = httpx.Timeout(connect=30.0, read=None, write=30.0, pool=5.0)
    try:
        async with httpx.AsyncClient(timeout=timeout, http2=True) as client:
            async with client.stream("POST", url, json=oai_req, headers=headers) as resp:
                if resp.status_code != 200:
                    body = await resp.aread()
                    try:
                        data = json.loads(body)
                        msg = (data.get("error") or {}).get("message") or str(data)[:200]
                    except Exception:
                        msg = body.decode("utf-8", errors="replace")[:200]
                    err_payload = json.dumps({
                        "type": "error",
                        "error": {
                            "type": "api_error",
                            "message": f"Upstream error {resp.status_code} "
                                       f"from '{nickname}': {msg}",
                        },
                    })
                    yield f"event: error\ndata: {err_payload}\n\n".encode()
                    return

                async def _raw():
                    async for chunk in resp.aiter_bytes(chunk_size=2048):
                        yield chunk

                async for event_str in stream_openai_to_anthropic(
                    _raw(), model_alias, msg_id
                ):
                    yield event_str.encode()

    except httpx.TimeoutException:
        err = json.dumps({
            "type": "error",
            "error": {"type": "api_error", "message": "Upstream connection timed out."},
        })
        yield f"event: error\ndata: {err}\n\n".encode()
    except httpx.RequestError as exc:
        err = json.dumps({
            "type": "error",
            "error": {"type": "api_error", "message": f"Network error: {exc}"},
        })
        yield f"event: error\ndata: {err}\n\n".encode()


# ── Login / logout ────────────────────────────────────────────────────────────

_LOGIN_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>LLM Router · Sign in</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:system-ui,-apple-system,sans-serif;background:#0d1117;color:#c9d1d9;
     display:flex;align-items:center;justify-content:center;min-height:100vh}
.card{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:40px 36px;
      width:100%;max-width:360px}
.logo{font-size:1.5rem;font-weight:700;color:#fff;margin-bottom:6px}
.sub{color:#8b949e;font-size:.875rem;margin-bottom:28px}
label{display:block;font-size:.8rem;color:#8b949e;margin-bottom:5px;
      text-transform:uppercase;letter-spacing:.04em}
input[type=password]{width:100%;padding:9px 13px;background:#0d1117;border:1px solid #30363d;
  border-radius:7px;color:#c9d1d9;font-size:1rem;outline:none;transition:border .15s}
input[type=password]:focus{border-color:#58a6ff;box-shadow:0 0 0 3px rgba(88,166,255,.15)}
button{width:100%;margin-top:14px;padding:9px;background:#238636;border:none;
       border-radius:7px;color:#fff;font-size:.95rem;font-weight:600;
       cursor:pointer;transition:background .15s}
button:hover{background:#2ea043}
.err{margin-top:14px;padding:10px 13px;background:rgba(248,81,73,.12);
     border:1px solid rgba(248,81,73,.4);border-radius:7px;color:#f85149;font-size:.85rem}
</style>
</head>
<body>
<div class="card">
  <div class="logo">⇌ LLM Router</div>
  <div class="sub">Admin dashboard — sign in to continue</div>
  <form method="POST" action="/login">
    <label for="pw">Password</label>
    <input id="pw" name="password" type="password" autofocus autocomplete="current-password" required>
    {ERROR}
    <button type="submit">Sign in →</button>
  </form>
</div>
</body>
</html>
"""


@app.get("/login", response_class=HTMLResponse)
async def login_get(request: Request):
    if verify_session(request):
        return RedirectResponse(url="/", status_code=302)
    return HTMLResponse(_LOGIN_HTML.replace("{ERROR}", ""))


@app.post("/login")
async def login_post(request: Request):
    if not UI_PASSWORD:
        return HTMLResponse(
            "<h1 style='font-family:sans-serif;padding:2rem'>"
            "UI_PASSWORD is not set in .env</h1>",
            status_code=500,
        )
    form = await request.form()
    pw = str(form.get("password", ""))
    if pw != UI_PASSWORD:
        time.sleep(1)
        err = '<div class="err">Incorrect password. Please try again.</div>'
        return HTMLResponse(_LOGIN_HTML.replace("{ERROR}", err), status_code=401)

    resp = RedirectResponse(url="/", status_code=302)
    is_https = request.headers.get("x-forwarded-proto", "http") == "https"
    create_session(resp, is_https=is_https)
    return resp


@app.get("/logout")
async def logout(request: Request):  # noqa: ARG001
    resp = RedirectResponse(url="/login", status_code=302)
    clear_session(resp)
    return resp


# ── Dashboard SPA ─────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    guard = _require_session(request)
    if guard:
        return guard
    html_path = BASE_DIR / "static" / "index.html"
    if not html_path.exists():
        return HTMLResponse("<h1>static/index.html not found</h1>", status_code=500)
    return HTMLResponse(html_path.read_text())


# ── API: config (endpoint URL for UI display) ────────────────────────────────

@app.get("/api/config")
async def api_config(request: Request):
    guard = _require_session_api(request)
    if guard:
        return guard
    domain = os.getenv("CLOUDFLARE_DOMAIN", "").strip()
    if domain:
        endpoint = f"https://{domain}/v1"
    else:
        # Fallback: derive from the request's own URL (works via SSH tunnel too)
        endpoint = str(request.base_url).rstrip("/") + "/v1"
    return {"endpoint": endpoint, "domain": domain}


# ── API: stats ────────────────────────────────────────────────────────────────

@app.get("/api/stats")
async def api_stats(request: Request):
    guard = _require_session_api(request)
    if guard:
        return guard
    return get_stats()


# ── API: virtual key ──────────────────────────────────────────────────────────

@app.get("/api/key/current")
async def api_key_current(request: Request):
    """Return the current virtual API key in plaintext (decrypted from DB)."""
    guard = _require_session_api(request)
    if guard:
        return guard
    key = get_virtual_key_plaintext()
    return {"key": key}  # null if not available (hash-only legacy mode)


@app.post("/api/key/regenerate")
async def api_key_regenerate(request: Request):
    guard = _require_session_api(request)
    if guard:
        return guard
    new_key = generate_and_store_virtual_key()
    log.info("Virtual API key rotated.")
    return {"key": new_key}


# ── API: connectivity test ────────────────────────────────────────────────────

async def _probe_upstream(base_url: str, api_key: str) -> dict:
    """
    Probe an upstream provider by calling GET {base_url}/models.
    Returns {ok, message, latency_ms, models[]}.
    Falls back gracefully if /models is not supported (returns ok=True with empty list).
    """
    base_url = base_url.rstrip("/")
    headers: dict = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    timeout = httpx.Timeout(connect=10.0, read=15.0, write=5.0, pool=5.0)
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(f"{base_url}/models", headers=headers)
            latency_ms = int((time.monotonic() - t0) * 1000)

            if resp.status_code == 200:
                data = resp.json()
                raw = data.get("data") or data.get("models") or []
                model_ids = sorted(
                    filter(None, (
                        (m.get("id") or m.get("name") or "") if isinstance(m, dict) else str(m)
                        for m in raw
                    ))
                )
                return {
                    "ok": True,
                    "message": f"Connected — {len(model_ids)} model(s) available",
                    "latency_ms": latency_ms,
                    "models": model_ids,
                }
            elif resp.status_code == 401:
                return {
                    "ok": False,
                    "message": "Authentication failed — check your API key",
                    "latency_ms": latency_ms,
                    "models": [],
                }
            elif resp.status_code == 404:
                # Some providers don't expose /models but are otherwise valid
                return {
                    "ok": True,
                    "message": (
                        f"Connected (HTTP {resp.status_code} — "
                        "this provider doesn't expose /models)"
                    ),
                    "latency_ms": latency_ms,
                    "models": [],
                }
            else:
                snippet = resp.text[:120].strip()
                return {
                    "ok": False,
                    "message": f"HTTP {resp.status_code}: {snippet}",
                    "latency_ms": latency_ms,
                    "models": [],
                }

    except httpx.TimeoutException:
        return {
            "ok": False,
            "message": "Connection timed out (10 s) — check the URL",
            "latency_ms": int((time.monotonic() - t0) * 1000),
            "models": [],
        }
    except httpx.RequestError as exc:
        return {
            "ok": False,
            "message": f"Connection error: {exc}",
            "latency_ms": int((time.monotonic() - t0) * 1000),
            "models": [],
        }


@app.post("/api/test-connection")
async def api_test_connection(request: Request):
    """
    Test connectivity to an upstream provider.
    Accepts {base_url, api_key, provider_id?}.
    If api_key is empty and provider_id is given, uses the stored encrypted key.
    """
    guard = _require_session_api(request)
    if guard:
        return guard
    body = await request.json()
    base_url = (body.get("base_url") or "").strip()
    api_key = (body.get("api_key") or "").strip()
    provider_id = body.get("provider_id")

    if not api_key and provider_id:
        p = get_provider(int(provider_id))
        if p:
            api_key = decrypt(p["api_key_enc"])

    if not base_url:
        return JSONResponse({"ok": False, "message": "base_url is required"}, status_code=400)

    return await _probe_upstream(base_url, api_key)


@app.post("/api/fetch-models")
async def api_fetch_models(request: Request):
    """
    Fetch the list of available models from an upstream provider.
    Accepts {base_url, api_key, provider_id?}.
    If api_key is empty and provider_id is given, uses the stored encrypted key.
    """
    guard = _require_session_api(request)
    if guard:
        return guard
    body = await request.json()
    base_url = (body.get("base_url") or "").strip()
    api_key = (body.get("api_key") or "").strip()
    provider_id = body.get("provider_id")

    if not api_key and provider_id:
        p = get_provider(int(provider_id))
        if p:
            api_key = decrypt(p["api_key_enc"])

    if not base_url:
        return JSONResponse({"ok": False, "error": "base_url is required"}, status_code=400)

    result = await _probe_upstream(base_url, api_key)
    return {
        "ok": result["ok"],
        "models": result["models"],
        "message": result["message"],
        "error": "" if result["ok"] else result["message"],
    }


# ── API: providers ────────────────────────────────────────────────────────────

@app.get("/api/providers")
async def api_providers_list(request: Request):
    guard = _require_session_api(request)
    if guard:
        return guard
    return list_providers()


@app.get("/api/providers/{provider_id}")
async def api_provider_get(provider_id: int, request: Request):
    guard = _require_session_api(request)
    if guard:
        return guard
    p = get_provider(provider_id)
    if not p:
        raise HTTPException(status_code=404, detail="Provider not found")
    p.pop("api_key_enc", None)  # never send encrypted bytes to browser
    p["aliases"] = list_aliases(provider_id)
    return p


@app.post("/api/providers")
async def api_provider_create(request: Request):
    guard = _require_session_api(request)
    if guard:
        return guard
    body = await request.json()
    nickname = (body.get("nickname") or "").strip()
    base_url = (body.get("base_url") or "").strip()
    api_key = (body.get("api_key") or "").strip()
    aliases = body.get("aliases") or []

    if not nickname:
        return JSONResponse({"error": "nickname is required"}, status_code=400)
    if not base_url:
        return JSONResponse({"error": "base_url is required"}, status_code=400)

    api_key_enc = encrypt(api_key) if api_key else ""
    pid = create_provider(nickname, base_url, api_key_enc)
    try:
        set_provider_aliases(pid, aliases)
    except ValueError as exc:
        # Duplicate alias name — roll back the provider creation and report cleanly
        delete_provider(pid)
        return JSONResponse({"error": str(exc)}, status_code=400)

    log.info("Provider created: %s (id=%d)", nickname, pid)
    return {"id": pid, "message": "Provider created"}


@app.put("/api/providers/{provider_id}")
async def api_provider_update(provider_id: int, request: Request):
    guard = _require_session_api(request)
    if guard:
        return guard
    existing = get_provider(provider_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Provider not found")

    body = await request.json()
    nickname = (body.get("nickname") or existing["nickname"]).strip()
    base_url = (body.get("base_url") or existing["base_url"]).strip()
    enabled = int(body.get("enabled", existing["enabled"]))
    aliases = body.get("aliases") or []

    # Only re-encrypt if a new key was provided; otherwise keep the stored enc value.
    new_key = (body.get("api_key") or "").strip()
    api_key_enc = encrypt(new_key) if new_key else existing["api_key_enc"]

    update_provider(provider_id, nickname, base_url, api_key_enc, enabled)
    try:
        set_provider_aliases(provider_id, aliases)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    log.info("Provider updated: id=%d", provider_id)
    return {"message": "Provider updated"}


@app.delete("/api/providers/{provider_id}")
async def api_provider_delete(provider_id: int, request: Request):
    guard = _require_session_api(request)
    if guard:
        return guard
    if not get_provider(provider_id):
        raise HTTPException(status_code=404, detail="Provider not found")
    delete_provider(provider_id)
    log.info("Provider deleted: id=%d", provider_id)
    return {"message": "Provider deleted"}


@app.post("/api/providers/{provider_id}/toggle")
async def api_provider_toggle(provider_id: int, request: Request):
    guard = _require_session_api(request)
    if guard:
        return guard
    if not get_provider(provider_id):
        raise HTTPException(status_code=404, detail="Provider not found")
    body = await request.json()
    enabled = bool(body.get("enabled", True))
    toggle_provider(provider_id, enabled)
    return {"message": "Updated", "enabled": enabled}
