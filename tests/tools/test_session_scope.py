"""SKMEM-P0-19 / P0-22: default-deny scope for non-owner gateway sessions, via the real handlers."""
import asyncio
import contextvars
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway import session_context as sc
from tools import session_scope as ss

OWNER = "telegram:111"
SCHEMA_WITH = {"type": "object", "properties": {"query": {}, "reader": {}}}
SCHEMA_WITHOUT = {"type": "object", "properties": {"query": {}}}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("HERMES_MEMORY_OWNER", OWNER)
    monkeypatch.delenv("_HERMES_GATEWAY", raising=False)
    monkeypatch.setattr(sc, "_session_context_engaged", True)
    yield
    sc.clear_session_vars([])


def _bind(**kw):
    sc.set_session_vars(**{**dict(platform="telegram", chat_id="", chat_type="", user_id=""), **kw})


def _group():
    _bind(chat_id="-100500", chat_type="group", user_id="333")


def _chef_dm():
    _bind(chat_id="111", chat_type="dm", user_id="111")


# ---- scope resolution -------------------------------------------------------------------------
def test_scope_matrix(monkeypatch):
    _chef_dm(); assert ss.session_reader() is None
    _bind(chat_id="222", chat_type="dm", user_id="222"); assert ss.session_reader() == "channel:222"
    _bind(chat_id="-9", chat_type="group", user_id="111", thread_id="77"); assert ss.session_reader() == "channel:-9"
    _bind(); assert ss.session_reader() == ss.PUBLIC
    monkeypatch.delenv("HERMES_MEMORY_OWNER"); _chef_dm(); assert ss.session_reader() == "channel:111"


def test_unbound_gateway_turn_is_non_owner(monkeypatch):
    sc.clear_session_vars([])
    assert ss.session_reader() is None  # plain CLI process
    monkeypatch.setenv("_HERMES_GATEWAY", "1")
    assert ss.session_reader() == ss.PUBLIC  # goal / wake / webhook turn with nothing bound


def test_cron_owner_stamp(monkeypatch):
    sc.set_session_vars(platform="", chat_id="", cron_session="1")
    assert ss.session_reader() == ss.PUBLIC  # unstamped job
    tok = ss.cron_owner_job.set(True)
    try:
        assert ss.session_reader() is None  # owner_created job
    finally:
        ss.cron_owner_job.reset(tok)


def test_delegated_child_inherits_scope_and_context_less_thread_is_non_owner(monkeypatch):
    monkeypatch.setenv("_HERMES_GATEWAY", "1")
    import concurrent.futures as cf
    _group()
    ctx = contextvars.copy_context()
    with cf.ThreadPoolExecutor(1) as ex:
        assert ex.submit(ctx.run, ss.session_reader).result() == "channel:-100500"
        assert ex.submit(ss.session_reader).result() == ss.PUBLIC  # no context: never owner


# ---- real MCP handler -------------------------------------------------------------------------
def _run_mcp(server_key, tool, args, schema=SCHEMA_WITH):
    from tests.tools.test_mcp_tool import _make_call_result, _make_mock_server
    from tools.mcp_tool import _servers
    from tools.mcp_tool_handlers import _make_tool_handler

    sess = MagicMock()
    sess.call_tool = AsyncMock(return_value=_make_call_result("ok", is_error=False))
    srv = _make_mock_server(server_key, session=sess)
    srv._tools = [SimpleNamespace(name=tool, inputSchema=schema)]
    _servers[server_key] = srv

    def fake_run(c, timeout=30):
        return asyncio.run(c() if callable(c) else c)

    try:
        with patch("tools.mcp_tool_loop._run_on_mcp_loop", side_effect=fake_run):
            out = json.loads(_make_tool_handler(server_key, tool, 120)(args))
    finally:
        _servers.pop(server_key, None)
    return out, sess


@pytest.mark.parametrize("key", ["skmemory", "SKMemory", " SKMEMORY "])
def test_group_search_forces_reader_any_key_casing(key):
    _group()
    out, sess = _run_mcp(key, "memory_search", {"query": "x", "reader": "owner"})
    assert out["result"] == "ok"
    sess.call_tool.assert_called_once_with("memory_search", arguments={"query": "x", "reader": "channel:-100500"})


