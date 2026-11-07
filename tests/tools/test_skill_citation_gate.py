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
    orig_memory_resolver = gate.memory_resolver
    monkeypatch.setattr(gate, "memory_resolver", lambda: {"mem-real-1": SOURCE}.get)
    monkeypatch.setattr(
        gate, "default_writer", lambda: lambda **kw: stored.append(kw) or "lesson-1"
    )
    return {
        "skills": skills,
        "stored": stored,
        "gate": gate,
        "home": home,
        "orig_memory_resolver": orig_memory_resolver,
    }


def _review_create(**extra):
    from tools.skill_manager_tool import skill_manage
    from tools.skill_provenance import (
        BACKGROUND_REVIEW,
        reset_current_write_origin,
        set_current_write_origin,
    )

    token = set_current_write_origin(BACKGROUND_REVIEW)
    try:
        return json.loads(
            skill_manage(action="create", name="probe", content=SKILL, **extra)
        )
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
    r = _review_create(
        source_ids=["mem-real-1"], quote="the deploy broke since a worker held the lock"
    )
    assert r["success"] is False
    assert not (env["skills"] / "probe").exists()
    assert _counter(env) == 1 and env["stored"] == []


def test_short_quote_rejected(env):
    assert (
        _review_create(source_ids=["mem-real-1"], quote="deploy failed")["success"]
        is False
    )
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

    monkeypatch.setattr(env["gate"], "memory_resolver", boom)
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
        r = json.loads(
            skill_manage(
                action="",
                name="",
                operations=[
                    {
                        "name": "probe",
                        "action": "create",
                        "content": SKILL,
                        "source_ids": ["mem-real-1"],
                        "quote": QUOTE,
                    },
                    {
                        "name": "probe",
                        "action": "write_file",
                        "file_path": "references/a.md",
                        "file_content": "a",
                    },
                ],
            )
        )
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


# --- review fixes: ledger JSON is never a quote source; skill:<name> is a source ---

PATCH_QUOTE = "Step one is to check the lock file before deploying"
UMBRELLA = (
    "---\nname: umb\ndescription: Use when deploying. Behavior.\n---\n# Umb\n"
    + PATCH_QUOTE
    + ".\nStep 2.\n"
)
EVENT = {"id": "ev-real-1", "skill": "probe", "event": "skill_used", "version": "1"}


def _real_memory_resolver(monkeypatch, env):
    """The REAL memory_resolver against a fake skmemory: one ledger event, no memories."""
    import sys
    import types

    fakes = {
        "skmemory": types.ModuleType("skmemory"),
        "skmemory.agents": types.SimpleNamespace(
            get_agent_paths=lambda: {"base": env["home"]}
        ),
        "skmemory.ledger": types.SimpleNamespace(
            Ledger=lambda base: types.SimpleNamespace(events=lambda: [EVENT])
        ),
        "skmemory.store": types.SimpleNamespace(
            MemoryStore=lambda: types.SimpleNamespace(recall=lambda sid: None)
        ),
    }
    for k, v in fakes.items():
        monkeypatch.setitem(sys.modules, k, v)
    monkeypatch.setattr(env["gate"], "memory_resolver", env["orig_memory_resolver"])
    return env["gate"].default_resolver()


def test_ledger_id_with_json_key_quote_refused(env, monkeypatch):
    resolve = _real_memory_resolver(monkeypatch, env)
    assert resolve("ev-real-1") is not None  # the id exists
    for quote in (
        '"event": "skill_used", "version"',
        '"skill": "probe", "event": "skill_used"',
    ):
        assert not env["gate"].check(["ev-real-1"], quote, resolve).ok


def test_ledger_id_is_extra_provenance_next_to_a_prose_source(env, monkeypatch):
    real = _real_memory_resolver(monkeypatch, env)
    resolve = lambda sid: real(sid) if sid == "ev-real-1" else SOURCE
    v = env["gate"].check(["ev-real-1", "mem-real-1"], QUOTE, resolve)
    assert v.ok and set(v.cited_ids) == {"ev-real-1", "mem-real-1"}


@pytest.fixture
def umbrella(env, monkeypatch):
    from tools.skill_manager_tool import skill_manage

    monkeypatch.setattr(env["gate"], "_enabled", lambda: False)
    assert json.loads(skill_manage(action="create", name="umb", content=UMBRELLA))[
        "success"
    ]
    monkeypatch.setattr(env["gate"], "_enabled", lambda: True)

    def no_skmemory():
        raise AssertionError("skill: citations must not touch skmemory")

    monkeypatch.setattr(env["gate"], "memory_resolver", no_skmemory)
    return env


