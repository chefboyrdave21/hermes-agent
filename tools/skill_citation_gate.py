"""Citation gate for autonomous skill writes (SKMEM-P4-04).

The background review fork (the curator) may write a lesson or patch a skill only
when it cites at least one source experience id (a skmemory memory id or an outcome
ledger event id) that actually exists, and quotes at least ``MIN_QUOTE_CHARS``
characters verbatim from one cited source (whitespace-normalised). Anything else is
refused, ``rejected_uncited`` is incremented in the curator state (shown by
``hermes curator status``) and a log line is written. Accepted lessons are stored in
skmemory as ``kind=procedure, outcome=lesson`` with the verified ids.

The quote must come from prose (a memory or a skill's text), never a ledger event, which
is extra provenance only. Fails closed: if the sources cannot be read, nothing is written. Foreground,
user-directed writes are never gated. ``curator.citation_gate: false`` turns it off.
"""

from __future__ import annotations

import contextvars
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Optional

logger = logging.getLogger("tools.skill_manager_tool")

MIN_QUOTE_CHARS = 20
MAX_CITED_IDS = 10
GATED_ACTIONS = frozenset({"create", "edit", "patch", "write_file"})

Resolver = Callable[[str], Any]  # id -> prose str | EXISTS_ONLY | None


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
    ids = [
        i.strip()
        for i in source_ids[:MAX_CITED_IDS]
        if isinstance(i, str) and i.strip()
    ]
    if not ids:
        return Verdict(False, "no source_ids cited")
    texts = {}
    for sid in dict.fromkeys(ids):
        text = resolver(sid)
        if text is EXISTS_ONLY:
            texts[sid] = ""  # exists: provenance only, no prose to quote
        elif text:
            texts[sid] = normalize(text)
    if not texts:
        return Verdict(False, "none of the cited ids exists")
    q = normalize(quote) if isinstance(quote, str) else ""
    if len(q) < MIN_QUOTE_CHARS:
        return Verdict(False, f"quote must be at least {MIN_QUOTE_CHARS} characters")
    if not any(t and q in t for t in texts.values()):
        return Verdict(False, "quote is not a verbatim substring of any cited source")
    return Verdict(True, cited_ids=tuple(texts))


def _enabled() -> bool:
    try:
        from agent import curator

        return bool(curator._load_config().get("citation_gate", True))
    except Exception:
        return True


class _Exists:
    """Marker for a source that exists but has no prose (an outcome ledger event). It counts as
    provenance only: a quote can never be satisfied by it."""


EXISTS_ONLY = _Exists()

SKILL_PREFIX = "skill:"


