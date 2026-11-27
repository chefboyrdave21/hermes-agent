"""tools.tool_search.never_defer: operator-listed tools or toolsets stay on the direct surface.

A platform plugin's guest toolsets must stay eager because a guest turn may not run
tool_search / tool_call. ``defer`` can only add tools to deferral, so this is the key
that keeps one out.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def _td(name: str) -> dict[str, Any]:
    return {"type": "function", "function": {
        "name": name, "description": f"desc for {name}",
        "parameters": {"type": "object", "properties": {}}}}


def _register(name: str, toolset: str) -> None:
    from tools.registry import registry
    registry.register(name=name, handler=lambda args, **kw: json.dumps({"ok": True}),
                      schema=_td(name)["function"], toolset=toolset)


def test_default_is_empty_and_registered_in_default_config():
    from hermes_cli.config_defaults import DEFAULT_CONFIG
    from tools.tool_search import ToolSearchConfig
    assert DEFAULT_CONFIG["tools"]["tool_search"]["never_defer"] == []
    assert ToolSearchConfig.from_raw(None).never_defer == frozenset()
    assert ToolSearchConfig.from_raw({"never_defer": [" a ", "", "b"]}).never_defer == {"a", "b"}


def test_scalar_is_warned_about_and_ignored(caplog):
    from tools.tool_search import ToolSearchConfig
    with caplog.at_level(logging.WARNING, logger="tools.tool_search"):
        cfg = ToolSearchConfig.from_raw({"never_defer": "nctalk-calls"})
    assert cfg.never_defer == frozenset()
    assert "never_defer" in caplog.text


def test_toolset_and_tool_name_entries_keep_plugin_tools_eager():
    from tools.tool_search import ToolSearchConfig, is_deferrable_tool_name
    _register("nd_probe_room_read", "nd-probe-room-read")
    _register("nd_probe_call_join", "nd-probe-calls")
    _register("nd_probe_other", "nd-probe-other")
    assert is_deferrable_tool_name("nd_probe_room_read")  # a plugin tool defers by default
    cfg = ToolSearchConfig.from_raw({"never_defer": ["nd-probe-room-read", "nd_probe_call_join"]})
    assert not cfg.is_deferrable("nd_probe_room_read")  # by toolset
    assert not cfg.is_deferrable("nd_probe_call_join")  # by tool name
    assert cfg.is_deferrable("nd_probe_other")  # unlisted plugin tool still defers


def test_never_defer_wins_over_defer():
    from tools.tool_search import ToolSearchConfig
    cfg = ToolSearchConfig.from_raw({"defer": ["todo_list"], "never_defer": ["todo_list"]})
    assert not cfg.is_deferrable("todo_list")


def test_assembly_keeps_listed_toolset_direct_and_out_of_bridge_scope():
    from tools.tool_search import BRIDGE_TOOL_NAMES, ToolSearchConfig, assemble_tool_defs
    from tools.registry import discover_builtin_tools
    discover_builtin_tools()
    kept = [f"nd_asm_kept_{i}" for i in range(3)]
    deferred = [f"nd_asm_deferred_{i}" for i in range(12)]
    for n in kept:
        _register(n, "nd-asm-guest")
    for n in deferred:
        _register(n, "nd-asm-other")
    assembled = assemble_tool_defs(
        [_td("terminal"), *(_td(n) for n in kept + deferred)],
        context_length=200_000,
        config=ToolSearchConfig.from_raw({"enabled": "on", "never_defer": ["nd-asm-guest"]}))
    names = {td["function"]["name"] for td in assembled.tool_defs}
    assert assembled.activated
    assert BRIDGE_TOOL_NAMES <= names
    assert set(kept) <= names
    assert not (set(deferred) & names)