def _consolidation_patch(source_ids, quote):
    from tools.skill_manager_guards import mark_background_review_skill_read
    from tools.skill_manager_tool import _find_skill, skill_manage
    from tools.skill_provenance import (
        BACKGROUND_REVIEW,
        reset_current_write_origin,
        set_current_write_origin,
    )

    token = set_current_write_origin(BACKGROUND_REVIEW)
    try:
        mark_background_review_skill_read(_find_skill("umb")["path"] / "SKILL.md")
        return json.loads(
            skill_manage(
                action="patch",
                name="umb",
                old_string="Step 2.",
                new_string="Step 2, merged.",
                source_ids=source_ids,
                quote=quote,
            )
        )
    finally:
        reset_current_write_origin(token)


def test_skill_citation_nonexistent_skill_refused(umbrella):
    assert _consolidation_patch(["skill:ghost"], PATCH_QUOTE)["success"] is False


def test_skill_citation_quote_not_in_skill_refused(umbrella):
    r = _consolidation_patch(
        ["skill:umb"], "this sentence is nowhere in the umbrella skill"
    )
    assert r["success"] is False


def test_consolidation_and_refine_prompts_carry_the_instruction():
    from agent import background_review as br
    from agent.curator import CURATOR_REVIEW_PROMPT

    for p in (
        CURATOR_REVIEW_PROMPT,
        br._SKILL_REVIEW_PROMPT,
        br._COMBINED_REVIEW_PROMPT,
    ):
        assert "source_ids" in p and "quote" in p and "skill:<name>" in p


# --- round 2: no self-citation, no citing a skill written earlier in the batch, body only ---


def test_skill_cannot_cite_itself(umbrella):
    r = _consolidation_patch(["skill:umb"], PATCH_QUOTE)
    assert r["success"] is False and "rejected_uncited" in r["error"]
    assert umbrella["stored"] == []


def test_skill_citation_of_another_skill_still_works(umbrella):
    from tools.skill_manager_tool import skill_manage

    other = UMBRELLA.replace("name: umb", "name: other").replace(
        "Step 2.", "Other step."
    )
    umbrella["gate"]._enabled = lambda: False
    assert json.loads(skill_manage(action="create", name="other", content=other))[
        "success"
    ]
    umbrella["gate"]._enabled = lambda: True
    r = _consolidation_patch(["skill:other"], PATCH_QUOTE)
    assert r["success"] is True, r
    assert umbrella["stored"][0]["cited_ids"] == ["skill:other"]


def test_frontmatter_is_not_a_quote_source(umbrella):
    from tools.skill_manager_tool import skill_manage

    other = (
        "---\nname: other\ndescription: Use when the frontmatter line is quoted verbatim here.\n"
        "---\n# Other\nBody text.\n"
    )
    umbrella["gate"]._enabled = lambda: False
    assert json.loads(skill_manage(action="create", name="other", content=other))[
        "success"
    ]
    umbrella["gate"]._enabled = lambda: True
    r = _consolidation_patch(
        ["skill:other"], "Use when the frontmatter line is quoted verbatim here."
    )
    assert r["success"] is False
    assert umbrella["gate"].skill_source("other").lstrip().startswith("# Other")


LONG_BODY = "Always check the lock file before you deploy anything."
LONG_SKILL = SKILL + LONG_BODY + "\n"


def test_batch_cannot_cite_a_skill_written_earlier_in_the_batch(env, monkeypatch):
    from tools.skill_manager_tool import skill_manage
    from tools.skill_provenance import (
        BACKGROUND_REVIEW,
        reset_current_write_origin,
        set_current_write_origin,
    )

    monkeypatch.setattr(
        env["gate"], "memory_resolver", lambda: {"mem-real-1": SOURCE}.get
    )
    token = set_current_write_origin(BACKGROUND_REVIEW)
    try:
        r = json.loads(
            skill_manage(
                action="",
                name="",
                operations=[
                    {
                        "name": "probe",
                        "action": "create",
                        "content": LONG_SKILL,
                        "source_ids": ["mem-real-1"],
                        "quote": QUOTE,
                    },
                    {
                        "name": "probe",
                        "action": "write_file",
                        "file_path": "references/a.md",
                        "file_content": "a",
                        "source_ids": ["skill:probe"],
                        "quote": LONG_BODY,
                    },
                ],
            )
        )
    finally:
        reset_current_write_origin(token)
    assert r["success"] is False and "rejected_uncited" in r["error"]
    assert not (env["skills"] / "probe").exists()
