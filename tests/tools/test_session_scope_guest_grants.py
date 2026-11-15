"""Plugin guest grants: a plugin tool registered with a guest guard (consent ``tools.guest``) runs in a
non-owner turn only when its guard approves that bound turn. Everything else stays default-deny."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from gateway import session_context as sc
from tools import session_scope as ss

ROOM = "u7o3gwt7"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("HERMES_MEMORY_OWNER", "nextcloud_talk:dknestri")
    monkeypatch.setenv("HERMES_MEMORY_OWNER_CHATS", f"nctalk:{ROOM}")
    monkeypatch.delenv("_HERMES_GATEWAY", raising=False)
    monkeypatch.setattr(sc, "_session_context_engaged", True)
    monkeypatch.setattr(ss, "_GUEST_GUARDS", {})
    yield
    sc.clear_session_vars([])


def _talk(user_id, chat_id=ROOM, thread_id=""):
    sc.set_session_vars(platform="nctalk", chat_id=chat_id, chat_type="group", user_id=user_id,
                        thread_id=thread_id)


def _room_guard(turn, args):
    return turn.platform == "nctalk" and turn.chat_id == ROOM and turn.user_id == "manolo" and not args.get("room")


def test_guest_tool_runs_when_its_guard_approves():
    import model_tools
    ss.register_guest_guard("talk_room_history", _room_guard)
    _talk("manolo")
    assert ss.session_reader() == f"channel:{ROOM}"  # the turn itself stays non-owner
    assert ss.native_refusal("talk_room_history") is None
    with patch("tools.registry.registry.dispatch", return_value="{}") as d:
        out = model_tools.handle_function_call("talk_room_history", {})
    assert "not available" not in out
    d.assert_called_once()


def test_guard_sees_the_bound_turn_and_args():
    seen = []
    ss.register_guest_guard("talk_room_history", lambda t, a: seen.append((t, a)) or True)
    _talk("manolo", thread_id="15652")
    assert ss.guest_grant("talk_room_history", {"limit": 5})
    turn, args = seen[0]
    assert (turn.platform, turn.chat_id, turn.chat_type, turn.user_id, turn.thread_id) == (
        "nctalk", ROOM, "group", "manolo", "15652")
    assert args == {"limit": 5}


def test_guard_refusal_and_args_are_honoured():
    import model_tools
    ss.register_guest_guard("talk_room_history", _room_guard)
    _talk("manolo")
    assert not ss.guest_grant("talk_room_history", {"room": "gaquurgh"})
    _talk("someone_else")
    assert "not available" in model_tools.handle_function_call("talk_room_history", {})
    _talk("manolo", chat_id="gaquurgh")
    assert ss.native_refusal("talk_room_history") is not None


def test_no_guard_no_grant_and_other_tools_stay_refused():
    import model_tools
    ss.register_guest_guard("talk_room_history", lambda t, a: True)
    _talk("manolo")
    for tool in ("terminal", "talk_thread_list", "memory", "send_message", "session_search", "skill_view"):
        assert "not available" in model_tools.handle_function_call(tool, {})


def test_guard_that_raises_or_returns_truthy_non_bool_is_refused():
    ss.register_guest_guard("a", lambda t, a: 1 / 0)
    ss.register_guest_guard("b", lambda t, a: "yes")
    _talk("manolo")
    assert not ss.guest_grant("a") and not ss.guest_grant("b")


def test_never_for_cron_api_local_unbound_or_declared_scope(monkeypatch):
    ss.register_guest_guard("talk_room_history", lambda t, a: True)
    sc.set_session_vars(platform="nctalk", chat_id=ROOM, cron_session="1")
    assert not ss.guest_grant("talk_room_history")
    sc.clear_session_vars([])
    sc.set_session_vars(platform="api_server", chat_id="s1", user_id="x")
    assert not ss.guest_grant("talk_room_history")
    sc.set_session_vars(platform="local", chat_id="x")
    assert not ss.guest_grant("talk_room_history")
    sc.set_session_vars(platform="", chat_id="")
    assert not ss.guest_grant("talk_room_history")
    _talk("manolo")
    with ss.declared_scope(None):
        assert not ss.guest_grant("talk_room_history")


def test_unregister_only_removes_the_same_guard():
    g1, g2 = (lambda t, a: True), (lambda t, a: True)
    ss.register_guest_guard("x", g1)
    ss.unregister_guest_guard("x", g2)
    assert ss._GUEST_GUARDS.get("x") is g1
    ss.unregister_guest_guard("x", g1)
    assert "x" not in ss._GUEST_GUARDS


def test_inline_executor_path_passes_args_to_the_guard():
    from agent import inline_tool_executors as ite
    ss.register_guest_guard("clarify_like", _room_guard)
    _talk("manolo")
    ran = []
    wrapped = ite._coerced("clarify_like", lambda agent, args, ctx: ran.append(args) or "ok")
    assert wrapped(None, {}, None) == "ok" and ran == [{}]
    assert "not available" in wrapped(None, {"room": "gaquurgh"}, None)


# ---- plugin context: consent gate -------------------------------------------------------------------
def _ctx(granted, monkeypatch):
    from hermes_cli.plugins import PluginContext
    ctx = PluginContext.__new__(PluginContext)
    ctx.manifest = SimpleNamespace(name="nctalk", source="user")
    ctx._manager = SimpleNamespace(scope_key=None, home_path=None, _plugin_tool_names=set(),
                                   _track_scoped_registration=lambda *a, **k: "handle",
                                   _remove_tool_name_if_unowned=lambda n: None)
    monkeypatch.setattr(PluginContext, "plugin_id", property(lambda self: "nextcloud-talk-platform"), raising=False)
    return ctx


@pytest.mark.parametrize("granted", [True, False])
def test_register_tool_guest_guard_needs_consent(granted, monkeypatch):
    from tools.registry import registry
    ctx = _ctx(granted, monkeypatch)
    monkeypatch.setattr(type(ctx), "_guest_tools_allowed", lambda self, name: granted)
    name = f"guest_probe_{granted}"
    try:
        ctx.register_tool(name=name, toolset="probe", schema={"name": name, "parameters": {}},
                          handler=lambda a, **k: "{}", guest_guard=lambda t, a: True)
        assert (name in ss._GUEST_GUARDS) is granted
    finally:
        ss.unregister_guest_guard(name)
        registry.deregister(name)


def test_capability_is_declared():
    from hermes_cli.plugin_capabilities import CAPABILITY_REGISTRY
    assert CAPABILITY_REGISTRY["tools.guest"].legacy_path == ("allow_guest_tools",)
