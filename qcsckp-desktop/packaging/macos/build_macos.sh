#!/usr/bin/env bash
set -euo pipefail

VERSION="${1:-0.1.56}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
PYTHON="$PROJECT_ROOT/.venv/bin/python"
ENTRY="$PROJECT_ROOT/gui_app.py"
STATIC_DIR="$PROJECT_ROOT/static"
CONTACT_CONFIG="$PROJECT_ROOT/services/contact_config.py"
CONTACT_HTTP="$PROJECT_ROOT/services/contact_http.py"
CONTACT_FALLBACK="$STATIC_DIR/images/contact-author-fallback.svg"
OUTPUT_ROOT="$PROJECT_ROOT/output/macos/v$VERSION"

for required in \
  "$PYTHON" \
  "$ENTRY" \
  "$STATIC_DIR" \
  "$CONTACT_CONFIG" \
  "$CONTACT_HTTP" \
  "$CONTACT_FALLBACK"; do
  if [[ ! -e "$required" ]]; then
    echo "Required build input does not exist: $required" >&2
    exit 1
  fi
done

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
  --hidden-import services.contact_config \
  --hidden-import services.contact_http \
  --distpath "$OUTPUT_ROOT/dist" \
  --workpath "$OUTPUT_ROOT/build" \
  --specpath "$OUTPUT_ROOT/spec" \
  "$ENTRY"

echo "APP_PATH=$OUTPUT_ROOT/dist/QCSCKP.app"
