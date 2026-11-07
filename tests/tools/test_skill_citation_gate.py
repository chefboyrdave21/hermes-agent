"""Citation gate for background-review skill writes (SKMEM-P4-04).

A lesson or skill patch from the review fork is written only when it cites a
source experience id that exists and quotes at least 20 characters verbatim
from a cited source. Hermetic: sources and the lesson store are fakes.
"""

import json

import pytest

SOURCE = "The deploy failed because the lock file was held by a stale   worker process."
QUOTE = "lock file was held by a stale worker process"  # whitespace differs from SOURCE
SKILL = "---\nname: probe\ndescription: Use when probing. Behavior.\n---\n# Probe\nStep 1.\n"


@pytest.fixture
def env(tmp_path, monkeypatch):
    from agent import skill_utils
    from tools import skill_citation_gate as gate
    from tools import skill_ledger, skill_manager_tool, skill_usage

    home = tmp_path / "home"
    skills = home / "skills"
    skills.mkdir(parents=True)
    monkeypatch.setattr(skill_ledger, "get_hermes_home", lambda: home)
    monkeypatch.setattr(skill_usage, "get_hermes_home", lambda: home)
    monkeypatch.setattr("agent.curator.get_hermes_home", lambda: home)
    monkeypatch.setattr(skill_manager_tool, "SKILLS_DIR", skills)
    monkeypatch.setattr(skill_utils, "get_all_skills_dirs", lambda: [skills])
    monkeypatch.setattr(gate, "_enabled", lambda: True)
    stored = []
    monkeypatch.setattr(gate, "default_resolver", lambda: {"mem-real-1": SOURCE}.get)
    monkeypatch.setattr(gate, "default_writer", lambda: lambda **kw: stored.append(kw) or "lesson-1")
    return {"skills": skills, "stored": stored, "gate": gate, "home": home}


def _review_create(**extra):
    from tools.skill_manager_tool import skill_manage
    from tools.skill_provenance import (
        BACKGROUND_REVIEW,
        reset_current_write_origin,
        set_current_write_origin,
    )

    token = set_current_write_origin(BACKGROUND_REVIEW)
    try:
        return json.loads(skill_manage(action="create", name="probe", content=SKILL, **extra))
    finally:
        reset_current_write_origin(token)


def _counter(env):
    from agent import curator

    return curator.load_state().get("rejected_uncited", 0)


def test_fabricated_id_rejected(env):
    r = _review_create(source_ids=["mem-fake-9"], quote=QUOTE)
    assert r["success"] is False and "rejected_uncited" in r["error"]
    assert not (env["skills"] / "probe").exists()
    assert _counter(env) == 1 and env["stored"] == []


def test_real_id_paraphrase_rejected(env):
    r = _review_create(source_ids=["mem-real-1"], quote="the deploy broke since a worker held the lock")
    assert r["success"] is False
    assert not (env["skills"] / "probe").exists()
    assert _counter(env) == 1 and env["stored"] == []


def test_short_quote_rejected(env):
    assert _review_create(source_ids=["mem-real-1"], quote="deploy failed")["success"] is False
    assert _counter(env) == 1


def test_missing_citation_rejected(env):
    assert _review_create()["success"] is False
    assert _counter(env) == 1


def test_real_id_verbatim_quote_accepted_and_lesson_stored(env):
    r = _review_create(source_ids=["mem-real-1", "mem-fake-9"], quote=QUOTE)
    assert r["success"] is True, r
    assert (env["skills"] / "probe" / "SKILL.md").exists()
    assert _counter(env) == 0
    (lesson,) = env["stored"]
    assert lesson["cited_ids"] == ["mem-real-1"]  # only ids that resolved
    assert lesson["quote"] == QUOTE and lesson["skill"] == "probe"


def test_foreground_writes_are_not_gated(env):
    from tools.skill_manager_tool import skill_manage

    r = json.loads(skill_manage(action="create", name="probe", content=SKILL))
    assert r["success"] is True and _counter(env) == 0


def test_gate_disabled_by_config_lets_writes_through(env, monkeypatch):
    monkeypatch.setattr(env["gate"], "_enabled", lambda: False)
    assert _review_create()["success"] is True


def test_unavailable_store_fails_closed(env, monkeypatch):
    def boom():
        raise RuntimeError("no store")

    monkeypatch.setattr(env["gate"], "default_resolver", boom)
    assert _review_create(source_ids=["mem-real-1"], quote=QUOTE)["success"] is False
    assert _counter(env) == 1


def test_batch_ops_are_gated_and_roll_back(env):
    from tools.skill_manager_tool import skill_manage
    from tools.skill_provenance import (
        BACKGROUND_REVIEW,
        reset_current_write_origin,
        set_current_write_origin,
    )

    token = set_current_write_origin(BACKGROUND_REVIEW)
    try:
        r = json.loads(skill_manage(action="", name="", operations=[
            {"name": "probe", "action": "create", "content": SKILL,
             "source_ids": ["mem-real-1"], "quote": QUOTE},
            {"name": "probe", "action": "write_file", "file_path": "references/a.md",
             "file_content": "a"},
        ]))
    finally:
        reset_current_write_origin(token)
    assert r["success"] is False
    assert not (env["skills"] / "probe").exists()


def test_normalize_and_verdicts():
    from tools.skill_citation_gate import check

    res = {"a": "x  y\n\tz " + "q" * 30}.get
    assert check(["a"], "x y z " + "q" * 20, res).ok
    assert not check([], "x y z " + "q" * 20, res).ok
    assert not check(["a"], "x y z", res).ok
