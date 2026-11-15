"""The citation gate reaches skmemory through its CLI, never by import.

A fake ``skmemory`` executable stands in for the real CLI: it serves ``recall --exact``
under a reader (``--reader`` or ``SKMEMORY_READER``), hides owner-only memories from any
other reader exactly as skmemory does (not found), and records ``snapshot`` calls.
"""

import json
import os
import stat
import sys
import textwrap

import pytest

from gateway import session_context as sc

SOURCE = "The deploy failed because the lock file was held by a stale   worker process."
QUOTE = "lock file was held by a stale worker process"

FAKE = textwrap.dedent(
    """\
    #!{python}
    import json, os, sys
    home = os.environ["HOME"]
    db = json.load(open(os.path.join(home, "fake-mem.json")))
    argv = sys.argv[1:]
    with open(os.path.join(home, "calls.jsonl"), "a") as f:
        f.write(json.dumps({{"argv": argv, "env": sorted(os.environ)}}) + "\\n")
    assert argv[0] == "--no-vector", argv
    cmd = argv[1]
    if cmd == "recall":
        rest = argv[2:]
        assert "--exact" in rest
        reader = rest[rest.index("--reader") + 1] if "--reader" in rest else os.environ.get("SKMEMORY_READER")
        sid = rest[rest.index("--") + 1]
        mem = db.get(sid)
        if mem and (reader is None or mem["audience"] == "@public"):
            print(json.dumps({{"id": mem.get("return_id", sid), "title": mem["title"], "content": mem["content"]}}))
            sys.exit(0)
        print(f"Memory not found: {{sid}}", file=sys.stderr)
        sys.exit(1)
    if cmd == "snapshot":
        print(json.dumps({{"id": "lesson-0001", "layer": "mid-term", "kind": "procedure",
                          "outcome": "lesson", "source_tier": "self-generated"}}))
        sys.exit(0)
    sys.exit(2)
    """
)


@pytest.fixture
def cli(tmp_path, monkeypatch):
    from tools import skill_citation_gate as gate

    home = tmp_path / "home"
    home.mkdir()
    (home / "fake-mem.json").write_text(json.dumps({
        "mem-public-1": {"title": "Deploy", "content": SOURCE, "audience": "@public"},
        "mem-owner-1": {"title": "Private deploy", "content": SOURCE, "audience": "owner"},
        "mem-prefix-1": {"title": "P", "content": SOURCE, "audience": "@public",
                         "return_id": "mem-prefix-1-longer"},
    }))
    binary = tmp_path / "bin" / "skmemory"
    binary.parent.mkdir()
    binary.write_text(FAKE.format(python=sys.executable))
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv(gate.SKMEMORY_BIN_ENV, str(binary))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-not-for-children")
    monkeypatch.delenv("SKMEMORY_READER", raising=False)
    monkeypatch.delenv("_HERMES_GATEWAY", raising=False)
    monkeypatch.setenv("HERMES_MEMORY_OWNER", "telegram:111")
    monkeypatch.setattr(sc, "_session_context_engaged", True)
    monkeypatch.setattr(gate, "_bump", lambda key: 1)
    yield {"gate": gate, "home": home, "binary": binary}
    sc.clear_session_vars([])


def _calls(home):
    p = home / "calls.jsonl"
    return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []


def _owner_dm():
    sc.set_session_vars(platform="telegram", chat_id="111", chat_type="dm", user_id="111",
                        session_id="sess-owner-1")


def _group():
    sc.set_session_vars(platform="telegram", chat_id="-100500", chat_type="group", user_id="333")


# ---- accept and store ---------------------------------------------------------------------
def test_owner_lesson_citing_a_memory_is_accepted_and_stored(cli):
    gate = cli["gate"]
    _owner_dm()
    v = gate.check(["mem-owner-1"], QUOTE, gate.memory_resolver())
    assert v.ok and v.cited_ids == ("mem-owner-1",)
    gate.record_lesson("create", "probe", v, QUOTE, "Check the lock file first.")
    snap = [c for c in _calls(cli["home"]) if c["argv"][1] == "snapshot"]
    assert len(snap) == 1
    argv = snap[0]["argv"]
    opt = lambda k: argv[argv.index(k) + 1]
    assert opt("--source-tier") == "self-generated"
    assert opt("--source-ref") == "hermes-curator:session:sess-owner-1"
    assert (opt("--kind"), opt("--outcome"), opt("--layer")) == ("procedure", "lesson", "mid-term")
    assert set(opt("--tags").split(",")) == {"lesson", "hermes-curator", "skill:probe"}
    assert json.loads(opt("--metadata-json"))["cited_ids"] == ["mem-owner-1"]
    assert argv[argv.index("--") + 1] == "Skill lesson: probe"


def test_cli_child_gets_no_provider_secrets(cli):
    gate = cli["gate"]
    _owner_dm()
    gate.memory_resolver()("mem-public-1")
    envs = [c["env"] for c in _calls(cli["home"])]
    assert envs and all("OPENAI_API_KEY" not in e for e in envs)


