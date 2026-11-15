#!/usr/bin/env bash
# sk-deploy tool `setup` for a hermes-agent export (SK fork only, not upstream).
#
# Runs inside ~/.local/share/sk-tools/hermes-agent/<sha12> before the export is
# published. A tool export has no .git, so this makes it a self-contained install:
#   1. pm tools closure (node, pnpm, python, ... pinned by pm/lock.json)
#   2. pm dependency env with the extras in HERMES_SK_EXTRAS, keyed to this export's
#      path, which also mints .hermes/bin/hermes and hermes-acp for this tree. pm
#      installs NO optional extra unless named: sk.2 named only telegram and lost the
#      mcp SDK, so the gateway started with 0 MCP servers. The default below is the
#      set the pre-sk-deploy install had (telegram mcp acp ddgs google youtube).
#   2b. sk/verify-install.py: every selected extra is installed in the built venv,
#      and a config.yaml with mcp_servers has the mcp extra; otherwise refuse
#   3. an install stamp with updateMechanism=external: the version reads from the
#      stamp (not from ~/.hermes/hermes-agent, the git fallback) and `hermes update`
#      leaves the tree alone; releases arrive only through sk-deploy
set -euo pipefail
export HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
here="$(pwd -P)"
sha12="$(basename "$here")"
mirror="${SK_TOOL_MIRROR:-$HOME/.local/state/sk-deploy/tool-src/hermes-agent.git}"
sha="$(git --git-dir="$mirror" rev-parse --verify --quiet "${sha12}^{commit}")"
tag="$(git --git-dir="$mirror" describe --tags --exact-match "$sha" 2>/dev/null || true)"
case "$tag" in
  v*-sk.*) display="${tag#v}"; base="${display%%-sk.*}" ;;
  *) echo "deploy-setup: $sha12 is not an sk release tag (v<upstream>-sk.<n>)" >&2; exit 1 ;;
esac
boot() { ls -d "$HERMES_HOME"/tools/python-3.1[0-9]*/bin/python3 2>/dev/null | sort -V | tail -1; }
py="$(boot)"; [ -n "$py" ] || { echo "deploy-setup: no pm python in $HERMES_HOME/tools" >&2; exit 1; }
"$py" -m pm.cli install --tools-only
py="$(boot)"
extras="${HERMES_SK_EXTRAS:-telegram mcp acp ddgs google youtube}"
extra_args=(); for e in $extras; do extra_args+=(--extra "$e"); done
"$py" -m pm.cli install "${extra_args[@]}"
"$py" -I sk/verify-install.py --extras "$extras" --root "$here"
stamp="$("$py" -I -c 'import sys; sys.path.insert(0, "."); from pathlib import Path; from pm.paths import install_stamp_path; print(install_stamp_path(Path(".").resolve()))')"
"$py" scripts/write_install_stamp.py --output "$stamp" --commit "$sha" --branch "sk/v$base" \
  --base-version "$base" --display-version "$display" --distance 0 --source sk-deploy \
  --update-mechanism external
"$here/.hermes/bin/hermes" --version
