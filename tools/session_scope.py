"""Default-deny session scope for the gateway (SKMEM-P0-19 / P0-22).

One question, answered from ContextVars the model cannot write: is this turn the owner's?
Owner = a verified owner DM (platform:user id listed in ``HERMES_MEMORY_OWNER`` AND a DM chat type),
a local operator surface (CLI/TUI/desktop process, no gateway), or a cron job stamped
``owner_created`` (see ``cron.jobs.create_job``). Everything else is non-owner, including:
groups, DMs from anyone else, and in gateway mode (``_HERMES_GATEWAY=1``) any turn with NO bound
session (goal, wake, webhook, background). Unset ``HERMES_MEMORY_OWNER`` fails closed.

Non-owner turns get: reader ``channel:<chat id>`` (or ``@public`` with no chat), no MEMORY.md /
USER.md, no pre_llm_call hook context, no external memory prefetch, no session_search, no /yolo,
and ONLY the tools in the allowlists below.
"""
from __future__ import annotations

import logging
import os
from contextvars import ContextVar
from typing import Any, Optional

OWNER_ENV = "HERMES_MEMORY_OWNER"  # "telegram:<user id>,..."
PUBLIC = "@public"
logger = logging.getLogger(__name__)
# Slash commands a non-owner may run. Everything else (state-changing or memory-revealing, including
# /memory, /skills, /approvals, /yolo, plugin and exec quick commands) is owner-only: default-deny.
SLASH_ALLOW = frozenset({"help", "whoami"})
_logged = False


def log_owner_config() -> None:
    """One WARNING when no owner is configured (the owner DM then silently reads as non-owner), else one INFO.
    Logs counts only, never ids. Called at gateway start and lazily on first resolution."""
    global _logged
    if _logged:
        return
    _logged = True
    n = len(_owners())
    if n == 0:
        logger.warning("%s is unset: NO session is the owner. Every chat, including the owner DM, runs "
                       "default-deny (reader channel:<id> / @public, no MEMORY.md, no hooks, no slash admin).", OWNER_ENV)
    else:
        logger.info("session scope: %d owner identity(ies) configured via %s", n, OWNER_ENV)
_DM_TYPES = frozenset({"dm", "direct", "private"})

# Set by cron's run scope: True only for jobs stamped owner_created.
cron_owner_job: ContextVar[bool] = ContextVar("HERMES_CRON_OWNER_JOB", default=False)

# MCP: normalized server key -> tools that run in non-owner sessions. Only skmemory READ tools whose
# results skmemory filters by the per-call reader (skmemory #105, SKMEM-P0-16), and only when the
# server's schema advertises `reader` (see mcp_reader_ok). memory_store and every write, health,
# graph, export, forget, audit and synthesis tool are NOT listed.
MCP_ALLOW: dict[str, frozenset[str]] = {
    "skmemory": frozenset({"memory_search", "memory_recall", "memory_list", "memory_context"}),
}
# Native tools: no filesystem, shell, code, browser, messaging, cron, delegation, skills, vision (local
# paths), memory or session tools. Stateless internet reads and output-only helpers.
NATIVE_ALLOW = frozenset({
    "web_search", "web_extract", "clarify", "todo_list", "text_to_speech", "image_generate",
})


def _owners() -> frozenset[str]:
    return frozenset(p.strip().lower() for p in os.environ.get(OWNER_ENV, "").split(",") if p.strip())


def _gateway_mode() -> bool:
    return os.environ.get("_HERMES_GATEWAY") == "1"


def source_reader(platform: str, chat_id: str, chat_type: str, user_id: str) -> Optional[str]:
    """Reader for an explicit message source (used before session vars are bound, e.g. slash dispatch)."""
    platform, chat_id, chat_type, user_id = (str(v or "").strip() for v in (platform, chat_id, chat_type, user_id))
    platform, chat_type = platform.lower(), chat_type.lower()
    if platform and chat_type in _DM_TYPES and user_id and f"{platform}:{user_id}" in _owners():
        return None
    return f"channel:{chat_id}" if chat_id else PUBLIC


def source_is_owner(source: Any) -> bool:
    log_owner_config()
    try:
        plat = getattr(getattr(source, "platform", None), "value", None) or str(getattr(source, "platform", "") or "")
        return source_reader(plat, getattr(source, "chat_id", ""), str(getattr(source, "chat_type", "") or ""),
                             getattr(source, "user_id", "")) is None
    except Exception:
        return False


def session_reader() -> Optional[str]:
    """None = owner scope; else the reader string (``channel:<id>`` or ``@public``)."""
    from gateway.session_context import get_session_env

    log_owner_config()
    g = lambda n: get_session_env(n, "").strip()
    platform = g("HERMES_SESSION_PLATFORM").lower()
    chat_id, chat_type, user_id = g("HERMES_SESSION_CHAT_ID"), g("HERMES_SESSION_CHAT_TYPE").lower(), g("HERMES_SESSION_USER_ID")
    owners = _owners()

    if g("HERMES_CRON_SESSION") == "1":
        if cron_owner_job.get():
            return None
        dest = g("HERMES_CRON_AUTO_DELIVER_CHAT_ID")
        return f"channel:{dest}" if dest else PUBLIC
    if not _gateway_mode() and not platform and not chat_id:
        return None  # local CLI/TUI/desktop/API process with no messaging identity: the operator
    if _gateway_mode() and not platform and not chat_id:
        return PUBLIC  # gateway turn with nothing bound (goal, wake, webhook): NOT the operator
    if platform and chat_type in _DM_TYPES and user_id and f"{platform}:{user_id}" in owners:
        return None
    return f"channel:{chat_id}" if chat_id else PUBLIC


def is_owner_session() -> bool:
    try:
        return session_reader() is None
    except Exception:  # fail closed
        return False


def norm_server(name: str) -> str:
    return (name or "").strip().lower()


def native_refusal(tool_name: str) -> Optional[str]:
    """Refusal text for a NON-MCP tool in a non-owner session, else None."""
    if is_owner_session() or tool_name in NATIVE_ALLOW:
        return None
    return f"{tool_name} is not available in this chat"


def mcp_refusal(server_name: str, tool_name: str, tools: Optional[list],
                check_schema: bool = True) -> tuple[Optional[str], Optional[str]]:
    """``(refusal, reader)`` for an MCP call. ``tools`` = the server's discovered tool objects, used for the
    capability check: a skmemory without ``reader`` in the tool schema would ignore the declaration, so the
    call is refused rather than served at owner scope."""
    try:
        reader = session_reader()
    except Exception:
        reader = PUBLIC
    if reader is None:
        return None, None
    if tool_name not in MCP_ALLOW.get(norm_server(server_name), ()):
        return f"{tool_name} is not available in this chat", None
    if check_schema and not mcp_reader_ok(tools, tool_name):
        return (f"{tool_name} is refused in this chat: the memory server does not advertise a reader "
                f"(upgrade skmemory)"), None
    return None, reader


def mcp_reader_ok(tools: Optional[list], tool_name: str) -> bool:
    for t in tools or ():
        name = t.get("name") if isinstance(t, dict) else getattr(t, "name", None)
        if name != tool_name:
            continue
        schema = t.get("inputSchema") if isinstance(t, dict) else (
            getattr(t, "inputSchema", None) or getattr(t, "input_schema", None))
        props = (schema or {}).get("properties") if isinstance(schema, dict) else None
        return isinstance(props, dict) and "reader" in props
    return False  # unknown tool: fail closed
