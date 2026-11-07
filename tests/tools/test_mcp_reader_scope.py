"""SKMEM-P0-19: the gateway forces the skmemory reader per chat; the model cannot widen it."""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway import session_context as sc
from tools.mcp_reader_scope import scope_tool_args, session_reader

OWNER = "telegram:111"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("HERMES_MEMORY_OWNER", OWNER)
    monkeypatch.delenv("HERMES_MEMORY_READER_SERVERS", raising=False)
    monkeypatch.setattr(sc, "_session_context_engaged", True)
    yield
    sc.clear_session_vars([])


def _bind(**kw):
    base = dict(platform="telegram", chat_id="", chat_type="", user_id="")
    sc.set_session_vars(**{**base, **kw})


def test_chef_dm_keeps_owner():
    _bind(chat_id="111", chat_type="dm", user_id="111")
    assert session_reader() is None
    args, err = scope_tool_args("skmemory", "memory_search", {"query": "x"})
    assert args == {"query": "x"} and err is None


def test_dm_from_non_chef_reads_as_channel():
    _bind(chat_id="222", chat_type="dm", user_id="222")
    assert session_reader() == "channel:222"


def test_group_with_chef_in_it_is_still_channel():
    _bind(chat_id="-100500", chat_type="group", user_id="111")
    assert session_reader() == "channel:-100500"


def test_unknown_session_reads_public():
    _bind(platform="telegram", chat_id="", chat_type="", user_id="")
    assert session_reader() == "@public"


def test_unset_owner_env_fails_closed(monkeypatch):
    monkeypatch.delenv("HERMES_MEMORY_OWNER")
    _bind(chat_id="111", chat_type="dm", user_id="111")
    assert session_reader() == "channel:111"


@pytest.mark.parametrize("claimed", ["owner", "@chef", "@inner", "channel:111", None])
def test_model_supplied_reader_is_overwritten_in_group(claimed):
    _bind(chat_id="-100500", chat_type="group", user_id="333")
    given = {"query": "x"} if claimed is None else {"query": "x", "reader": claimed}
    args, err = scope_tool_args("skmemory", "memory_search", given)
    assert err is None and args["reader"] == "channel:-100500"
    assert "reader" not in {"query": "x"}  # caller's dict untouched
    assert given.get("reader") == claimed or claimed is None


def test_cli_without_gateway_session_is_owner(monkeypatch):
    monkeypatch.setattr(sc, "_session_context_engaged", False)
    assert session_reader() is None


def test_skcapstone_memory_tools_refused_in_group_only():
    _bind(chat_id="-100500", chat_type="group", user_id="333")
    assert scope_tool_args("skcapstone", "memory_search", {})[1]
    assert scope_tool_args("skcapstone", "ritual", {})[1]
    assert scope_tool_args("skcapstone", "coord_status", {})[1] is None
    _bind(chat_id="111", chat_type="dm", user_id="111")
    assert scope_tool_args("skcapstone", "memory_search", {})[1] is None


def test_other_servers_untouched():
    _bind(chat_id="-1", chat_type="group", user_id="3")
    args = {"a": 1}
    assert scope_tool_args("github", "memory_search", args) == (args, None)


def test_handler_sends_forced_reader_to_server():
    """End to end through _make_tool_handler with a fake session."""
    import asyncio
    from tests.tools.test_mcp_tool import _make_call_result, _make_mock_server
    from tools.mcp_tool import _servers
    from tools.mcp_tool_handlers import _make_tool_handler

    sess = MagicMock()
    sess.call_tool = AsyncMock(return_value=_make_call_result("ok", is_error=False))
    _servers["skmemory"] = _make_mock_server("skmemory", session=sess)
    _bind(chat_id="-100500", chat_type="group", user_id="333")

    def fake_run(c, timeout=30):
        return asyncio.run(c() if callable(c) else c)

    try:
        h = _make_tool_handler("skmemory", "memory_search", 120)
        with patch("tools.mcp_tool_loop._run_on_mcp_loop", side_effect=fake_run):
            out = json.loads(h({"query": "x", "reader": "owner"}))
        assert out["result"] == "ok"
        sess.call_tool.assert_called_once_with(
            "memory_search", arguments={"query": "x", "reader": "channel:-100500"})
    finally:
        _servers.pop("skmemory", None)
