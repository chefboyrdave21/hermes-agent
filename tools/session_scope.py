"""Default-deny session scope for the gateway (SKMEM-P0-19 / P0-22).

One question, answered from ContextVars the model cannot write: is this turn the owner's?
Owner = a verified owner DM (platform:user id listed in ``HERMES_MEMORY_OWNER`` AND a DM chat type),
a local operator surface (CLI/TUI/desktop process, no gateway), or a cron job stamped
``owner_created`` (see ``cron.jobs.create_job``). Everything else is non-owner, including:
groups, DMs from anyone else, and in gateway mode (``_HERMES_GATEWAY=1``) any turn with NO bound
session (goal, wake, webhook, background). Unset ``HERMES_MEMORY_OWNER`` fails closed.

Non-owner turns get: reader ``channel:<chat id>`` (or ``@public`` with no chat), no MEMORY.md /
USER.md, no pre_llm_call hook context, no external memory prefetch, no session_search, no /yolo,
and ONLY the tools in the allowlists below, plus the owner-speaker grants (``HERMES_OWNER_SPEAKER_TOOLS``,
default the Talk call tools) when the owner is the one speaking in a shared chat, for that chat only.
"""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Optional

OWNER_ENV = "HERMES_MEMORY_OWNER"  # speakers: "telegram:<user id>,nctalk:<nextcloud user id>,..."
OWNER_CHATS_ENV = "HERMES_MEMORY_OWNER_CHATS"  # private owner-only chats: "telegram:-100123,nctalk:<room token>,..."
API_KEY_ENV = "API_SERVER_KEY"
PUBLIC = "@public"
_DM_TYPES = frozenset({"dm", "direct", "private"})
logger = logging.getLogger(__name__)
# Slash commands a non-owner may run. Everything else (state-changing or memory-revealing, including
# /memory, /skills, /approvals, /yolo, plugin and exec quick commands) is owner-only: default-deny.
SLASH_ALLOW = frozenset({"help", "whoami"})
_logged = False
# Declared scope for work that runs OUTSIDE a bound session (finalize, expiry, shutdown cleanup): a 1-tuple
# ``(reader,)`` resolved from the session being finalized, never from whatever the thread happens to hold.
_override: ContextVar[Optional[tuple]] = ContextVar("HERMES_SCOPE_OVERRIDE", default=None)


@contextmanager
def declared_scope(source: Any = None):
    """Run a block under the scope of ``source`` (a stored session origin). ``None``/unknown => non-owner."""
    reader = PUBLIC
    if source is not None:
        plat = getattr(getattr(source, "platform", None), "value", None) or str(getattr(source, "platform", "") or "")
        try:
            reader = source_reader(plat, getattr(source, "chat_id", ""), str(getattr(source, "chat_type", "") or ""),
                                   getattr(source, "user_id", ""), getattr(source, "profile", "") or "")
        except Exception:
            reader = PUBLIC
    tok = _override.set((reader,))
    try:
        yield reader
    finally:
        _override.reset(tok)


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
        logger.info("session scope: %d owner identity(ies) via %s, %d owner chat(s) via %s", n, OWNER_ENV,
                    len(_owner_chats()), OWNER_CHATS_ENV)



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

# Owner-speaker grants: tools that run in a NON-owner chat when the speaker of THIS turn is a verified owner
# (an identity in HERMES_MEMORY_OWNER), acting only on this chat. The turn stays non-owner in every other way
# (reader, memory, hooks, prompt, every other tool). Default: the Talk call tools, so the owner can say
# "join call" in a group room with a guest; whether the agent may join that room is still the call stack's own
# allowlist (skcall SKCALL_ALLOWED_ROOMS). Comma list in HERMES_OWNER_SPEAKER_TOOLS; "off" or "" disables.
OWNER_SPEAKER_TOOLS_ENV = "HERMES_OWNER_SPEAKER_TOOLS"
OWNER_SPEAKER_TOOLS_DEFAULT = frozenset({"call_join", "call_leave", "call_status"})
# Args that would point a granted tool away from this chat. A grant never reaches another room.
_ELSEWHERE_ARGS = ("other_room",)


def _canon_platform(platform: str) -> str:
    p = (platform or "").strip().lower()
    return "nctalk" if p in ("nextcloud_talk", "nextcloud-talk") else p


def _canon_entry(entry: str, lower_id: bool) -> str:
    plat, _, ident = entry.strip().partition(":")
    ident = ident.strip()
    return f"{_canon_platform(plat)}:{ident.lower() if lower_id else ident}"


def _owners() -> frozenset[str]:
    return frozenset(_canon_entry(p, True) for p in os.environ.get(OWNER_ENV, "").split(",") if p.strip())


def _owner_chats() -> frozenset[str]:
    return frozenset(_canon_entry(p, False) for p in os.environ.get(OWNER_CHATS_ENV, "").split(",") if p.strip())


def _gateway_mode() -> bool:
    return os.environ.get("_HERMES_GATEWAY") == "1"


