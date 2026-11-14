#!/usr/bin/env bash
# sk-deploy tool `setup` for a hermes-agent export (SK fork only, not upstream).
#
# Runs inside ~/.local/share/sk-tools/hermes-agent/<sha12> before the export is
# published. A tool export has no .git, so this makes it a self-contained install:
#   1. pm tools closure (node, pnpm, python, ... pinned by pm/lock.json)
#   2. pm dependency env [all] + telegram, keyed to this export's path, which also
#      mints .hermes/bin/hermes and hermes-acp for this tree
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
"$py" -m pm.cli install --extra telegram
stamp="$("$py" -I -c 'import sys; sys.path.insert(0, "."); from pathlib import Path; from pm.paths import install_stamp_path; print(install_stamp_path(Path(".").resolve()))')"
"$py" scripts/write_install_stamp.py --output "$stamp" --commit "$sha" --branch "sk/v$base" \
  --base-version "$base" --display-version "$display" --distance 0 --source sk-deploy \
  --update-mechanism external
"$here/.hermes/bin/hermes" --version
