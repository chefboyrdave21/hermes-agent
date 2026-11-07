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
    monkeypatch.setattr(sc, "_session_context_engaged", False)
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


def test_delegated_child_inherits_scope_and_context_less_thread_is_non_owner():
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
    assert turn_context._memory_turn_start_and_prefetch(ag, "tell me things") == ""
    assert not mm.prefetch_all.called


def test_yolo_never_active_for_non_owner():
    from tools import approval
    with patch.object(approval, "is_current_session_yolo_enabled", return_value=True):
        _group(); assert approval._yolo_active() is False
        _chef_dm(); assert approval._yolo_active() is True