_FRONTMATTER = re.compile(r"\A\s*---\r?\n.*?\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)


def _body(text: str) -> str:
    """SKILL.md without its YAML frontmatter: only the body is quotable."""
    return _FRONTMATTER.sub("", text, count=1)


# Skills written so far in the current batch (None outside a batch): a batch may not cite a
# skill it has itself just written, or it could launder its own output into a "source".
_batch_written: contextvars.ContextVar[Optional[set]] = contextvars.ContextVar(
    "skill_citation_batch_written", default=None
)


def begin_batch() -> contextvars.Token:
    return _batch_written.set(set())


def end_batch(token: contextvars.Token) -> None:
    _batch_written.reset(token)


def note_written(name: str) -> None:
    if (written := _batch_written.get()) is not None:
        written.add(_skill_name(name))


def _skill_name(name: str) -> str:
    return str(name).strip().rsplit("/", 1)[-1]


def skill_source(name: str) -> Optional[str]:
    """Current SKILL.md text of an existing skill (``skill:<name>`` citations), else None.
    Consolidation and /refine merge existing skills, so an existing skill is a valid source."""
    from tools import skill_manager_tool as smt

    found = smt._find_skill(name.strip())
    if not found:
        return None
    try:
        return _body(
            (found["path"] / "SKILL.md").read_text(
                encoding="utf-8-sig", errors="replace"
            )
        )
    except OSError:
        return None


def memory_resolver() -> Resolver:
    """Read-only resolver over the active agent's skmemory store and outcome ledger. Memory ids
    give prose (title + content); ledger event ids give ``EXISTS_ONLY``, never JSON text."""
    from skmemory.agents import get_agent_paths
    from skmemory.ledger import Ledger
    from skmemory.store import MemoryStore

    store = MemoryStore()
    base = get_agent_paths()["base"] / "memory"
    ids: set = set()
    loaded = []

    def resolve(sid: str):
        mem = store.recall(sid)
        if mem is not None:
            return f"{mem.title}\n{mem.content}"
        if not loaded:
            ids.update(e["id"] for e in Ledger(base).events())
            loaded.append(True)
        return EXISTS_ONLY if sid in ids else None

    return resolve


def default_resolver() -> Resolver:
    """``skill:<name>`` resolves locally; anything else goes to skmemory, built on first use so a
    consolidation citing skills never needs it."""
    lazy: list = []

    def resolve(sid: str):
        if sid.startswith(SKILL_PREFIX):
            return skill_source(sid[len(SKILL_PREFIX) :])
        if not lazy:
            lazy.append(memory_resolver())
        return lazy[0](sid)

    return resolve


def default_writer() -> Callable[..., Optional[str]]:
    from skmemory.store import MemoryStore

    store = MemoryStore()

    def write(
        *, skill: str, action: str, text: str, quote: str, cited_ids: list
    ) -> Optional[str]:
        mem = store.snapshot(
            f"Skill lesson: {skill}",
            f"{text[:2000]}\n\nQuote: {quote}",
            tags=["lesson", "hermes-curator", f"skill:{skill}"],
            source="hermes-curator",
            kind="procedure",
            outcome="lesson",
            metadata={
                "cited_ids": list(cited_ids),
                "quote": quote,
                "skill": skill,
                "action": action,
            },
        )
        if (mem.kind, mem.outcome) != ("procedure", "lesson"):
            logger.warning(
                "skill citation gate: lesson %s stored as kind=%s outcome=%s",
                mem.id,
                mem.kind,
                mem.outcome,
            )
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


def review(
    action: str, name: str, source_ids: Any, quote: Any
) -> tuple[Optional[dict], Optional[Verdict]]:
    """Gate one background-review write. Returns ``(refusal, verdict)``: a refusal dict when the
    write must not happen (counter bumped, line logged), else ``(None, verdict)``; ``(None, None)``
    when the gate does not apply."""
    from tools.skill_provenance import is_background_review

    if action not in GATED_ACTIONS or not is_background_review() or not _enabled():
        return None, None
    try:
        base = default_resolver()
        barred = {_skill_name(name), *(_batch_written.get() or ())}

        def resolve(sid: str):
            # never the write target itself, never a skill this batch already wrote
            if (
                sid.startswith(SKILL_PREFIX)
                and _skill_name(sid[len(SKILL_PREFIX) :]) in barred
            ):
                return None
            return base(sid)

        verdict = check(source_ids, quote, resolve)
    except Exception as exc:  # fail closed
        verdict = Verdict(
            False, f"cited sources could not be read ({type(exc).__name__})"
        )
    if verdict.ok:
        return None, verdict
    total = _bump("rejected_uncited")
    logger.warning(
        "skill citation gate: rejected_uncited %s %s (%s); total=%s",
        action,
        name,
        verdict.reason,
        total,
    )
    return (
        {
            "success": False,
            "rejected_uncited": True,
            "error": (
                f"rejected_uncited: refusing background {action} for skill '{name}': "
                f"{verdict.reason}. Cite source_ids (existing memory ids or ledger event "
                f"ids) and a verbatim quote (>= {MIN_QUOTE_CHARS} characters) from one of them."
            ),
        },
        None,
    )


def record_lesson(
    action: str, name: str, verdict: Verdict, quote: str, text: str
) -> None:
    """Store the accepted lesson in skmemory and count it. Never breaks the write that succeeded."""
    try:
        default_writer()(
            skill=name,
            action=action,
            text=text,
            quote=normalize(quote),
            cited_ids=list(verdict.cited_ids),
        )
        _bump("accepted_lessons")
    except Exception:
        logger.warning(
            "skill citation gate: lesson for %s accepted but not stored",
            name,
            exc_info=True,
        )
