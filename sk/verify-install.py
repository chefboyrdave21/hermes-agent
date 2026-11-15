"""Refuse a hermes-agent export whose dependency env lacks a selected extra.

Run by sk/deploy-setup.sh after ``pm.cli install``, from the export root, with the
pm bootstrap python. It re-runs itself inside the export's committed venv and checks
that every distribution the selected extras declare in pyproject.toml is installed
there, and that a config.yaml with ``mcp_servers`` has the ``mcp`` extra selected.

Why: pm installs no optional extra unless asked. v0.23.1-sk.2 was built with only
``--extra telegram``, so the ``mcp`` SDK was missing and the gateway started with 0
of its MCP servers, silently (discovery logs that at debug level).
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

_SELF = "hermes-agent"


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _req_name(req: str) -> tuple[str, list[str]]:
    """``"foo[a,b]==1.2; marker"`` gives ``("foo", ["a", "b"])``."""
    m = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[([^\]]*)\])?", req)
    if not m:
        raise SystemExit(f"verify-install: cannot parse requirement {req!r}")
    extras = [e.strip() for e in (m.group(2) or "").split(",") if e.strip()]
    return _norm(m.group(1)), extras


def _optional_deps(pyproject: Path) -> dict[str, list[str]]:
    import tomllib

    return tomllib.loads(pyproject.read_text())["project"].get("optional-dependencies", {})


def declared_extras(pyproject: Path) -> set[str]:
    return set(_optional_deps(pyproject))


def required_dists(pyproject: Path, extras: list[str]) -> list[str]:
    """Distribution names the given extras pull in (self-references expanded)."""
    deps = _optional_deps(pyproject)
    seen: set[str] = set()
    out: set[str] = set()
    todo = list(extras)
    while todo:
        extra = todo.pop()
        if extra in seen:
            continue
        seen.add(extra)
        if extra not in deps:
            raise SystemExit(f"verify-install: extra {extra!r} is not declared in pyproject.toml")
        for req in deps[extra]:
            name, sub = _req_name(req)
            if name == _SELF:
                todo.extend(sub)
            else:
                out.add(name)
    return sorted(out)


def missing_dists(required: list[str], installed: set[str]) -> list[str]:
    have = {_norm(n) for n in installed}
    return [r for r in required if r not in have]


def config_problems(config: Path, extras: list[str]) -> list[str]:
    if not config.is_file():
        return []
    from ruamel.yaml import YAML  # the YAML library hermes-agent itself depends on

    data = YAML(typ="safe").load(config.read_text()) or {}
    servers = data.get("mcp_servers") or {}
    enabled = [n for n, c in servers.items() if not (isinstance(c, dict) and c.get("enabled") is False)]
    if enabled and "mcp" not in extras:
        return [f"config.yaml configures {len(enabled)} MCP server(s) but the 'mcp' extra is not selected"]
    return []


def _installed() -> set[str]:
    import importlib.metadata as md

    return {d.metadata["Name"] for d in md.distributions() if d.metadata["Name"]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--extras", required=True, help="space-separated extras, as passed to pm.cli install")
    ap.add_argument("--root", default=".")
    ap.add_argument("--config", default=None)
    ap.add_argument("--in-venv", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    root = Path(args.root).resolve()
    extras = args.extras.split()
    config = Path(args.config) if args.config else Path(
        os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))) / "config.yaml"

    if not args.in_venv:
        sys.path.insert(0, str(root))
        from pm.environments import project_python

        py = project_python(root)
        if not py.exists():
            print(f"verify-install: no committed venv python for {root}", file=sys.stderr)
            return 1
        cmd = [str(py), "-I", str(Path(__file__).resolve()), "--in-venv", "--extras", args.extras,
               "--root", str(root), "--config", str(config)]
        os.execv(cmd[0], cmd)

    problems = [f"extra dependency not installed: {d}"
                for d in missing_dists(required_dists(root / "pyproject.toml", extras), _installed())]
    problems += config_problems(config, extras)
    for p in problems:
        print(f"verify-install: {p}", file=sys.stderr)
    if problems:
        return 1
    print(f"verify-install: ok ({' '.join(extras)}; {sys.executable})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