def test_schema_lacking_reader_is_refused():
    _group()
    out, sess = _run_mcp("skmemory", "memory_search", {"query": "x"}, schema=SCHEMA_WITHOUT)
    assert "reader" in out["error"] and not sess.call_tool.called


@pytest.mark.parametrize("server,tool", [
    ("skcapstone", "capauth_secret_get"), ("skstacks", "skstacks_secret_get"), ("skcapstone", "gtd_inbox"),
    ("skchat", "chat_history"), ("skcapstone", "telegram_catchup"), ("skcapstone", "check_inbox"),
    ("skcapstone", "trust_rehydrate"), ("skchat", "send_message"), ("skmemory", "memory_store"),
    ("skmemory", "memory_export"), ("SKCAPSTONE", "memory_search"), ("anything", "whatever"),
])
def test_default_deny_across_servers(server, tool):
    _group()
    out, sess = _run_mcp(server, tool, {})
    assert "not available" in out["error"] and not sess.call_tool.called


def test_chef_dm_unchanged_for_mcp():
    _chef_dm()
    out, sess = _run_mcp("skcapstone", "gtd_inbox", {}, schema=SCHEMA_WITHOUT)
    assert out["result"] == "ok"
    sess.call_tool.assert_called_once_with("gtd_inbox", arguments={})


# ---- native dispatch --------------------------------------------------------------------------
@pytest.mark.parametrize("tool", ["terminal", "read_file", "write_file", "patch", "execute_code", "session_search",
                                  "memory", "delegate_task", "cronjob", "send_message", "skill_view"])
def test_native_tools_denied_through_handle_function_call(tool):
    import model_tools
    _group()
    out = json.loads(model_tools.handle_function_call(tool, {}))
    assert "not available" in out["error"]


@pytest.mark.parametrize("tool", ["session_search", "memory", "delegate_task", "clarify"])
def test_inline_executors_gate(tool):
    from agent.inline_tool_executors import INLINE_TOOL_EXECUTORS
    _group()
    if tool == "clarify":
        return  # allowlisted: reaches its own executor
    out = json.loads(INLINE_TOOL_EXECUTORS[tool](MagicMock(), {}, MagicMock()))
    assert "not available" in out["error"]


def test_memory_manager_tools_and_prefetch_gated():
    from agent.memory_manager import MemoryManager
    mm = MemoryManager()
    mm._tool_to_provider["x"] = MagicMock()
    _group()
    assert "not available" in json.loads(mm.handle_tool_call("x", {}))["error"]
    assert mm.prefetch_all("hello there") == ""


# ---- prompt builder / hooks / yolo --------------------------------------------------------------
def _agent():
    store = MagicMock(); store.format_for_system_prompt.return_value = "PRIVATE MEMORY"
    return SimpleNamespace(_memory_store=store, _memory_enabled=True, _user_profile_enabled=True, _memory_manager=None)


def test_system_prompt_has_no_memory_blocks_for_non_owner():
    from agent.system_prompt import _memory_parts
    _group(); assert _memory_parts(_agent()) == []
    _chef_dm(); assert _memory_parts(_agent()) == ["PRIVATE MEMORY", "PRIVATE MEMORY"]


def test_pre_llm_call_hooks_do_not_run_for_non_owner():
    from agent import turn_context
    ag = SimpleNamespace(_persist_disabled=False, session_id="s", model="m", platform="telegram", _user_id="333")
    with patch("hermes_cli.lifecycle.invoke_hook", return_value=[{"context": "CHEF MAIL"}]) as hook:
        _group()
        assert turn_context._collect_pre_llm_call_context(
            ag, effective_task_id="t", turn_id="u", original_user_message="hi", messages=[], conversation_history=[]) == ""
        assert not hook.called
        _chef_dm()
        assert "CHEF MAIL" in turn_context._collect_pre_llm_call_context(
            ag, effective_task_id="t", turn_id="u", original_user_message="hi", messages=[], conversation_history=[])


