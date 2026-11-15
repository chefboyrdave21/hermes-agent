"""sk-deploy setup must build the dependency extras the gateway needs.

v0.23.1-sk.2 shipped an export built with ``--extra telegram`` only. pm installs
no other extra by default, so the ``mcp`` SDK was absent, ``discover_mcp_tools``
returned [] at debug level, and the gateway came up with 0 of its 8 MCP servers.
These tests pin the extras list and the post-install verification that would
have refused that export.
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SETUP = ROOT / "sk" / "deploy-setup.sh"


def _load_verify():
    spec = importlib.util.spec_from_file_location("sk_verify_install", ROOT / "sk" / "verify-install.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _default_extras() -> list[str]:
    m = re.search(r'HERMES_SK_EXTRAS:-([^}]*)\}', SETUP.read_text())
    assert m, "deploy-setup.sh must define a HERMES_SK_EXTRAS default"
    return m.group(1).split()


def test_default_extras_cover_mcp_acp_and_telegram():
    extras = _default_extras()
    for needed in ("telegram", "mcp", "acp"):
        assert needed in extras, f"{needed} missing from the default sk-deploy extras"


def test_default_extras_are_declared_in_pyproject():
    verify = _load_verify()
    declared = verify.declared_extras(ROOT / "pyproject.toml")
    unknown = [e for e in _default_extras() if e not in declared]
    assert not unknown, f"extras not declared in pyproject: {unknown}"


def test_setup_runs_the_install_verification():
    text = SETUP.read_text()
    assert "verify-install.py" in text
    # verification has to come after the dependency install, before the stamp
    assert text.index('pm.cli install "${extra_args[@]}"') < text.index("-I sk/verify-install.py") < text.index("scripts/write_install_stamp.py")


def test_missing_requirement_is_reported():
    verify = _load_verify()
    required = verify.required_dists(ROOT / "pyproject.toml", ["mcp"])
    assert "mcp" in required
    missing = verify.missing_dists(required, installed={"httpx2", "starlette"})
    assert missing == ["mcp"]


def test_complete_install_passes():
    verify = _load_verify()
    required = verify.required_dists(ROOT / "pyproject.toml", ["mcp", "telegram"])
    assert verify.missing_dists(required, installed=set(required)) == []


def test_config_with_mcp_servers_requires_the_mcp_extra(tmp_path):
    verify = _load_verify()
    cfg = tmp_path / "config.yaml"
    cfg.write_text("mcp_servers:\n  skmemory:\n    command: /bin/true\n")
    assert verify.config_problems(cfg, ["telegram"]) == [
        "config.yaml configures 1 MCP server(s) but the 'mcp' extra is not selected"
    ]
    assert verify.config_problems(cfg, ["telegram", "mcp"]) == []


def test_config_without_mcp_servers_needs_nothing(tmp_path):
    verify = _load_verify()
    cfg = tmp_path / "config.yaml"
    cfg.write_text("model:\n  default: x\n")
    assert verify.config_problems(cfg, ["telegram"]) == []
    assert verify.config_problems(tmp_path / "absent.yaml", ["telegram"]) == []


def test_unknown_extra_is_an_error():
    verify = _load_verify()
    with pytest.raises(SystemExit):
        verify.required_dists(ROOT / "pyproject.toml", ["no-such-extra"])
