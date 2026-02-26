#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
TARGET_DIR="$ROOT_DIR/odoo-core/src"
BRANCH="${ODOO_CORE_BRANCH:-19.0}"
REPO_URL="${ODOO_CORE_REPO_URL:-https://github.com/odoo/odoo.git}"

mkdir -p "$TARGET_DIR"

if [[ -d "$TARGET_DIR/.git" ]]; then
  echo "odoo-core/src already initialized. Pulling latest $BRANCH..."
  git -C "$TARGET_DIR" fetch origin "$BRANCH" --depth=1
  git -C "$TARGET_DIR" checkout "$BRANCH"
  git -C "$TARGET_DIR" reset --hard "origin/$BRANCH"
  echo "Updated odoo-core/src to origin/$BRANCH"
  exit 0
fi

if [[ -n "$(ls -A "$TARGET_DIR" 2>/dev/null)" ]]; then
  echo "Refusing to clone: $TARGET_DIR is not empty." >&2
  exit 1
fi

echo "Cloning Odoo core branch $BRANCH into odoo-core/src..."
git clone --depth=1 --branch "$BRANCH" "$REPO_URL" "$TARGET_DIR"
echo "Done. Edit core files under: odoo-core/src"
