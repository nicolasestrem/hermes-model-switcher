#!/usr/bin/env bash
# LOCAL install of the packaged plugin into a THROWAWAY Hermes home.
#
# Usage: bash scripts/install-local.sh [HERMES_HOME] [PACKAGE_DIR]
#
# Refuses the LIVE home (~/.hermes) unless HERMES_MODEL_SWITCHER_ALLOW_LIVE=1.
# Build the package first:  npm run build && python scripts/build_package.py
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

TARGET_HOME="${1:-${HERMES_HOME:-}}"
if [ -z "$TARGET_HOME" ]; then
  echo "install-local: no target home given (arg 1 or \$HERMES_HOME)" >&2
  exit 2
fi
TARGET_HOME="$(readlink -f "$TARGET_HOME")"
# The live home is the REAL ~/.hermes (never $HERMES_HOME, which tests point
# at lab homes). Fail CLOSED when the live home cannot be determined.
LIVE_HOME="$(readlink -f "${HERMES_MODEL_SWITCHER_LIVE_HOME:-${HOME:-}}/no-op" 2>/dev/null | sed 's|/no-op$||')"
if [ -z "$LIVE_HOME" ] || [ "$LIVE_HOME" = "/no-op" ]; then
  echo "install-local: cannot determine the live home (HOME unset) — refusing" >&2
  exit 4
fi
if [ "$TARGET_HOME" = "$LIVE_HOME" ] && [ "${HERMES_MODEL_SWITCHER_ALLOW_LIVE:-0}" != "1" ]; then
  echo "install-local: refusing the live home $TARGET_HOME (set HERMES_MODEL_SWITCHER_ALLOW_LIVE=1 to override)" >&2
  exit 3
fi

PKG="${2:-$REPO/dist-package/hermes-model-switcher}"
if [ ! -f "$PKG/dashboard/manifest.json" ] || [ ! -f "$PKG/dashboard/dist/index.js" ] \
   || [ ! -f "$PKG/dashboard/plugin_api.py" ]; then
  echo "install-local: $PKG is not an assembled package (run: npm run build && python scripts/build_package.py)" >&2
  exit 1
fi

DEST="$TARGET_HOME/plugins/hermes-model-switcher"
rm -rf "$DEST"
mkdir -p "$TARGET_HOME/plugins"
cp -a "$PKG" "$DEST"
# plugin state (journal/presets) lives under <home>/plugins/<name>/ when the
# backend first writes; ensure it is private from the start.
mkdir -p "$DEST/state"
chmod 700 "$DEST"
echo "installed $DEST"
