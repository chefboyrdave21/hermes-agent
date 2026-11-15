"""Citation gate for autonomous skill writes (SKMEM-P4-04).

The background review fork (the curator) may write a lesson or patch a skill only
when it cites at least one source experience id (a skmemory memory id or an outcome
ledger event id) that actually exists, and quotes at least ``MIN_QUOTE_CHARS``
characters verbatim from one cited source (whitespace-normalised). Anything else is
refused, ``rejected_uncited`` is incremented in the curator state (shown by
``hermes curator status``) and a log line is written. Accepted lessons are stored in
skmemory as ``kind=procedure, outcome=lesson`` with the verified ids.

skmemory is reached through its CLI, never imported: it lives in its own venv
(``~/.skenv``), not Hermes's. ``HERMES_SKMEMORY_BIN`` or ``curator.skmemory_bin`` names
the binary, else ``skmemory`` on PATH, else ``~/.skenv/bin/skmemory``; a missing CLI is
an error (the gate fails closed and says why), never a silent skip. Memory ids are
checked with ``skmemory recall --exact`` under the turn's reader (the session scope): in
a non-owner turn an id that reader may not see reads as not found, so it cannot be
cited. Lessons are stored only from an owner turn (non-owner turns never write memory),
as ``source_tier=self-generated`` with ``source_ref`` naming the session or run.

The quote must come from prose (a memory or a skill's text). Fails closed: if the sources
cannot be read, nothing is written. Foreground, user-directed writes are never gated.
``curator.citation_gate: false`` turns it off.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
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
    if not text.lstrip().startswith("---"):
        return text
    # An unterminated frontmatter block has no body to quote: return nothing, not the file.
    return _FRONTMATTER.sub("", text, count=1) if _FRONTMATTER.match(text) else ""


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


SKMEMORY_BIN_ENV = "HERMES_SKMEMORY_BIN"
DEFAULT_SKMEMORY_BIN = "~/.skenv/bin/skmemory"
CLI_TIMEOUT_SECONDS = 60
# What the skmemory CLI child inherits: agent selection, home and locale. Never Hermes's
# provider keys or tokens.
_CLI_ENV_KEYS = (
    "HOME", "PATH", "LANG", "LC_ALL", "TZ", "USER", "LOGNAME", "XDG_RUNTIME_DIR",
    "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "SKAGENT", "SKCAPSTONE_AGENT",
    "SKMEMORY_AGENT", "SK_DEFAULT_AGENT", "SKCAPSTONE_HOME", "SKMEMORY_READER",
)
_MEMORY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{2,127}")


class SkmemoryCliError(RuntimeError):
    """The skmemory CLI is missing or failed: the gate refuses, the lesson is not stored."""


def _configured_bin() -> str:
    env = os.environ.get(SKMEMORY_BIN_ENV, "").strip()
    if env:
        return env
    try:
        from agent import curator

        return str(curator._load_config().get("skmemory_bin") or "").strip()
    except Exception:
        return ""


def skmemory_cli() -> str:
    """Path of the skmemory CLI, or :class:`SkmemoryCliError` naming where it looked."""
    configured = _configured_bin()
    if configured:
        path = Path(configured).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
        raise SkmemoryCliError(
            f"skmemory CLI not found at {path} (from {SKMEMORY_BIN_ENV} or curator.skmemory_bin)"
        )
    found = shutil.which("skmemory")
    if found:
        return found
    default = Path(DEFAULT_SKMEMORY_BIN).expanduser()
    if default.is_file() and os.access(default, os.X_OK):
        return str(default)
    raise SkmemoryCliError(
        f"skmemory CLI not found on PATH or at {DEFAULT_SKMEMORY_BIN}; set {SKMEMORY_BIN_ENV} "
        "or curator.skmemory_bin"
    )


def _run_cli(binary: str, args: list) -> subprocess.CompletedProcess:
    env = {k: os.environ[k] for k in _CLI_ENV_KEYS if k in os.environ}
    try:
        return subprocess.run(
            [binary, "--no-vector", *args], capture_output=True, text=True,
            timeout=CLI_TIMEOUT_SECONDS, env=env, stdin=subprocess.DEVNULL, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SkmemoryCliError(f"skmemory CLI failed to run: {type(exc).__name__}") from exc


def _turn_reader() -> Optional[str]:
    """The session scope's reader: None for the owner, else ``channel:<id>`` or ``@public``.
    Unreadable scope fails closed to ``@public``."""
    try:
        from tools import session_scope

        return session_scope.session_reader()
    except Exception:
        return "@public"


def memory_resolver() -> Resolver:
    """Read-only resolver over skmemory through its CLI, under the turn's reader. Memory ids
    give prose (title + content); an id the reader may not see, or that does not exist, is
    None. Only an exact id counts (no prefix match)."""
    binary = skmemory_cli()
    reader = _turn_reader()

    def resolve(sid: str):
        if not _MEMORY_ID.fullmatch(sid):
            return None
        args = ["recall", "--exact", *(["--reader", reader] if reader else []), "--", sid]
        proc = _run_cli(binary, args)
        if proc.returncode == 1 and "not found" in (proc.stderr or "").lower():
            return None
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip()[-200:]
            raise SkmemoryCliError(f"skmemory recall exit {proc.returncode}: {tail}")
        try:
            mem = json.loads(proc.stdout)
        except ValueError as exc:
            raise SkmemoryCliError("skmemory recall printed no JSON") from exc
        if not isinstance(mem, dict) or mem.get("id") != sid:
            return None
        return f"{mem.get('title', '')}\n{mem.get('content', '')}"

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


def _source_ref() -> str:
    try:
        from gateway.session_context import get_session_env

        sid = get_session_env("HERMES_SESSION_ID", "").strip()
    except Exception:
        sid = ""
    if sid:
        return f"hermes-curator:session:{sid}"
    return f"hermes-curator:run:{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"


def default_writer() -> Callable[..., Optional[str]]:
    """Store an accepted lesson with ``skmemory snapshot`` and return its id."""
    binary = skmemory_cli()

    def write(
        *, skill: str, action: str, text: str, quote: str, cited_ids: list
    ) -> Optional[str]:
        meta = {"cited_ids": list(cited_ids), "quote": quote, "skill": skill, "action": action}
        args = [
            "snapshot", "--layer", "mid-term",
            "--tags", f"lesson,hermes-curator,skill:{skill}",
            "--source", "hermes-curator", "--source-tier", "self-generated",
            "--source-ref", _source_ref(), "--kind", "procedure", "--outcome", "lesson",
            "--metadata-json", json.dumps(meta), "--json",
            "--", f"Skill lesson: {skill}", f"{text[:2000]}\n\nQuote: {quote}",
        ]
        proc = _run_cli(binary, args)
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip()[-200:]
            raise SkmemoryCliError(f"skmemory snapshot exit {proc.returncode}: {tail}")
        try:
            out = json.loads(proc.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError) as exc:
            raise SkmemoryCliError("skmemory snapshot printed no JSON") from exc
        if (out.get("kind"), out.get("outcome")) != ("procedure", "lesson"):
            logger.warning(
                "skill citation gate: lesson %s stored as kind=%s outcome=%s",
                out.get("id"), out.get("kind"), out.get("outcome"),
            )
        return out.get("id")

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
    except SkmemoryCliError as exc:  # fail closed, and say why
        verdict = Verdict(False, f"cited sources could not be read: {exc}")
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
    """Store the accepted lesson in skmemory and count it. Never breaks the write that succeeded.
    Owner turns only: a non-owner turn never writes memory, so its lesson is counted and logged
    as not stored."""
    if _turn_reader() is not None:
        _bump("lessons_not_stored_non_owner")
        logger.warning(
            "skill citation gate: lesson for %s accepted but not stored (non-owner turn)", name
        )
        return
    try:
        default_writer()(
            skill=name,
            action=action,
            text=text,
            quote=normalize(quote),
            cited_ids=list(verdict.cited_ids),
        )
        _bump("accepted_lessons")
    except SkmemoryCliError as exc:
        _bump("lessons_not_stored")
        logger.error("skill citation gate: lesson for %s accepted but not stored: %s", name, exc)
    except Exception:
        _bump("lessons_not_stored")
        logger.warning(
            "skill citation gate: lesson for %s accepted but not stored",
            name,
            exc_info=True,
        )
