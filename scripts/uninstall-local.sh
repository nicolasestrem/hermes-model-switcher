#!/usr/bin/env bash
# GNU/Linux only (GNU readlink required).
# LOCAL uninstall: removes the plugin directory from a target Hermes home.
# Applied model choices are NEVER undone by removal — see README "Recovery".
#
# Usage: bash scripts/uninstall-local.sh [HERMES_HOME]
set -euo pipefail

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

if [ ! -d "$DEST" ]; then
  echo "uninstall-local: $DEST not installed" >&2
  exit 1
fi
# Preserve journals/snapshots for explicit recovery after uninstall.
BACKUP="$(mktemp -d -- "$TARGET_HOME/.model-switcher-removed.XXXXXXXX")"
mv -- "$DEST" "$BACKUP/plugin"
echo "uninstalled $DEST; recovery data preserved at $BACKUP/plugin (model choices unchanged)"
