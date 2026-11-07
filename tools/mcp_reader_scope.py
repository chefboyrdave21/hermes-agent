"""Gateway-owned reader scope for the skmemory / skcapstone MCP servers (SKMEM-P0-19).

skmemory MCP is one stdio process serving every chat, so its ``SKMEMORY_READER`` env ceiling cannot
differ per chat. The gateway therefore declares the reader PER CALL, at the tool-dispatch layer, from
session context the model cannot write. Policy:

* verified owner DM (platform + user id in ``HERMES_MEMORY_OWNER``, DM chat type)  -> owner (no reader)
* local operator surface (CLI/TUI/desktop, or no gateway session bound at all)     -> owner (no reader)
* any other chat (group, or a DM from anyone else)                                 -> ``channel:<chat id>``
* a gateway session with no usable chat identity                                   -> ``@public``

A model-supplied ``reader`` is always overwritten for non-owner sessions. skmemory narrows the
per-call reader against its own ``SKMEMORY_READER`` ceiling, so this can only ever narrow.
Unset ``HERMES_MEMORY_OWNER`` fails closed: nobody is an owner.
"""
from __future__ import annotations

import os
from typing import Any, Optional

OWNER_ENV = "HERMES_MEMORY_OWNER"          # "telegram:<user id>,..." (comma separated)
SERVERS_ENV = "HERMES_MEMORY_READER_SERVERS"
DEFAULT_READER_SERVERS = frozenset({"skmemory"})
# skcapstone has no reader support: in non-owner sessions its memory-reading tools are refused.
DEFAULT_DENY_SERVERS = frozenset({"skcapstone"})
DENY_PREFIXES = ("memory_", "ritual", "journal_", "germination", "agent_context", "anchor_",
                 "session_capture", "trust_febs", "emotion_", "soul_", "brain_first")
_LOCAL_SOURCES = frozenset({"cli", "tui", "desktop", "local"})
_DM_TYPES = frozenset({"dm", "direct", "private"})
PUBLIC = "@public"


def _owners() -> frozenset[str]:
    raw = os.environ.get(OWNER_ENV, "")
    return frozenset(p.strip().lower() for p in raw.split(",") if p.strip())


def _env_set(name: str, default: frozenset[str]) -> frozenset[str]:
    raw = os.environ.get(name)
    return default if raw is None else frozenset(p.strip() for p in raw.split(",") if p.strip())


def session_reader() -> Optional[str]:
    """None = owner scope (leave arguments alone); else the reader string to force."""
    from gateway.session_context import get_session_env, session_context_engaged

    platform = get_session_env("HERMES_SESSION_PLATFORM", "").strip().lower()
    source = get_session_env("HERMES_SESSION_SOURCE", "").strip().lower()
    chat_id = get_session_env("HERMES_SESSION_CHAT_ID", "").strip()
    chat_type = get_session_env("HERMES_SESSION_CHAT_TYPE", "").strip().lower()
    user_id = get_session_env("HERMES_SESSION_USER_ID", "").strip()
    owners = _owners()

    if not session_context_engaged() or (source in _LOCAL_SOURCES and not platform):
        return None  # separate local process (CLI/TUI), no gateway session: the operator
    if get_session_env("HERMES_CRON_SESSION", "") == "1":
        plat = get_session_env("HERMES_CRON_AUTO_DELIVER_PLATFORM", "").strip().lower()
        dest = get_session_env("HERMES_CRON_AUTO_DELIVER_CHAT_ID", "").strip()
        if not dest:
            return None  # cron that delivers nowhere: local
        return None if f"{plat}:{dest}" in owners else f"channel:{dest}"
    if platform and chat_type in _DM_TYPES and user_id and f"{platform}:{user_id}" in owners:
        return None
    if chat_id:
        return f"channel:{chat_id}"
    return PUBLIC


def scope_tool_args(server_name: str, tool_name: str, args: Any) -> tuple[Any, Optional[str]]:
    """Return ``(args, refusal)``. ``refusal`` is an error message when the call must not run."""
    readers = _env_set(SERVERS_ENV, DEFAULT_READER_SERVERS)
    denied = server_name in DEFAULT_DENY_SERVERS
    if server_name not in readers and not denied:
        return args, None
    try:
        reader = session_reader()
    except Exception:  # fail closed: an unclassifiable session is never the owner
        reader = PUBLIC
    if reader is None:
        return args, None
    if server_name in readers:
        scoped = dict(args) if isinstance(args, dict) else {}
        scoped["reader"] = reader  # unconditional: the model's value is discarded
        return scoped, None
    if tool_name.startswith(DENY_PREFIXES):
        return args, (f"{server_name}.{tool_name} reads owner memory and is not available "
                      f"in this chat (reader {reader})")
    return args, None
