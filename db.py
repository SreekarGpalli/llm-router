"""SQLite database layer for LLM Router — WAL mode, no ORM."""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
from pathlib import Path
from typing import Optional

DB_PATH = os.getenv("DB_PATH", "/opt/llm-router/data/router.db")


# ── Connection ────────────────────────────────────────────────────────────────

def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    c.execute("PRAGMA synchronous=NORMAL")
    return c


# ── Schema ────────────────────────────────────────────────────────────────────

def init_db() -> None:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    db = _conn()
    with db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS config (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS providers (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                nickname    TEXT    NOT NULL,
                base_url    TEXT    NOT NULL,
                api_key_enc TEXT    NOT NULL DEFAULT '',
                enabled     INTEGER NOT NULL DEFAULT 1,
                created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS aliases (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                provider_id    INTEGER NOT NULL
                               REFERENCES providers(id) ON DELETE CASCADE,
                anthropic_name TEXT    NOT NULL,
                upstream_name  TEXT    NOT NULL DEFAULT '',
                is_default     INTEGER NOT NULL DEFAULT 0,
                created_at     TEXT    NOT NULL DEFAULT (datetime('now'))
            );

            -- Per-provider unique index: same alias name is allowed on
            -- different providers (useful for A/B testing two backends).
            -- Migration: drop the old global index if it exists from a prior install.
            DROP INDEX IF EXISTS ux_alias_name;
            CREATE UNIQUE INDEX IF NOT EXISTS ux_alias_provider_name
                ON aliases(provider_id, anthropic_name);
        """)
    db.close()


# ── Config helpers ────────────────────────────────────────────────────────────

def get_config(key: str) -> Optional[str]:
    db = _conn()
    row = db.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
    db.close()
    return row["value"] if row else None


def set_config(key: str, value: str) -> None:
    db = _conn()
    with db:
        db.execute("INSERT OR REPLACE INTO config(key,value) VALUES(?,?)", (key, value))
    db.close()


# ── Virtual API key ───────────────────────────────────────────────────────────

def virtual_key_exists() -> bool:
    return get_config("virtual_key_hash") is not None


def store_virtual_key_hash(key_hash: str) -> None:
    """Store hash only.  Called by install script which pre-generates the key."""
    set_config("virtual_key_hash", key_hash)


def generate_and_store_virtual_key() -> str:
    """
    Generate a new virtual API key and persist:
      • SHA-256 hash     → fast timing-safe comparison on every request
      • Fernet ciphertext → so the UI can display the current key at any time

    FIX: previous version only stored the hash, making it impossible to show
    the user their key after first generation without regenerating.
    """
    from crypto import encrypt  # local import avoids circular dependency

    key = "sk-ant-router-" + secrets.token_urlsafe(32)
    _persist_key(key)
    return key


def store_virtual_key_with_plaintext(key: str) -> None:
    """
    Persist a pre-generated key (used when install script passes
    INITIAL_VIRTUAL_KEY in env, not just the hash).
    Stores both hash and encrypted copy so UI can display it.
    """
    _persist_key(key)


def _persist_key(key: str) -> None:
    """Internal: write both hash and encrypted copy atomically."""
    from crypto import encrypt  # local import

    key_hash = hashlib.sha256(key.encode()).hexdigest()
    key_enc = encrypt(key)
    db = _conn()
    with db:
        db.execute(
            "INSERT OR REPLACE INTO config(key,value) VALUES(?,?)",
            ("virtual_key_hash", key_hash),
        )
        db.execute(
            "INSERT OR REPLACE INTO config(key,value) VALUES(?,?)",
            ("virtual_key_enc", key_enc),
        )
    db.close()


def get_virtual_key_plaintext() -> Optional[str]:
    """Return the current virtual API key in plaintext, or None if unavailable."""
    from crypto import decrypt  # local import

    enc = get_config("virtual_key_enc")
    if not enc:
        return None
    return decrypt(enc) or None


def verify_virtual_key(provided: str) -> bool:
    stored = get_config("virtual_key_hash")
    if not stored:
        return False
    provided_hash = hashlib.sha256(provided.encode()).hexdigest()
    return hmac.compare_digest(stored, provided_hash)


# ── Providers ─────────────────────────────────────────────────────────────────

def list_providers() -> list[dict]:
    db = _conn()
    rows = db.execute("""
        SELECT p.id, p.nickname, p.base_url, p.enabled,
               COUNT(a.id) AS alias_count
        FROM   providers p
        LEFT JOIN aliases a ON a.provider_id = p.id
        GROUP  BY p.id
        ORDER  BY p.id
    """).fetchall()
    db.close()
    return [dict(r) for r in rows]


def get_provider(provider_id: int) -> Optional[dict]:
    db = _conn()
    row = db.execute("SELECT * FROM providers WHERE id=?", (provider_id,)).fetchone()
    db.close()
    return dict(row) if row else None


def create_provider(nickname: str, base_url: str, api_key_enc: str) -> int:
    db = _conn()
    with db:
        cur = db.execute(
            "INSERT INTO providers(nickname,base_url,api_key_enc) VALUES(?,?,?)",
            (nickname, base_url, api_key_enc),
        )
    pid = cur.lastrowid
    db.close()
    return pid


def update_provider(
    provider_id: int,
    nickname: str,
    base_url: str,
    api_key_enc: str,
    enabled: int,
) -> None:
    db = _conn()
    with db:
        db.execute(
            "UPDATE providers SET nickname=?,base_url=?,api_key_enc=?,enabled=? WHERE id=?",
            (nickname, base_url, api_key_enc, enabled, provider_id),
        )
    db.close()


def toggle_provider(provider_id: int, enabled: bool) -> None:
    db = _conn()
    with db:
        db.execute(
            "UPDATE providers SET enabled=? WHERE id=?",
            (1 if enabled else 0, provider_id),
        )
    db.close()


def delete_provider(provider_id: int) -> None:
    db = _conn()
    with db:
        db.execute("DELETE FROM providers WHERE id=?", (provider_id,))
    db.close()


# ── Aliases ───────────────────────────────────────────────────────────────────

def list_aliases(provider_id: int) -> list[dict]:
    db = _conn()
    rows = db.execute(
        "SELECT * FROM aliases WHERE provider_id=? ORDER BY id",
        (provider_id,),
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


def list_all_aliases() -> list[dict]:
    db = _conn()
    rows = db.execute("""
        SELECT a.*, p.nickname AS provider_nickname
        FROM   aliases a
        JOIN   providers p ON p.id = a.provider_id
        ORDER  BY a.id
    """).fetchall()
    db.close()
    return [dict(r) for r in rows]


def set_provider_aliases(provider_id: int, aliases: list[dict]) -> None:
    """
    Atomically replace all aliases for a provider.

    Raises ValueError (NOT a generic exception) when an alias name is already
    owned by a different provider, so callers can surface a clean 400 error.
    The UNIQUE INDEX on aliases(anthropic_name) enforces one alias → one provider.
    """
    db = _conn()
    try:
        with db:
            db.execute("DELETE FROM aliases WHERE provider_id=?", (provider_id,))
            for a in aliases:
                anthropic_name = (a.get("anthropic_name") or "").strip()
                if not anthropic_name:
                    continue
                try:
                    db.execute(
                        "INSERT INTO aliases"
                        "(provider_id,anthropic_name,upstream_name,is_default)"
                        " VALUES(?,?,?,?)",
                        (
                            provider_id,
                            anthropic_name,
                            (a.get("upstream_name") or "").strip(),
                            1 if a.get("is_default") else 0,
                        ),
                    )
                except sqlite3.IntegrityError:
                    raise ValueError(
                        f"Alias name '{anthropic_name}' is already defined for this provider. "
                        "Remove the duplicate row or give it a different name."
                    )
    finally:
        db.close()


# ── Alias resolution ──────────────────────────────────────────────────────────

def resolve_alias(model_name: str) -> Optional[dict]:
    """
    Look up model_name in alias table.

    Priority:
      1. Exact match on anthropic_name — prefers ENABLED provider, then lowest id.
         (With per-provider uniqueness, the same alias can exist on multiple providers;
          we always pick the enabled one first, then the oldest-created as tiebreaker.)
      2. Default alias on an ENABLED provider (fallback only when no exact match).

    Returns a full row dict including provider columns, or None.
    """
    db = _conn()

    # Prefer enabled providers; among ties, lowest provider id (first added) wins.
    row = db.execute("""
        SELECT a.*, p.nickname, p.base_url, p.api_key_enc, p.enabled
        FROM   aliases a
        JOIN   providers p ON p.id = a.provider_id
        WHERE  a.anthropic_name = ?
        ORDER  BY p.enabled DESC, p.id ASC
        LIMIT  1
    """, (model_name,)).fetchone()

    if row:
        db.close()
        return dict(row)

    # No exact match — fall back to any default on an ENABLED provider.
    row = db.execute("""
        SELECT a.*, p.nickname, p.base_url, p.api_key_enc, p.enabled
        FROM   aliases a
        JOIN   providers p ON p.id = a.provider_id
        WHERE  a.is_default = 1 AND p.enabled = 1
        ORDER  BY p.id ASC
        LIMIT  1
    """).fetchone()

    db.close()
    return dict(row) if row else None


# ── Stats ─────────────────────────────────────────────────────────────────────

def list_alias_names() -> list[str]:
    """Return all configured anthropic_name values (for error messages)."""
    db = _conn()
    rows = db.execute(
        "SELECT DISTINCT anthropic_name FROM aliases ORDER BY anthropic_name"
    ).fetchall()
    db.close()
    return [r["anthropic_name"] for r in rows]


def get_stats() -> dict:
    db = _conn()
    providers = db.execute(
        "SELECT COUNT(*) AS n FROM providers WHERE enabled=1"
    ).fetchone()["n"]
    aliases = db.execute("SELECT COUNT(*) AS n FROM aliases").fetchone()["n"]
    db.close()
    return {"providers": providers, "aliases": aliases}