def source_reader(platform: str, chat_id: str, chat_type: str, user_id: str, profile: str = "") -> Optional[str]:
    """Reader for an explicit message source. Owner = the speaker is in HERMES_MEMORY_OWNER AND the chat is a
    DM or is listed in HERMES_MEMORY_OWNER_CHATS (a private chat where only the owner and the agent talk).
    Any other speaker in an owner chat is non-owner for that turn. api_server: owner iff API_SERVER_KEY is set
    (the adapter refuses to start without it and 401s every request that lacks it) and the request is not for a
    named profile (profile keys belong to other principals)."""
    platform, chat_id, chat_type, user_id = (str(v or "").strip() for v in (platform, chat_id, chat_type, user_id))
    plat, chat_type = _canon_platform(platform), chat_type.lower()
    if plat == "local":
        # v0.23 hosts the operator's own surfaces (CLI, TUI, desktop, ACP) as gateway sessions on the
        # LOCAL platform: reachable only over the gateway's local socket or the token-gated dashboard,
        # never from a messaging adapter. Same principal as the gateway-less local process above.
        return None
    if plat == "api_server":
        if os.environ.get(API_KEY_ENV, "").strip() and (profile or "").strip().lower() in ("", "default"):
            return None
    elif plat and user_id and f"{plat}:{user_id.lower()}" in _owners():
        if chat_type in _DM_TYPES or (chat_id and f"{plat}:{chat_id}" in _owner_chats()):
            return None
    return f"channel:{chat_id}" if chat_id else PUBLIC


def source_is_owner(source: Any) -> bool:
    log_owner_config()
    try:
        plat = getattr(getattr(source, "platform", None), "value", None) or str(getattr(source, "platform", "") or "")
        return source_reader(plat, getattr(source, "chat_id", ""), str(getattr(source, "chat_type", "") or ""),
                             getattr(source, "user_id", ""), getattr(source, "profile", "") or "") is None
    except Exception:
        return False


def session_reader() -> Optional[str]:
    """None = owner scope; else the reader string (``channel:<id>`` or ``@public``)."""
    from gateway.session_context import get_session_env

    log_owner_config()
    if (ov := _override.get()) is not None:
        return ov[0]
    g = lambda n: get_session_env(n, "").strip()
    platform = g("HERMES_SESSION_PLATFORM").lower()
    chat_id, chat_type, user_id = g("HERMES_SESSION_CHAT_ID"), g("HERMES_SESSION_CHAT_TYPE").lower(), g("HERMES_SESSION_USER_ID")

    if g("HERMES_CRON_SESSION") == "1":
        if cron_owner_job.get():
            return None
        dest = g("HERMES_CRON_AUTO_DELIVER_CHAT_ID")
        return f"channel:{dest}" if dest else PUBLIC
    if not _gateway_mode() and not platform and not chat_id:
        return None  # local CLI/TUI/desktop/API process with no messaging identity: the operator
    if _gateway_mode() and not platform and not chat_id:
        return PUBLIC  # gateway turn with nothing bound (goal, wake, webhook): NOT the operator
    return source_reader(platform, chat_id, chat_type, user_id, g("HERMES_SESSION_PROFILE"))


def is_owner_session() -> bool:
    try:
        return session_reader() is None
    except Exception:  # fail closed
        return False


def norm_server(name: str) -> str:
    return (name or "").strip().lower()


def owner_speaker_tools() -> frozenset[str]:
    raw = os.environ.get(OWNER_SPEAKER_TOOLS_ENV)
    if raw is None:
        return OWNER_SPEAKER_TOOLS_DEFAULT
    if raw.strip().lower() in ("", "off", "none", "0", "false"):
        return frozenset()
    return frozenset(t.strip() for t in raw.split(",") if t.strip())


def speaker_is_owner() -> bool:
    """True when the bound turn's SPEAKER is a verified owner identity, whatever the chat (DM, group, listed
    or not). Never true for cron, api_server, unbound gateway turns or a declared (override) scope."""
    from gateway.session_context import get_session_env

    try:
        if _override.get() is not None:
            return False
        g = lambda n: get_session_env(n, "").strip()
        if g("HERMES_CRON_SESSION") == "1":
            return False
        plat, user_id = _canon_platform(g("HERMES_SESSION_PLATFORM")), g("HERMES_SESSION_USER_ID")
        if not plat or plat == "api_server" or not user_id or not g("HERMES_SESSION_CHAT_ID"):
            return False
        return f"{plat}:{user_id.lower()}" in _owners()
    except Exception:  # fail closed
        return False


def owner_speaker_grant(tool_name: str, args: Optional[dict] = None) -> bool:
    """May ``tool_name`` run in this non-owner turn because the owner is the one asking? Only tools in
    ``owner_speaker_tools()``, only when the speaker is an owner, and only on this chat (no ``other_room``)."""
    if tool_name not in owner_speaker_tools() or not speaker_is_owner():
        return False
    args = args if isinstance(args, dict) else {}
    if any(str(args.get(k, "")).strip().lower() in ("1", "true", "yes", "on") for k in _ELSEWHERE_ARGS):
        return False
    return True


def native_refusal(tool_name: str, args: Optional[dict] = None) -> Optional[str]:
    """Refusal text for a NON-MCP tool in a non-owner session, else None."""
    if is_owner_session() or tool_name in NATIVE_ALLOW or owner_speaker_grant(tool_name, args):
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
