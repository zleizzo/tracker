#!/bin/bash
# Builds daemon/build/TrackerDaemon.app. Works with just the Command Line Tools,
# even when the full Xcode license has not been accepted.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="$HERE/build"
APP="$OUT/TrackerDaemon.app"

SWIFTC=""
SDK=""
if /usr/bin/swiftc --version >/dev/null 2>&1; then
  SWIFTC=/usr/bin/swiftc
  SDK="$(xcrun --show-sdk-path 2>/dev/null || true)"
fi
if [ -z "$SWIFTC" ] && [ -x /Library/Developer/CommandLineTools/usr/bin/swiftc ]; then
  SWIFTC=/Library/Developer/CommandLineTools/usr/bin/swiftc
  SDK=/Library/Developer/CommandLineTools/SDKs/MacOSX.sdk
fi
if [ -z "$SWIFTC" ]; then
  echo "error: no usable swiftc. Install the Command Line Tools: xcode-select --install" >&2
  exit 1
fi
ARCH="$(uname -m)"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
echo "compiling with $SWIFTC"
"$SWIFTC" -O -swift-version 5 ${SDK:+-sdk "$SDK"} -target "$ARCH-apple-macosx13.0" \
  -module-name TrackerDaemon "$HERE"/Sources/*.swift -o "$APP/Contents/MacOS/TrackerDaemon"
cp "$HERE/Info.plist" "$APP/Contents/Info.plist"
echo "APPL????" > "$APP/Contents/PkgInfo"
# Ad-hoc signature so macOS privacy permissions attach to a stable identity.
codesign --force --sign - --identifier com.tracker.daemon "$APP" >/dev/null 2>&1 || echo "warning: codesign failed (continuing unsigned)"
echo "built $APP"