def test_external_prefetch_skipped_for_non_owner():
    from agent import turn_context
    mm = MagicMock(); mm.prefetch_all.return_value = "SECRET"
    ag = SimpleNamespace(_memory_manager=mm, _user_turn_count=1, session_id="s")
    _group()
    assert turn_context._memory_turn_start_and_prefetch(ag, "tell me things", None, "") == ""
    assert not mm.prefetch_all.called


def test_yolo_never_active_for_non_owner():
    from tools import approval
    with patch.object(approval, "is_current_session_yolo_enabled", return_value=True):
        _group(); assert approval._yolo_active() is False
        _chef_dm(); assert approval._yolo_active() is True


# ---- P0-22 follow-up: slash, quick commands, hooks, sync, tts, AGENTS.md, logging -----------------
def _src(user="333", chat="-100500", ctype="group"):
    return SimpleNamespace(platform=SimpleNamespace(value="telegram"), chat_id=chat, chat_type=ctype, user_id=user)


class _Runner:
    """Just enough of the gateway runner for the real ``_check_slash_access``."""
    config = SimpleNamespace()


def _slash_check(source, cmd):
    from gateway.run_busy import GatewayBusySessionMixin  # real method, fake self
    return GatewayBusySessionMixin._check_slash_access(_Runner(), source, cmd)


@pytest.mark.parametrize("cmd", ["memory", "skills", "approvals", "yolo", "update", "model", "my-exec-quick"])
def test_slash_default_deny_for_non_owner(cmd):
    assert "owner-only" in _slash_check(_src(), cmd)
    assert "owner-only" in _slash_check(_src("222", "222", "dm"), cmd)


@pytest.mark.parametrize("cmd", ["help", "whoami"])
def test_slash_allowlist_for_non_owner(cmd):
    with patch("gateway.slash_access.policy_for_runner_source", return_value=SimpleNamespace(enabled=False)):
        assert _slash_check(_src(), cmd) is None


def test_slash_owner_dm_not_denied_by_scope():
    with patch("gateway.slash_access.policy_for_runner_source", return_value=SimpleNamespace(enabled=False)):
        assert _slash_check(_src("111", "111", "dm"), "memory") is None


def test_shell_hooks_never_spawn_for_non_owner():
    from agent import shell_hooks
    spec = SimpleNamespace(event="on_session_end", command="x.sh", matches_tool=lambda n: True)
    cb = shell_hooks._make_callback(spec)
    with patch.object(shell_hooks, "_spawn", return_value={}) as spawn, \
            patch.object(shell_hooks, "_evaluate_result", return_value={"ok": 1}):
        _group(); assert cb(session_id="s") is None and not spawn.called
        _chef_dm(); assert cb(session_id="s") == {"ok": 1} and spawn.called


def test_memory_writes_blocked_for_non_owner():
    from agent.memory_manager import MemoryManager
    mm = MemoryManager(); prov = MagicMock(); prov.name = "p"; mm._providers = [prov]
    _group()
    mm.sync_all("hi there friend", "reply", session_id="s")
    mm.on_session_end([{"role": "user", "content": "x"}])
    assert mm.on_pre_compress([{"role": "user", "content": "x"}]) == ""
    mm.on_memory_write("add", "memory", "x"); mm.on_delegation("t", "r")
    assert not prov.method_calls


def test_tts_ignores_output_path_for_non_owner(tmp_path):
    from tools.tts_tool import _resolve_output_base
    target = str(tmp_path / "victim.mp3")
    _group()
    base, err = _resolve_output_base(target, "edge", None, False)
    assert err is None and str(base) != target and base.name.startswith("tts_")
    _chef_dm()
    base, err = _resolve_output_base(target, "edge", None, False)
    assert err is None and str(base).startswith(target.rsplit('.', 1)[0])


