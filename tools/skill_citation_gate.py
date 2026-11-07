"""Citation gate for autonomous skill writes (SKMEM-P4-04).

The background review fork (the curator) may write a lesson or patch a skill only
when it cites at least one source experience id (a skmemory memory id or an outcome
ledger event id) that actually exists, and quotes at least ``MIN_QUOTE_CHARS``
characters verbatim from one cited source (whitespace-normalised). Anything else is
refused, ``rejected_uncited`` is incremented in the curator state (shown by
``hermes curator status``) and a log line is written. Accepted lessons are stored in
skmemory as ``kind=procedure, outcome=lesson`` with the verified ids.

Fails closed: if the sources cannot be read, nothing is written. Foreground,
user-directed writes are never gated. ``curator.citation_gate: false`` turns it off.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional

logger = logging.getLogger("tools.skill_manager_tool")

MIN_QUOTE_CHARS = 20
MAX_CITED_IDS = 10
GATED_ACTIONS = frozenset({"create", "edit", "patch", "write_file"})

Resolver = Callable[[str], Optional[str]]


def normalize(text: str) -> str:
    return " ".join(str(text).split())


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: str = ""
    cited_ids: tuple = ()


def check(source_ids: Any, quote: Any, resolver: Resolver) -> Verdict:
    """Pure decision. ``resolver(id)`` returns the source text, or None when the id does not exist."""
    if not isinstance(source_ids, (list, tuple)) or not source_ids:
        return Verdict(False, "no source_ids cited")
    ids = [i.strip() for i in source_ids[:MAX_CITED_IDS] if isinstance(i, str) and i.strip()]
    if not ids:
        return Verdict(False, "no source_ids cited")
    texts = {}
    for sid in dict.fromkeys(ids):
        text = resolver(sid)
        if text:
            texts[sid] = normalize(text)
    if not texts:
        return Verdict(False, "none of the cited ids exists")
    q = normalize(quote) if isinstance(quote, str) else ""
    if len(q) < MIN_QUOTE_CHARS:
        return Verdict(False, f"quote must be at least {MIN_QUOTE_CHARS} characters")
    if not any(q in t for t in texts.values()):
        return Verdict(False, "quote is not a verbatim substring of any cited source")
    return Verdict(True, cited_ids=tuple(texts))


def _enabled() -> bool:
    try:
        from agent import curator
        return bool(curator._load_config().get("citation_gate", True))
    except Exception:
        return True


def default_resolver() -> Resolver:
    """Read-only resolver over the active agent's skmemory store and outcome ledger."""
    from skmemory.agents import get_agent_paths
    from skmemory.ledger import Ledger
    from skmemory.store import MemoryStore

    store = MemoryStore()
    base = get_agent_paths()["base"] / "memory"
    events: dict[str, dict] = {}
    loaded = []

    def resolve(sid: str) -> Optional[str]:
        mem = store.recall(sid)
        if mem is not None:
            return f"{mem.title}\n{mem.content}"
        if not loaded:
            events.update({e["id"]: e for e in Ledger(base).events()})
            loaded.append(True)
        ev = events.get(sid)
        return json.dumps(ev, ensure_ascii=False, sort_keys=True) if ev else None

    return resolve


def default_writer() -> Callable[..., Optional[str]]:
    from skmemory.store import MemoryStore

    store = MemoryStore()

    def write(*, skill: str, action: str, text: str, quote: str, cited_ids: list) -> Optional[str]:
        mem = store.snapshot(
            f"Skill lesson: {skill}",
            f"{text[:2000]}\n\nQuote: {quote}",
            tags=["lesson", "hermes-curator", f"skill:{skill}"],
            source="hermes-curator",
            kind="procedure",
            outcome="lesson",
            metadata={"cited_ids": list(cited_ids), "quote": quote, "skill": skill, "action": action},
        )
        if (mem.kind, mem.outcome) != ("procedure", "lesson"):
            logger.warning("skill citation gate: lesson %s stored as kind=%s outcome=%s",
                           mem.id, mem.kind, mem.outcome)
        return mem.id

    return write


def _bump(key: str) -> int:
    try:
        from agent import curator
        state = curator.load_state()
        state[key] = int(state.get(key) or 0) + 1
        curator.save_state(state)
        return state[key]
    except Exception:
        logger.debug("curator counter %s not updated", key, exc_info=True)
        return -1


def review(action: str, name: str, source_ids: Any, quote: Any) -> tuple[Optional[dict], Optional[Verdict]]:
    """Gate one background-review write. Returns ``(refusal, verdict)``: a refusal dict when the
    write must not happen (counter bumped, line logged), else ``(None, verdict)``; ``(None, None)``
    when the gate does not apply."""
    from tools.skill_provenance import is_background_review

    if action not in GATED_ACTIONS or not is_background_review() or not _enabled():
        return None, None
    try:
        verdict = check(source_ids, quote, default_resolver())
    except Exception as exc:  # fail closed
        verdict = Verdict(False, f"cited sources could not be read ({type(exc).__name__})")
    if verdict.ok:
        return None, verdict
    total = _bump("rejected_uncited")
    logger.warning("skill citation gate: rejected_uncited %s %s (%s); total=%s",
                   action, name, verdict.reason, total)
    return ({"success": False, "rejected_uncited": True,
             "error": (f"rejected_uncited: refusing background {action} for skill '{name}': "
                       f"{verdict.reason}. Cite source_ids (existing memory ids or ledger event "
                       f"ids) and a verbatim quote (>= {MIN_QUOTE_CHARS} characters) from one of them.")},
            None)


def record_lesson(action: str, name: str, verdict: Verdict, quote: str, text: str) -> None:
    """Store the accepted lesson in skmemory and count it. Never breaks the write that succeeded."""
    try:
        default_writer()(skill=name, action=action, text=text, quote=normalize(quote),
                         cited_ids=list(verdict.cited_ids))
        _bump("accepted_lessons")
    except Exception:
        logger.warning("skill citation gate: lesson for %s accepted but not stored", name, exc_info=True)
