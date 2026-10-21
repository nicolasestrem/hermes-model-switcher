#!/usr/bin/env bash
# LOCAL install of the packaged plugin into a THROWAWAY Hermes home.
#
# Usage: bash scripts/install-local.sh [HERMES_HOME] [PACKAGE_DIR] [--replace] [--enable]
#
# GNU/Linux only (GNU readlink required). Always refuses the live home (~/.hermes).
# Build the package first:  npm run build && python scripts/build_package.py
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

REPLACE=0
ENABLE=0
POSITIONAL=()
for ARG in "$@"; do
  case "$ARG" in
    --replace) REPLACE=1 ;;
    --enable) ENABLE=1 ;;
    --*) echo "install-local: unknown option $ARG" >&2; exit 2 ;;
    *) POSITIONAL+=("$ARG") ;;
  esac
done
if [ "${#POSITIONAL[@]}" -gt 2 ]; then
  echo "Usage: install-local.sh [HERMES_HOME] [PACKAGE_DIR] [--replace] [--enable]" >&2
  exit 2
fi
TARGET_HOME="${POSITIONAL[0]:-${HERMES_HOME:-}}"
if [ -z "$TARGET_HOME" ]; then
  echo "lab helper: no target home given" >&2
  exit 2
fi
# Resolve the native live home independently of HERMES_HOME (the lab override).
if [ -n "${HERMES_MODEL_SWITCHER_LIVE_HOME:-}" ]; then
  LIVE_INPUT="$HERMES_MODEL_SWITCHER_LIVE_HOME"
elif [ -n "${HOME:-}" ]; then
  LIVE_INPUT="$HOME/.hermes"
else
  echo "lab helper: cannot determine live home" >&2
  exit 4
fi
LIVE_HOME="$(readlink -m -- "$LIVE_INPUT")"
TARGET_HOME="$(readlink -m -- "$TARGET_HOME")"
case "$TARGET_HOME/" in "$LIVE_HOME/"|"$LIVE_HOME/"*)
  echo "lab helper: refusing live home or its descendants" >&2; exit 3;;
esac
case "$LIVE_HOME/" in "$TARGET_HOME/"*)
  echo "lab helper: refusing an ancestor of the live home" >&2; exit 3;;
esac
DEST="$TARGET_HOME/plugins/hermes-model-switcher"
# Do not let a redirected plugins directory turn lab deletion into external deletion.
CHECK_PATH="$DEST"
while [ "$CHECK_PATH" != "/" ]; do
  if [ -L "$CHECK_PATH" ]; then
    echo "lab helper: refusing symlinked destination" >&2; exit 3
  fi
  CHECK_PATH="$(dirname -- "$CHECK_PATH")"
done
RESOLVED_DEST="$(readlink -m -- "$DEST")"
if [ "$RESOLVED_DEST" != "$DEST" ]; then
  echo "lab helper: destination escapes the lab home" >&2; exit 3
fi

PKG="${POSITIONAL[1]:-$REPO/dist-package/hermes-model-switcher}"
# ``__init__.py`` is part of the loadable layout, not a nicety: Hermes rejects a
# plugin.yaml with no __init__.py/desktop/plugin.js/plugin.json beside it. The
# rest of this list is every file the installed backend actually loads: python/
# (imported by dashboard/plugin_api.py), dashboard/model_catalog.py (loaded by
# file path), and dashboard/dist/style.css (the manifest's css asset). A
# package missing any of them would "install" and then fail at import or 404.
REQUIRED=(
  "plugin.yaml"
  "__init__.py"
  "dashboard/manifest.json"
  "dashboard/dist/index.js"
  "dashboard/dist/style.css"
  "dashboard/plugin_api.py"
  "dashboard/model_catalog.py"
  "python/hermes_adapter.py"
  "python/route_inventory.py"
  "python/model_switcher/__init__.py"
  "python/model_switcher/engine.py"
  "python/model_switcher/journal.py"
  "python/model_switcher/adapters.py"
  "python/model_switcher/presets.py"
  "python/model_switcher/registry/__init__.py"
  "python/model_switcher/registry/scanner.py"
  "python/model_switcher/registry/descriptors.py"
)
for REL in "${REQUIRED[@]}"; do
  if [ ! -f "$PKG/$REL" ]; then
    echo "install-local: $PKG/$REL missing — not an assembled package (run: npm run build && python scripts/build_package.py)" >&2
    exit 1
  fi
done

if [ -e "$DEST" ]; then
  if [ "$REPLACE" != 1 ]; then
    echo "install-local: destination exists; pass --replace to replace it" >&2
    exit 5
  fi
  # Preserve the old install and recovery state rather than deleting it.
  BACKUP="$(mktemp -d -- "$TARGET_HOME/.model-switcher-backup.XXXXXXXX")"
  mv -- "$DEST" "$BACKUP/plugin"
  echo "install-local: previous installation preserved at $BACKUP/plugin"
fi
mkdir -p "$TARGET_HOME/plugins"
cp -a -- "$PKG" "$DEST"
chmod 700 "$DEST"
echo "installed $DEST"
# D3 truth: plugin state (journal/presets) NEVER lives inside the plugin tree —
# the backend refuses that — only at <home>/state/hermes-model-switcher (0700,
# created on first write). Nothing to pre-create here.
if [ "$ENABLE" != 1 ]; then
  # D2: a user-source plugin stays hidden (and its API unmounted) until it is
  # listed in plugins.enabled — print the exact enable command.
  echo "install-local: not enabled yet — enable it with:"
  echo "  HERMES_HOME=\"$TARGET_HOME\" hermes plugins enable hermes-model-switcher"
  exit 0
fi
if ! command -v hermes >/dev/null 2>&1; then
  echo "install-local: --enable needs the hermes CLI on PATH; enable manually with:" >&2
  echo "  HERMES_HOME=\"$TARGET_HOME\" hermes plugins enable hermes-model-switcher" >&2
  exit 6
fi
if ! HERMES_HOME="$TARGET_HOME" hermes plugins enable hermes-model-switcher; then
  echo "install-local: hermes plugins enable failed; enable manually with:" >&2
  echo "  HERMES_HOME=\"$TARGET_HOME\" hermes plugins enable hermes-model-switcher" >&2
  exit 7
fi
