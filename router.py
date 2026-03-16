"""
Alias resolution: maps incoming Anthropic model names to upstream providers.

Resolution order:
  1. Exact match on anthropic_name — prefer enabled provider, then lowest id.
     (Same alias CAN exist on multiple providers; enabled wins.)
  2. Any alias with is_default=1 on an enabled provider.
  3. RouterError if nothing matched — message includes ALL configured alias names
     so the operator can immediately see what to set on the client side.

Pass-through rule: if upstream_name is empty the incoming model name is
forwarded to the upstream unchanged (useful for Ollama custom names).
"""
from __future__ import annotations

from typing import Tuple

from crypto import decrypt
from db import list_alias_names, resolve_alias


class RouterError(Exception):
    def __init__(self, error_type: str, message: str, http_status: int = 400):
        self.error_type = error_type
        self.message = message
        self.http_status = http_status
        super().__init__(message)


def get_route(model_name: str) -> Tuple[dict, str]:
    """
    Resolve model_name → (provider_info dict, upstream_model_name str).
    Raises RouterError on any failure.
    """
    alias = resolve_alias(model_name)

    if alias is None:
        names = list_alias_names()
        if names:
            sample = names[:6]
            shown = ", ".join(f"'{n}'" for n in sample)
            if len(names) > 6:
                shown += f" … +{len(names) - 6} more"
            hint = (
                f" Configured aliases: {shown}. "
                "Add '{model_name}' as an alias in the LLM Router UI, "
                "or mark one alias as the Default fallback."
            ).replace("{model_name}", model_name)
        else:
            hint = (
                " No aliases configured yet. "
                "Open the LLM Router UI and add a provider with at least one alias."
            )
        raise RouterError(
            "invalid_request_error",
            f"No alias found for model '{model_name}'.{hint}",
            400,
        )

    if not alias["enabled"]:
        raise RouterError(
            "invalid_request_error",
            f"Provider '{alias['nickname']}' is currently disabled. "
            "Re-enable it in the LLM Router UI to use this model.",
            400,
        )

    api_key = decrypt(alias["api_key_enc"])
    upstream_model = alias["upstream_name"] if alias["upstream_name"] else model_name

    return (
        {
            "nickname": alias["nickname"],
            "base_url": alias["base_url"].rstrip("/"),
            "api_key": api_key,
        },
        upstream_model,
    )
