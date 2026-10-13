#!/usr/bin/env bash
# Install local privacy pre-commit / pre-push hooks for this repository.
# Hooks live in the shared git hooks dir, so they apply to every worktree
# of this repo. NO CI/CD is involved — this is purely a local gate.
set -euo pipefail
repo_root="$(git rev-parse --path-format=absolute --git-common-dir)"
mkdir -p "$repo_root/hooks"
script_dir="$(cd "$(dirname "$0")" && pwd)"

for hook in pre-commit pre-push; do
  target="$repo_root/hooks/$hook"
  args=""
  [ "$hook" = "pre-commit" ] && args="--staged-only"
  cat > "$target" <<EOF
#!/usr/bin/env bash
# Privacy guard (installed by scripts/install_hooks.sh) — local only.
exec python3 "$script_dir/privacy_guard.py" $args
EOF
  chmod 700 "$target"
  echo "installed $target"
done
echo "Note: .gitignore alone does NOT guarantee exclusion; these hooks are the enforcement."