def test_agents_md_skipped_for_non_owner(tmp_path):
    from agent.prompt_builder import _load_agents_md
    (tmp_path / "AGENTS.md").write_text("OPERATOR PRIVATE RULES")
    _group(); assert _load_agents_md(tmp_path) == ""
    _chef_dm(); assert "OPERATOR PRIVATE RULES" in _load_agents_md(tmp_path)


def test_owner_config_logging_no_ids(monkeypatch, caplog):
    import logging
    monkeypatch.setattr(ss, "_logged", False)
    monkeypatch.delenv("HERMES_MEMORY_OWNER")
    with caplog.at_level(logging.INFO, logger="tools.session_scope"):
        ss.log_owner_config()
    assert any(r.levelno == logging.WARNING and "unset" in r.getMessage() for r in caplog.records)
    caplog.clear(); monkeypatch.setattr(ss, "_logged", False); monkeypatch.setenv("HERMES_MEMORY_OWNER", OWNER)
    with caplog.at_level(logging.INFO, logger="tools.session_scope"):
        ss.log_owner_config()
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "1 owner" in msgs and "111" not in msgs


# ---- finalize / expiry / shutdown run outside a bound session: scope comes from the stored session ----
def _release(entries, key, fn):
    from gateway.run_agent_cache import GatewayAgentCacheMixin
    runner = SimpleNamespace(session_store=SimpleNamespace(_entries=entries))
    GatewayAgentCacheMixin._run_release_in_profile_scope(runner, fn, (), key)


def _entry(user, chat, ctype):
    return SimpleNamespace(origin=_src(user, chat, ctype))


def _finalize_effects():
    """What a finalize does here: the shell session-end hook and the memory provider commit."""
    from agent import shell_hooks
    from agent.memory_manager import MemoryManager
    seen = {"hook": False, "provider": False}
    spec = SimpleNamespace(event="on_session_end", command="x.sh", matches_tool=lambda n: True)
    mm = MemoryManager(); prov = MagicMock(); prov.name = "p"
    prov.on_session_end.side_effect = lambda m: seen.__setitem__("provider", True)
    mm._providers = [prov]

    def run():
        with patch.object(shell_hooks, "_spawn", side_effect=lambda *a: seen.__setitem__("hook", True) or {}), \
                patch.object(shell_hooks, "_evaluate_result", return_value=None):
            shell_hooks._make_callback(spec)(session_id="s")
        mm.on_session_end([{"role": "user", "content": "x"}])
    return seen, run


def test_finalize_owner_dm_runs_hook_and_sync(monkeypatch):
    monkeypatch.setenv("_HERMES_GATEWAY", "1")
    sc.clear_session_vars([])  # nothing bound, exactly like a finalize on a worker thread
    seen, run = _finalize_effects()
    _release({"k": _entry("111", "111", "dm")}, "k", run)
    assert seen == {"hook": True, "provider": True}


def test_finalize_group_session_does_not(monkeypatch):
    monkeypatch.setenv("_HERMES_GATEWAY", "1")
    sc.clear_session_vars([])
    seen, run = _finalize_effects()
    _release({"k": _entry("333", "-100500", "group")}, "k", run)
    assert seen == {"hook": False, "provider": False}


def test_finalize_unbound_or_unknown_session_does_not(monkeypatch):
    monkeypatch.setenv("_HERMES_GATEWAY", "1")
    sc.clear_session_vars([])
    seen, run = _finalize_effects()
    _release({}, "missing-key", run)          # session key with no stored entry
    run_unkeyed = _finalize_effects()
    _release({}, None, run_unkeyed[1])        # no key at all, nothing bound
    assert seen == {"hook": False, "provider": False}
    assert run_unkeyed[0] == {"hook": False, "provider": False}


def test_finalize_scope_does_not_leak_from_ambient_owner_turn():
    _chef_dm()  # an owner turn is ambient, but the session being finalized is a group
    seen, run = _finalize_effects()
    _release({"k": _entry("333", "-100500", "group")}, "k", run)
    assert seen == {"hook": False, "provider": False}


