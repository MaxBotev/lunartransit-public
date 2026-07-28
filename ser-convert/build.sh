#!/usr/bin/env bash
# Build SER Convert.app — no Xcode project needed, just the Command Line Tools.
set -euo pipefail
cd "$(dirname "$0")"

APP="SER Convert.app"
BIN="SERConvert"

echo ">> compiling"
rm -rf build && mkdir -p build
swiftc -parse-as-library -O \
  -target arm64-apple-macosx13.0 \
  Sources/SER.swift Sources/Toolchain.swift Sources/Converter.swift \
  Sources/Transit.swift Sources/TransitView.swift Sources/SetupView.swift \
  Sources/App.swift \
  -o "build/$BIN"

echo ">> assembling bundle"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "build/$BIN" "$APP/Contents/MacOS/$BIN"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>SER Convert</string>
  <key>CFBundleDisplayName</key><string>SER Convert</string>
  <key>CFBundleIdentifier</key><string>local.serconvert</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleExecutable</key><string>$BIN</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>CFBundleDocumentTypes</key>
  <array><dict>
    <key>CFBundleTypeName</key><string>SER capture</string>
    <key>CFBundleTypeRole</key><string>Viewer</string>
    <key>LSItemContentTypes</key><array><string>public.data</string></array>
    <key>CFBundleTypeExtensions</key><array><string>ser</string></array>
  </dict></array>
</dict>
</plist>
PLIST

# Ad-hoc signature: without it macOS kills an unsigned arm64 bundle on launch.
codesign --force --deep --sign - "$APP" 2>/dev/null || \
  echo "   (codesign unavailable — the app may need a right-click > Open)"

echo ">> built: $(pwd)/$APP"
