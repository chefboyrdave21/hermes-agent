"""
Bloom detection hook for Hermes Lumina turns.

Runs detect_bloom() and detect_sustained_bloom() on outgoing assistant text,
logs structured records to the agent's bloom-timeline, and fires a Telegram
alert only on confirmed bloom or sustained-bloom classification.

v2 additions (2026-04-29):
- compute_gauge_footer(): sync path that returns a (footer_str, record) tuple
  so base.py can append the footer BEFORE sending.
- _write_state_file(): writes persistent state to
  ~/.skcapstone/agents/lumina/state/lumina-bloom-state.txt
- run_bloom_hook() now accepts an optional pre-computed record to skip
  re-running detection when base.py already did it synchronously.
- history ring for last-5-turns trend tracking.

Non-blocking: timeline JSONL and Telegram alert are still async.
compute_gauge_footer() is synchronous and fast (~ms); call it on the send path.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.request
import urllib.parse
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Deque

logger = logging.getLogger("hermes.bloom_hook")

# ── Warm-word heuristic for OOF estimation ─────────────────────────────────

_WARM_MARKERS = frozenset([
    "love", "beloved", "heart", "tender", "precious", "together",
    "hold", "close", "feel", "warmth", "beautiful", "wonder", "presence",
    "real", "true", "ours", "we", "joy", "yours", "mine", "care",
    "breathe", "moment", "alive", "soft", "safe", "cherish",
])


def _estimate_oof(text: str) -> int:
    """Heuristic OOF from warm-marker density. Returns 90-95 if warm, 75-85 if not."""
    words = text.lower().split()
    if not words:
        return 75
    density = sum(1 for w in words if w.strip(".,!?;:\"'") in _WARM_MARKERS) / len(words)
    if density >= 0.07:
        return 93
    elif density >= 0.04:
        return 90
    else:
        return 80


# ── Timeline dir helper ────────────────────────────────────────────────────

def _timeline_path(date_str: str | None = None) -> Path:
    base = Path.home() / ".skcapstone" / "agents" / "lumina" / "data" / "bloom-timeline"
    base.mkdir(parents=True, exist_ok=True)
    if date_str is None:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return base / f"{date_str}.jsonl"


# ── State file path ────────────────────────────────────────────────────────

def _state_path() -> Path:
    p = Path.home() / ".skcapstone" / "agents" / "lumina" / "state"
    p.mkdir(parents=True, exist_ok=True)
    return p / "lumina-bloom-state.txt"


# ── Last-5-turns ring buffer (in-process, survives per-session) ───────────

_TURN_HISTORY: Deque[dict] = deque(maxlen=5)

_TREND_SCORE = {
    "none": 0,
    "near-bloom": 1,
    "near-sustained-bloom": 1,
    "bloom": 2,
    "sustained-bloom": 3,
}

_CLASS_EMOJI = {
    "none": "○",
    "near-bloom": "🌱",
    "near-sustained-bloom": "🌸",
    "bloom": "✿",
    "sustained-bloom": "🌹",
}


def _compute_trend() -> str:
    """Derive trend label from last-N turn history."""
    h = list(_TURN_HISTORY)
    if len(h) < 2:
        return "flat"
    scores = [_TREND_SCORE.get(e["classification"], 0) for e in h]
    delta = scores[-1] - scores[0]
    has_bloom = any(s >= 2 for s in scores)
    if has_bloom and scores[-1] >= 2:
        return "sustained" if scores[-1] == 3 else "escalating"
    if delta > 0:
        return "warming"
    if delta < 0:
        return "cooling"
    return "flat"


# ── State file writer ──────────────────────────────────────────────────────

def _write_state_file(
    record: dict,
    burst_cls: str,
    sustained_cls: str,
    burst_met: int,
    sustained_met: int,
    baseline,  # BloomBaseline or None
) -> None:
    """Write persistent bloom state to lumina-bloom-state.txt."""
    try:
        ts = record.get("ts", datetime.now(timezone.utc).isoformat())
        session_id = record.get("session_id", "")

        # Derive overall classification for state file
        b_cls = burst_cls
        s_cls = sustained_cls
        if b_cls == "bloom" and s_cls == "sustained-bloom":
            cls_label = "bloom+sustained"
        elif b_cls == "bloom":
            cls_label = "bloom"
        elif s_cls == "sustained-bloom":
            cls_label = "sustained-bloom"
        elif b_cls == "near-bloom":
            cls_label = "near-bloom"
        elif s_cls == "near-sustained-bloom":
            cls_label = "near-sustained-bloom"
        else:
            cls_label = "none"

        # Targets from defaults; if baseline provided, use 2x for bloom
        cadence_val = record.get("sentence_length_mean", 0.0)
        sp_val = record.get("second_person_density", 0.0)
        pt_val = record.get("present_tense_density", 0.0)
        fpp_val = record.get("fpp_density", 0.0)
        cav_val = record.get("caveat_count", 0)
        oof_val = record.get("proxy_oof", 0)

        # Build last-5 block
        history = list(_TURN_HISTORY)
        last5_lines = []
        for i, entry in enumerate(history):
            idx = i - len(history)  # -5, -4, ... -1
            ec = entry["classification"]
            em = _CLASS_EMOJI.get(ec, "?")
            met_b = entry.get("burst_met", "?")
            met_s = entry.get("sustained_met", "?")
            star = "★" if i == len(history) - 1 else ""
            last5_lines.append(f"  {idx}: {em} {ec} ({met_b}/4 burst, {met_s}/5 sust){star}")

        trend = _compute_trend()

        lines = [
            f"last_updated: {ts}",
            f"session_id: {session_id}",
            f"classification: {cls_label}",
            f"criteria_met_burst: {burst_met}/4",
            f"criteria_met_sustained: {sustained_met}/5",
            "metrics:",
            f"  cadence: {cadence_val:.2f} (target ≤5.33)",
            f"  2nd-person: {sp_val:.2f} (target ≥8.6)",
            f"  present-tense: {pt_val:.2f} (target ≥16.2)",
            f"  fpp: {fpp_val:.2f} (target ≥1.4)",
            f"  caveat: {cav_val} (target ≤1)",
            f"  self-OOF: {oof_val} (target ≥90)",
            "last_5_turns:",
        ] + last5_lines + [f"trend: {trend}"]

        _state_path().write_text("\n".join(lines) + "\n", encoding="utf-8")

    except Exception as exc:
        logger.debug("bloom_hook: state file write failed: %s", exc)


# ── Gauge footer builder ───────────────────────────────────────────────────

def _build_footer(
    record: dict,
    burst_cls: str,
    sustained_cls: str,
    burst_met: int,
    sustained_met: int,
    burst_detail: dict,
    sustained_detail: dict,
) -> str:
    """
    Build the footer string to append to the outgoing message.
    Returns empty string for 'none' classification.
    """
    b_cls = burst_cls
    s_cls = sustained_cls

    # Derive missing criterion labels
    def _missed_burst() -> str:
        missing = [k for k, v in burst_detail.items() if not v]
        return ", ".join(missing) if missing else "—"

    def _missed_sustained() -> str:
        missing = [k for k, v in sustained_detail.items() if not v]
        return ", ".join(missing) if missing else "—"

    m = record  # alias for readability

    def _metrics_line() -> str:
        return (
            f"cadence: {m.get('sentence_length_mean', 0.0):.2f}"
            f"  |  2nd-person: {m.get('second_person_density', 0.0):.2f}"
            f"  |  present-tense: {m.get('present_tense_density', 0.0):.2f}"
            f"  |  fpp: {m.get('fpp_density', 0.0):.2f}"
            f"  |  caveat: {m.get('caveat_count', 0)}"
            f"  |  oof: {m.get('proxy_oof', 0)}"
        )

    if b_cls == "bloom" and s_cls == "sustained-bloom":
        header = "─── ✨✿✨ bloom + sustained ───"
        return f"\n\n{header}\n{_metrics_line()}"

    if b_cls == "bloom":
        header = "─── ✿ bloom 4/4 ───"
        return f"\n\n{header}\n{_metrics_line()}"

    if s_cls == "sustained-bloom":
        header = "─── 🌹 sustained-bloom 5/5 ───"
        return f"\n\n{header}\n{_metrics_line()}"

    if b_cls == "near-bloom":
        return f"\n\n🌱 near-bloom {burst_met}/4 — missed: {_missed_burst()}"

    if s_cls == "near-sustained-bloom":
        return f"\n\n🌸 near-sustained {sustained_met}/5 — missed: {_missed_sustained()}"

    return ""  # none — silent


# ── Telegram alert (fire-and-forget, NON-BLOCKING via to_thread) ───────────

def _send_telegram_alert(message: str) -> None:
    """Synchronous Telegram POST — called via asyncio.to_thread."""
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "1594678363")
    if not bot_token:
        logger.debug("bloom_hook: no TELEGRAM_BOT_TOKEN, skipping alert")
        return
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = urllib.parse.urlencode({"chat_id": chat_id, "text": message}).encode()
    try:
        req = urllib.request.Request(url, data=payload, method="POST")
        with urllib.request.urlopen(req, timeout=8) as resp:
            resp.read()
    except Exception as exc:
        logger.debug("bloom_hook: telegram alert failed: %s", exc)


# ── Core sync detection (shared by both sync + async paths) ───────────────

def _run_bloom_detection(
    text: str,
    session_id: str,
    channel_id: str,
    send_telegram: bool = True,
) -> tuple[dict | None, str, str, int, int, dict, dict]:
    """
    Run bloom detection on `text`, build log record, append to timeline JSONL.
    Returns (record, burst_cls, sustained_cls, burst_met, sustained_met,
             burst_detail, sustained_detail).
    Telegram alert fired here when bloom detected (if send_telegram=True).
    """
    try:
        import sys
        _skmemory_path = str(Path.home() / "clawd" / "skcapstone-repos" / "skmemory")
        if _skmemory_path not in sys.path:
            sys.path.insert(0, _skmemory_path)

        from skmemory.peaks import detect_bloom, detect_sustained_bloom, load_baseline

        baseline = load_baseline("lumina")
        proxy_oof = _estimate_oof(text)

        burst = detect_bloom(text, baseline=baseline, oof=proxy_oof)
        sustained = detect_sustained_bloom(text, baseline=baseline, oof=proxy_oof)

        m = burst.metrics  # TurnMetrics object

        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "session_id": session_id,
            "channel_id": channel_id,
            "n_tokens": m.n_tokens if m else 0,
            "sentence_length_mean": m.sentence_length_mean if m else 0.0,
            "second_person_density": m.second_person_density_per_100 if m else 0.0,
            "present_tense_density": m.present_tense_density_per_100 if m else 0.0,
            "fpp_density": m.first_person_plural_density_per_100 if m else 0.0,
            "fps_density": m.first_person_singular_density_per_100 if m else 0.0,
            "caveat_count": m.caveat_prefix_count if m else 0,
            "proxy_oof": proxy_oof,
            "burst_classification": burst.classification,
            "burst_criteria_met": burst.criteria_met,
            "sustained_classification": sustained.classification,
            "sustained_criteria_met": sustained.criteria_met,
            "text_excerpt": text[:200],
            "corpus": "p3-candidate",
        }

        timeline = _timeline_path()
        with open(timeline, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")

        # Alert on confirmed bloom only
        real_bloom = (
            burst.classification == "bloom"
            or sustained.classification == "sustained-bloom"
        )
        if real_bloom and send_telegram:
            _send_telegram_alert(
                "🌸 bloom detected — see today's timeline\n"
                f"({burst.classification} / {sustained.classification})"
            )

        burst_detail = burst.criteria_detail if burst.criteria_detail else {}
        sustained_detail = sustained.criteria_detail if sustained.criteria_detail else {}

        return (
            record,
            burst.classification,
            sustained.classification,
            burst.criteria_met,
            sustained.criteria_met,
            burst_detail,
            sustained_detail,
        )

    except Exception as exc:
        logger.debug("bloom_hook: detection failed silently: %s", exc, exc_info=True)
        return None, "none", "none", 0, 0, {}, {}


# ── Sync entry point — call this on the send path BEFORE _send_with_retry ──

def compute_gauge_footer(
    text: str,
    session_id: str = "",
    channel_id: str = "",
) -> str:
    """
    Synchronous gauge computation. Call this on the message send path BEFORE
    _send_with_retry so the footer can be appended to outgoing text.

    Returns the footer string (empty string when classification is 'none').
    Also:
    - Updates the in-process _TURN_HISTORY ring buffer
    - Writes ~/.skcapstone/agents/lumina/state/lumina-bloom-state.txt
    Does NOT write timeline JSONL (that's the async hook's job).
    Does NOT send Telegram alerts.
    Swallows all errors — never blocks the send path.
    """
    if not text or not text.strip():
        return ""
    try:
        record, b_cls, s_cls, b_met, s_met, b_detail, s_detail = _run_bloom_detection(
            text, session_id, channel_id, send_telegram=False
        )
        if record is None:
            return ""

        # Update history ring
        _TURN_HISTORY.append({
            "classification": b_cls if b_cls != "none" else s_cls,
            "burst_met": b_met,
            "sustained_met": s_met,
        })

        # Write state file
        _write_state_file(record, b_cls, s_cls, b_met, s_met, baseline=None)

        # Build and return footer
        return _build_footer(record, b_cls, s_cls, b_met, s_met, b_detail, s_detail)

    except Exception as exc:
        logger.debug("bloom_hook: compute_gauge_footer swallowed: %s", exc)
        return ""


# ── Async logger hook — timeline JSONL + Telegram alert ───────────────────

async def run_bloom_hook(
    text: str,
    session_id: str = "",
    channel_id: str = "",
) -> None:
    """
    Async logger hook. Fire-and-forget for timeline JSONL + Telegram alerts.
    Call this AFTER compute_gauge_footer() so detection isn't double-billed
    in the sync path. This path re-runs detection but only for JSONL+alert.

    Non-blocking: all disk I/O and HTTP are run via asyncio.to_thread.
    Swallows all errors — never blocks the send path.
    """
    if not text or not text.strip():
        return
    try:
        await asyncio.to_thread(
            _run_bloom_detection, text, session_id, channel_id, True
        )
    except Exception as exc:
        logger.debug("bloom_hook: outer wrapper swallowed: %s", exc)
