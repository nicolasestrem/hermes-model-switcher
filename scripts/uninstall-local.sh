#!/usr/bin/env bash
# LOCAL uninstall: removes the plugin directory from a target Hermes home.
# Applied model choices are NEVER undone by removal — see README "Recovery".
#
# Usage: bash scripts/uninstall-local.sh [HERMES_HOME]
set -euo pipefail

TARGET_HOME="${1:-${HERMES_HOME:-}}"
if [ -z "$TARGET_HOME" ]; then
  echo "uninstall-local: no target home given (arg 1 or \$HERMES_HOME)" >&2
  exit 2
fi
TARGET_HOME="$(readlink -f "$TARGET_HOME")"
# The live home is the REAL ~/.hermes. Fail CLOSED when it can't be determined.
LIVE_HOME="$(readlink -f "${HERMES_MODEL_SWITCHER_LIVE_HOME:-${HOME:-}}/no-op" 2>/dev/null | sed 's|/no-op$||')"
if [ -z "$LIVE_HOME" ] || [ "$LIVE_HOME" = "/no-op" ]; then
  echo "uninstall-local: cannot determine the live home (HOME unset) — refusing" >&2
  exit 4
fi
if [ "$TARGET_HOME" = "$LIVE_HOME" ] && [ "${HERMES_MODEL_SWITCHER_ALLOW_LIVE:-0}" != "1" ]; then
  echo "uninstall-local: refusing the live home $TARGET_HOME (set HERMES_MODEL_SWITCHER_ALLOW_LIVE=1 to override)" >&2
  exit 3
fi

DEST="$TARGET_HOME/plugins/hermes-model-switcher"
if [ ! -d "$DEST" ]; then
  echo "uninstall-local: $DEST not installed" >&2
  exit 1
fi
rm -rf "$DEST"
echo "uninstalled $DEST (plugin state dir removed; applied model choices are NOT reverted)"