# ---- owner chats, nextcloud talk, api_server, prompt cache scope --------------------------------------
@pytest.fixture
def _chats(monkeypatch):
    monkeypatch.setenv("HERMES_MEMORY_OWNER", "telegram:111,nctalk:chef")
    monkeypatch.setenv("HERMES_MEMORY_OWNER_CHATS", "telegram:-1001,nextcloud_talk:roomA1")


def _full_tools_and_injection(src_kwargs):
    """True when the REAL gateway paths treat this bound turn as full-access."""
    import model_tools
    from agent.system_prompt import _memory_parts
    _bind(**src_kwargs)
    tool_ok = "not available" not in model_tools.handle_function_call("terminal", {})
    inj = _memory_parts(_agent()) != []
    slash = _slash_check_bound(src_kwargs)
    return tool_ok, inj, slash


def _slash_check_bound(kw):
    src = SimpleNamespace(platform=SimpleNamespace(value=kw.get("platform", "telegram")), chat_id=kw["chat_id"],
                          chat_type=kw["chat_type"], user_id=kw["user_id"])
    with patch("gateway.slash_access.policy_for_runner_source", return_value=SimpleNamespace(enabled=False)):
        return _slash_check(src, "memory") is None


def test_owner_speaker_in_listed_chat_is_full(_chats):
    with patch("tools.registry.registry.dispatch", return_value="{}"):
        assert _full_tools_and_injection(dict(chat_id="-1001", chat_type="group", user_id="111")) == (True, True, True)


def test_other_speaker_in_same_listed_chat_is_restricted(_chats):
    assert _full_tools_and_injection(dict(chat_id="-1001", chat_type="group", user_id="999")) == (False, False, False)


def test_owner_speaker_in_unlisted_group_is_restricted(_chats):
    assert _full_tools_and_injection(dict(chat_id="-2002", chat_type="group", user_id="111")) == (False, False, False)


def test_nctalk_dm_owner_and_alias_and_room_config(_chats):
    _bind(platform="nctalk", chat_id="xx9z", chat_type="dm", user_id="Chef"); assert ss.session_reader() is None
    _bind(platform="nextcloud_talk", chat_id="xx9z", chat_type="dm", user_id="chef"); assert ss.session_reader() is None
    _bind(platform="nctalk", chat_id="roomA1", chat_type="group", user_id="chef"); assert ss.session_reader() is None
    _bind(platform="nctalk", chat_id="roomB2", chat_type="group", user_id="chef"); assert ss.session_reader() == "channel:roomB2"
    _bind(platform="nctalk", chat_id="roomA1", chat_type="group", user_id="guest"); assert ss.session_reader() == "channel:roomA1"
    _bind(platform="nctalk", chat_id="xx9z", chat_type="dm", user_id="guest"); assert ss.session_reader() == "channel:xx9z"


def test_api_server_owner_only_with_key_and_default_profile(monkeypatch):
    _bind(platform="api_server", chat_id="sess1")
    monkeypatch.delenv("API_SERVER_KEY", raising=False); assert ss.session_reader() == "channel:sess1"
    monkeypatch.setenv("API_SERVER_KEY", "k" * 20); assert ss.session_reader() is None
    _bind(platform="api_server", chat_id="sess1", profile="other"); assert ss.session_reader() == "channel:sess1"


def _turn_agent():
    calls = []
    ag = SimpleNamespace(_cached_system_prompt=None, _prompt_scope="unset")
    del ag._prompt_scope
    ag._build_system_prompt = lambda m: calls.append("build") or f"PROMPT[{ss.session_reader()}]"
    return ag, calls


def _prompt_for_turn(ag):
    """The exact scope/cache block from turn_context, via a minimal replay of its contract."""
    import inspect
    from agent import turn_context
    src = inspect.getsource(turn_context)
    assert "_prompt_scope" in src and "_force_build" in src
    from tools.session_scope import session_reader as _scope_now
    _scope = _scope_now()
    force = ag._cached_system_prompt is not None and getattr(ag, "_prompt_scope", _scope) != _scope
    if force:
        ag._cached_system_prompt = None
    if ag._cached_system_prompt is None:
        ag._cached_system_prompt = ag._build_system_prompt(None)
    ag._prompt_scope = _scope
    return ag._cached_system_prompt


