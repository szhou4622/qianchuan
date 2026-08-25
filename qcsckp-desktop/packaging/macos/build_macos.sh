#!/usr/bin/env bash
set -euo pipefail

VERSION="${1:-0.1.61}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
PYTHON="$PROJECT_ROOT/.venv/bin/python"
ENTRY="$PROJECT_ROOT/gui_app.py"
STATIC_DIR="$PROJECT_ROOT/static"
CONTACT_CONFIG="$PROJECT_ROOT/services/contact_config.py"
CONTACT_HTTP="$PROJECT_ROOT/services/contact_http.py"
CONTACT_FALLBACK="$STATIC_DIR/images/contact-author-fallback.svg"
LICENSE_CLIENT="$PROJECT_ROOT/services/license_client.py"
LICENSE_STORAGE="$PROJECT_ROOT/services/license_storage.py"
LICENSE_MANAGER="$PROJECT_ROOT/services/license_manager.py"
LICENSE_PAGE="$STATIC_DIR/license.html"
LICENSE_MANAGEMENT_PAGE="$STATIC_DIR/license_management.html"
OUTPUT_ROOT="$PROJECT_ROOT/output/macos/v$VERSION"
BUNDLE_ID="${QCSCKP_BUNDLE_ID:-com.dadaozixun.qcsckp}"
ARCH="${QCSCKP_MAC_ARCH:-$(uname -m)}"
CERTIFICATE_PATH="${APPLE_CERTIFICATE_PATH:-}"
CERTIFICATE_PASSWORD="${APPLE_CERTIFICATE_PASSWORD:-}"
KEYCHAIN_PATH=""

cleanup() {
  if [[ -n "$KEYCHAIN_PATH" ]]; then
    security delete-keychain "$KEYCHAIN_PATH" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

for required in \
  "$PYTHON" \
  "$ENTRY" \
  "$STATIC_DIR" \
  "$CONTACT_CONFIG" \
  "$CONTACT_HTTP" \
  "$CONTACT_FALLBACK" \
  "$LICENSE_CLIENT" \
  "$LICENSE_STORAGE" \
  "$LICENSE_MANAGER" \
  "$LICENSE_PAGE" \
  "$LICENSE_MANAGEMENT_PAGE"; do
  if [[ ! -e "$required" ]]; then
    echo "Required build input does not exist: $required" >&2
    exit 1
  fi
done

if [[ -z "$CERTIFICATE_PATH" || -z "$CERTIFICATE_PASSWORD" ]]; then
  echo "APPLE_CERTIFICATE_PATH and APPLE_CERTIFICATE_PASSWORD are required for a notarized build." >&2
  exit 1
fi
for name in APPLE_ID APPLE_TEAM_ID APPLE_APP_SPECIFIC_PASSWORD; do
  if [[ -z "${!name:-}" ]]; then
    echo "$name is required for notarization." >&2
    exit 1
  fi
done

TEMP_ROOT="${RUNNER_TEMP:-${TMPDIR:-/tmp}}"
KEYCHAIN_PATH="$TEMP_ROOT/qcsckp-signing-$(uuidgen).keychain-db"
KEYCHAIN_PASSWORD="$(uuidgen)$(uuidgen)"
security create-keychain -p "$KEYCHAIN_PASSWORD" "$KEYCHAIN_PATH"
security set-keychain-settings -lut 21600 "$KEYCHAIN_PATH"
security unlock-keychain -p "$KEYCHAIN_PASSWORD" "$KEYCHAIN_PATH"
security import "$CERTIFICATE_PATH" -k "$KEYCHAIN_PATH" -P "$CERTIFICATE_PASSWORD" -T /usr/bin/codesign -T /usr/bin/security
security set-key-partition-list -S apple-tool:,apple: -s -k "$KEYCHAIN_PASSWORD" "$KEYCHAIN_PATH" >/dev/null
security list-keychains -d user -s "$KEYCHAIN_PATH" login.keychain-db

IDENTITY="$(security find-identity -v -p codesigning "$KEYCHAIN_PATH" | awk -F\" '/Developer ID Application/{print $2; exit}')"
if [[ -z "$IDENTITY" ]]; then
  echo "Developer ID Application identity was not found in the temporary keychain." >&2
  exit 1
fi

rm -rf "$OUTPUT_ROOT"
mkdir -p "$OUTPUT_ROOT/dist" "$OUTPUT_ROOT/build" "$OUTPUT_ROOT/spec"

"$PYTHON" -m PyInstaller \
  --noconfirm \
  --clean \
  --windowed \
  --onedir \
  --name QCSCKP \
  --add-data "$STATIC_DIR:static" \
  --collect-all playwright \
  --collect-all webview \
  --collect-all lark_oapi \
  --collect-all baseopensdk \
  --collect-all pystray \
  --collect-all PIL \
  --collect-all keyring \
  --hidden-import services.contact_config \
  --hidden-import services.contact_http \
  --hidden-import services.license_client \
  --hidden-import services.license_storage \
  --hidden-import services.license_manager \
  --hidden-import services.update_manifest \
  --hidden-import services.update_service_mac \
  --codesign-identity "$IDENTITY" \
  --osx-bundle-identifier "$BUNDLE_ID" \
  --target-arch "$ARCH" \
  --distpath "$OUTPUT_ROOT/dist" \
  --workpath "$OUTPUT_ROOT/build" \
  --specpath "$OUTPUT_ROOT/spec" \
  "$ENTRY"

APP_PATH="$OUTPUT_ROOT/dist/QCSCKP.app"
codesign --verify --deep --strict --verbose=2 "$APP_PATH"

NOTARY_PROFILE="qcsckp-notary-$(uuidgen)"
xcrun notarytool store-credentials "$NOTARY_PROFILE" \
  --keychain "$KEYCHAIN_PATH" \
  --apple-id "$APPLE_ID" \
  --team-id "$APPLE_TEAM_ID" \
  --password "$APPLE_APP_SPECIFIC_PASSWORD" >/dev/null

APP_ZIP="$OUTPUT_ROOT/QCSCKP-v$VERSION-macOS-$ARCH.app.zip"
ditto -c -k --keepParent "$APP_PATH" "$APP_ZIP"
xcrun notarytool submit "$APP_ZIP" \
  --keychain-profile "$NOTARY_PROFILE" \
  --keychain "$KEYCHAIN_PATH" \
  --wait
xcrun stapler staple "$APP_PATH"
xcrun stapler validate "$APP_PATH"
spctl --assess --type execute --verbose=2 "$APP_PATH"

DMG_PATH="$OUTPUT_ROOT/QCSCKP-v$VERSION-macOS-$ARCH.dmg"
hdiutil create -volname "QCSCKP" -srcfolder "$APP_PATH" -ov -format UDZO "$DMG_PATH"
codesign --force --timestamp --sign "$IDENTITY" "$DMG_PATH"
xcrun notarytool submit "$DMG_PATH" \
  --keychain-profile "$NOTARY_PROFILE" \
  --keychain "$KEYCHAIN_PATH" \
  --wait
xcrun stapler staple "$DMG_PATH"
xcrun stapler validate "$DMG_PATH"
spctl --assess --type open --context context:primary-signature --verbose=2 "$DMG_PATH"

shasum -a 256 "$DMG_PATH" > "$DMG_PATH.sha256.txt"
rm -f "$APP_ZIP"

echo "APP_PATH=$APP_PATH"
echo "DMG_PATH=$DMG_PATH"
