#!/bin/sh
# onair — build, sign, install and launch the macOS agent.
#
# There is no prebuilt download on purpose. macOS will not let an *unsigned* app
# hold Accessibility for the processes it launches, so a downloaded unsigned
# build fails silently: every control looks fine and changes nothing. Building
# here creates a stable local signing identity, which also means the permission
# survives future rebuilds.
set -e

cd "$(dirname "$0")"
say() { printf '\n\033[1m%s\033[0m\n' "$1"; }

say "Checking prerequisites"
for tool in clang swiftc python3 make; do
  command -v "$tool" >/dev/null 2>&1 || {
    echo "  missing: $tool"
    echo "  Install the Xcode command line tools first:  xcode-select --install"
    exit 1
  }
  echo "  ok  $tool"
done

say "Creating the signing identity (first run only)"
make cert

say "Building and signing"
make app

say "Installing to /Applications"
if [ -d /Applications/OnAir.app ]; then
  # A running copy holds the port and would keep answering requests.
  pkill -x OnAir 2>/dev/null || true
  sleep 1
  rm -rf /Applications/OnAir.app
fi
cp -R build/OnAir.app /Applications/OnAir.app
echo "  installed /Applications/OnAir.app"

say "Launching"
open /Applications/OnAir.app
sleep 3

say "One permission left — this cannot be automated"
cat <<'TXT'
  System Settings is opening on Privacy & Security > Accessibility.

    1. Click +
    2. Choose Applications > OnAir
    3. Make sure its toggle is on
    4. Menu bar icon > Stop agent, then Start agent

  Without it, volume, brightness and mute stay read-only.

  Also, once, for meeting controls:
    Chrome > View > Developer > Allow JavaScript from Apple Events

  Then: menu bar icon > Pair iPad (QR code), and scan it.
  Check anything at any time with:  make doctor
TXT
open "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility" 2>/dev/null || true
