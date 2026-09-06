#!/usr/bin/env bash

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_AGENT="$REPO_DIR/agent/quality-guardian.md"
OPENCODE_AGENT_DIR="${HOME}/.config/opencode/agents"
TARGET_AGENT="$OPENCODE_AGENT_DIR/quality-guardian.md"

echo "== Quality Guardian Installer =="

if [[ ! -f "$SOURCE_AGENT" ]]; then
    echo "ERROR: Agent source not found:"
    echo "  $SOURCE_AGENT"
    exit 1
fi

mkdir -p "$OPENCODE_AGENT_DIR"

if [[ -f "$TARGET_AGENT" ]]; then
    BACKUP="${TARGET_AGENT}.bak.$(date +%Y%m%d%H%M%S)"
    cp "$TARGET_AGENT" "$BACKUP"
    echo "Existing agent backed up:"
    echo "  $BACKUP"
fi

cp "$SOURCE_AGENT" "$TARGET_AGENT"

echo
echo "Installation complete."
echo
echo "Installed:"
echo "  $TARGET_AGENT"
echo
echo "Version:"
cat "$REPO_DIR/VERSION"
echo

if command -v opencode >/dev/null 2>&1; then
    echo "OpenCode:"
    opencode --version || true
else
    echo "WARNING: OpenCode command not found."
fi