# ---- refuse hidden or unknown ids -----------------------------------------------------------
def test_non_owner_cannot_cite_a_memory_its_reader_may_not_see(cli):
    gate = cli["gate"]
    _group()
    resolve = gate.memory_resolver()
    assert resolve("mem-owner-1") is None  # hidden reads as not found
    assert not gate.check(["mem-owner-1"], QUOTE, resolve).ok
    recall = [c["argv"] for c in _calls(cli["home"]) if c["argv"][1] == "recall"]
    assert recall and recall[-1][recall[-1].index("--reader") + 1] == "channel:-100500"


def test_non_owner_may_cite_what_its_reader_can_see_but_stores_nothing(cli):
    gate = cli["gate"]
    _group()
    v = gate.check(["mem-public-1"], QUOTE, gate.memory_resolver())
    assert v.ok
    gate.record_lesson("create", "probe", v, QUOTE, "text")
    assert not [c for c in _calls(cli["home"]) if c["argv"][1] == "snapshot"]


def test_unknown_id_is_refused(cli):
    gate = cli["gate"]
    _owner_dm()
    v = gate.check(["mem-does-not-exist"], QUOTE, gate.memory_resolver())
    assert not v.ok and v.reason == "none of the cited ids exists"


def test_a_prefix_match_is_not_the_cited_id(cli):
    gate = cli["gate"]
    _owner_dm()
    assert gate.memory_resolver()("mem-prefix-1") is None


@pytest.mark.parametrize("sid", ["--reader", "-x", "a b", "../etc/passwd", ""])
def test_ids_that_could_be_options_or_paths_never_reach_the_cli(cli, sid):
    gate = cli["gate"]
    _owner_dm()
    assert gate.memory_resolver()(sid) is None
    assert not _calls(cli["home"])


def test_unbound_gateway_turn_reads_as_public(cli, monkeypatch):
    gate = cli["gate"]
    monkeypatch.setenv("_HERMES_GATEWAY", "1")
    sc.clear_session_vars([])
    assert gate.memory_resolver()("mem-owner-1") is None


# ---- CLI missing ------------------------------------------------------------------------------
def test_missing_configured_cli_is_a_clear_refusal(cli, monkeypatch, tmp_path):
    gate = cli["gate"]
    monkeypatch.setenv(gate.SKMEMORY_BIN_ENV, str(tmp_path / "nope" / "skmemory"))
    with pytest.raises(gate.SkmemoryCliError, match="skmemory CLI not found at"):
        gate.skmemory_cli()


def test_no_cli_anywhere_names_where_it_looked(cli, monkeypatch):
    gate = cli["gate"]
    monkeypatch.delenv(gate.SKMEMORY_BIN_ENV)
    monkeypatch.setattr(gate, "_configured_bin", lambda: "")
    monkeypatch.setenv("PATH", "/nonexistent")
    with pytest.raises(gate.SkmemoryCliError, match=r"on PATH or at ~/.skenv/bin/skmemory"):
        gate.skmemory_cli()


def test_missing_cli_makes_the_review_refuse_with_the_reason(cli, monkeypatch, tmp_path):
    from tools.skill_provenance import BACKGROUND_REVIEW, reset_current_write_origin, set_current_write_origin

    gate = cli["gate"]
    monkeypatch.setenv(gate.SKMEMORY_BIN_ENV, str(tmp_path / "nope" / "skmemory"))
    monkeypatch.setattr(gate, "_enabled", lambda: True)
    _owner_dm()
    token = set_current_write_origin(BACKGROUND_REVIEW)
    try:
        refusal, verdict = gate.review("create", "probe", ["mem-owner-1"], QUOTE)
    finally:
        reset_current_write_origin(token)
    assert verdict is None and refusal["rejected_uncited"]
    assert "skmemory CLI not found" in refusal["error"]


def test_missing_cli_at_store_time_is_logged_not_silent(cli, monkeypatch, tmp_path, caplog):
    gate = cli["gate"]
    _owner_dm()
    v = gate.check(["mem-owner-1"], QUOTE, gate.memory_resolver())
    monkeypatch.setenv(gate.SKMEMORY_BIN_ENV, str(tmp_path / "gone" / "skmemory"))
    with caplog.at_level("ERROR", logger="tools.skill_manager_tool"):
        gate.record_lesson("create", "probe", v, QUOTE, "text")
    assert any("not stored" in r.getMessage() and "skmemory CLI not found" in r.getMessage()
               for r in caplog.records)


def test_gate_module_never_imports_skmemory():
    import tools.skill_citation_gate as gate

    src = open(gate.__file__).read()
    assert "import skmemory" not in src and "from skmemory" not in src
    assert not any(m == "skmemory" or m.startswith("skmemory.") for m in sys.modules) or os.environ.get(
        "SKMEMORY_IN_HERMES_VENV"
    )