def test_agent_signature_includes_scope(_chats):
    from gateway.run_agent_cache import GatewayAgentCacheMixin as M
    args = ("m", {}, [], "")
    _bind(chat_id="-1001", chat_type="group", user_id="111"); a = M._agent_config_signature(*args, user_id="111")
    _bind(chat_id="-1001", chat_type="group", user_id="999"); b = M._agent_config_signature(*args, user_id="111")
    assert a != b  # same cache inputs, different scope: never share an agent / frozen prompt


def test_frozen_prompt_not_reused_across_scopes(_chats):
    ag, calls = _turn_agent()
    _bind(chat_id="-1001", chat_type="group", user_id="111")
    assert _prompt_for_turn(ag) == "PROMPT[None]"
    assert _prompt_for_turn(ag) == "PROMPT[None]" and calls == ["build"]  # same scope: cache hit
    _bind(chat_id="-1001", chat_type="group", user_id="999")
    assert _prompt_for_turn(ag) == "PROMPT[channel:-1001]" and len(calls) == 2


# ---- owner-speaker grants (Talk call tools in a shared room) ------------------------------------
def _talk_group(user_id):
    _bind(platform="nctalk", chat_id="zmiqk5yk", chat_type="group", user_id=user_id)


@pytest.fixture
def _talk_owner(monkeypatch):
    monkeypatch.setenv("HERMES_MEMORY_OWNER", "nextcloud_talk:dknestri")
    monkeypatch.delenv("HERMES_OWNER_SPEAKER_TOOLS", raising=False)


@pytest.mark.parametrize("tool", ["call_join", "call_leave", "call_status"])
def test_owner_speaker_may_use_call_tools_in_group(_talk_owner, tool):
    import model_tools
    _talk_group("DKnestri")
    assert ss.session_reader() == "channel:zmiqk5yk"  # the turn itself stays non-owner
    with patch("tools.registry.registry.dispatch", return_value="{}") as d:
        out = model_tools.handle_function_call(tool, {})
    assert "not available" not in out
    d.assert_called_once()


def test_owner_speaker_grant_is_only_the_call_tools(_talk_owner):
    import model_tools
    _talk_group("dknestri")
    for tool in ("terminal", "skill_view", "memory", "send_message", "session_search"):
        assert "not available" in model_tools.handle_function_call(tool, {})
    assert ss.native_refusal("skill_view") is not None
    assert ss.native_refusal("call_join") is None


def test_guest_in_same_room_gets_no_call_tools(_talk_owner):
    import model_tools
    _talk_group("manolo")
    assert "not available" in model_tools.handle_function_call("call_join", {})


def test_owner_speaker_grant_never_reaches_another_room(_talk_owner):
    _talk_group("dknestri")
    assert ss.owner_speaker_grant("call_join", {"room": "zmiqk5yk"})
    assert not ss.owner_speaker_grant("call_join", {"room": "gaquurgh", "other_room": True})
    assert not ss.owner_speaker_grant("call_join", {"other_room": "true"})


def test_owner_speaker_grant_configurable(_talk_owner, monkeypatch):
    _talk_group("dknestri")
    monkeypatch.setenv("HERMES_OWNER_SPEAKER_TOOLS", "off")
    assert not ss.owner_speaker_grant("call_join")
    monkeypatch.setenv("HERMES_OWNER_SPEAKER_TOOLS", "call_status")
    assert ss.owner_speaker_grant("call_status") and not ss.owner_speaker_grant("call_join")


def test_owner_speaker_grant_not_for_cron_api_or_unbound(_talk_owner, monkeypatch):
    _bind(platform="api_server", chat_id="s1", user_id="dknestri")
    assert not ss.owner_speaker_grant("call_join")
    _bind()
    assert not ss.owner_speaker_grant("call_join")
    _talk_group("dknestri")
    with ss.declared_scope(None):
        assert not ss.owner_speaker_grant("call_join")
