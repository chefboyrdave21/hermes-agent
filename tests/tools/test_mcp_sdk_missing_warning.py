"""Configured MCP servers with no ``mcp`` SDK must be loud, not a debug line.

An install built without the ``mcp`` extra (pm installs no optional extra unless
named) has servers in config.yaml but cannot import the SDK. Discovery then returns
[] and the gateway runs with none of its MCP tools; at debug level nothing in the
default logs said why.
"""

import logging

from tools import mcp_tool_config as _config
from tools import mcp_tool_discovery as discovery


def _patch(monkeypatch, servers, sdk_ok):
    monkeypatch.setattr(_config, "_load_mcp_config", lambda: servers)
    monkeypatch.setattr("tools.mcp_tool._ensure_mcp_sdk", lambda: sdk_ok)
    monkeypatch.setattr("agent.safe_worker_policy.safe_worker_enabled", lambda: False)


def test_configured_servers_without_sdk_warn(monkeypatch, caplog):
    _patch(monkeypatch, {"a": {"command": "x"}, "b": {"command": "y"}}, sdk_ok=False)
    with caplog.at_level(logging.DEBUG):
        assert discovery.discover_mcp_tools() == []
    warns = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warns) == 1
    msg = warns[0].getMessage()
    assert "2 MCP server(s) configured" in msg and "mcp" in msg


def test_no_servers_stays_quiet(monkeypatch, caplog):
    _patch(monkeypatch, {}, sdk_ok=False)
    with caplog.at_level(logging.DEBUG):
        assert discovery.discover_mcp_tools() == []
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_filter_that_keeps_no_server_stays_quiet(monkeypatch, caplog):
    _patch(monkeypatch, {"a": {"command": "x"}}, sdk_ok=False)
    with caplog.at_level(logging.DEBUG):
        assert discovery.discover_mcp_tools(allowed_mcp_names=["web"]) == []
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
