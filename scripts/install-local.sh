#!/usr/bin/env bash
# LOCAL install of the packaged plugin into a THROWAWAY Hermes home.
#
# Usage: bash scripts/install-local.sh [HERMES_HOME] [PACKAGE_DIR] [--replace]
#
# Refuses the LIVE home (~/.hermes) unless HERMES_MODEL_SWITCHER_ALLOW_LIVE=1.
# Build the package first:  npm run build && python scripts/build_package.py
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

TARGET_HOME="${1:-${HERMES_HOME:-}}"
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

PKG="${2:-$REPO/dist-package/hermes-model-switcher}"
# ``__init__.py`` is part of the loadable layout, not a nicety: Hermes rejects a
# plugin.yaml with no __init__.py/desktop/plugin.js/plugin.json beside it.
if [ ! -f "$PKG/dashboard/manifest.json" ] || [ ! -f "$PKG/dashboard/dist/index.js" ] \
   || [ ! -f "$PKG/dashboard/plugin_api.py" ] || [ ! -f "$PKG/__init__.py" ]; then
  echo "install-local: $PKG is not an assembled package (run: npm run build && python scripts/build_package.py)" >&2
  exit 1
fi

if [ -e "$DEST" ]; then
  if [ "${3:-}" != "--replace" ]; then
    echo "install-local: destination exists; pass --replace to replace it" >&2
    exit 5
  fi
  # Preserve the old install and recovery state rather than deleting it.
  BACKUP="$(mktemp -d -- "$TARGET_HOME/plugins/.model-switcher-backup.XXXXXXXX")"
  mv -- "$DEST" "$BACKUP/plugin"
  echo "install-local: previous installation preserved at $BACKUP/plugin"
fi
mkdir -p "$TARGET_HOME/plugins"
cp -a -- "$PKG" "$DEST"
# plugin state (journal/presets) lives under <home>/plugins/<name>/ when the
# backend first writes; ensure it is private from the start.
mkdir -p "$DEST/state"
chmod 700 "$DEST"
echo "installed $DEST"
