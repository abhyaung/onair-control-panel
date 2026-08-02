#!/bin/sh
# onair — one-line installer.
#
#   curl -fsSL https://raw.githubusercontent.com/abhyaung/onair-control-panel/main/install.sh | sh
#
# Clones (or updates) the repo, then builds, signs and installs the agent.
#
# There is no prebuilt download because macOS will not let an *unsigned* app
# hold Accessibility for the processes it launches — a downloaded unsigned build
# installs cleanly and then silently does nothing. Building locally creates a
# stable signing identity, which also makes the permission survive updates.
set -e

REPO="https://github.com/abhyaung/onair-control-panel"
DEST="${ONAIR_DIR:-$HOME/.onair/src}"

printf '\n\033[1monair installer\033[0m\n'

for tool in git clang swiftc python3 make; do
  command -v "$tool" >/dev/null 2>&1 || {
    printf '\nMissing: %s\n' "$tool"
    printf 'Install the Xcode command line tools first:\n  xcode-select --install\n\n'
    exit 1
  }
done

if [ -d "$DEST/.git" ]; then
  echo "  updating $DEST"
  git -C "$DEST" pull --ff-only --quiet
else
  echo "  cloning into $DEST"
  mkdir -p "$(dirname "$DEST")"
  git clone --quiet "$REPO" "$DEST"
fi

exec "$DEST/agent-macos/install.sh"
