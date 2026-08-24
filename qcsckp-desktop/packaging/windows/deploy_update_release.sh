#!/usr/bin/env bash
set -euo pipefail

VERSION="${1:?version is required}"
ZIP_PATH="${2:?zip path is required}"
MANIFEST_PATH="${3:?manifest path is required}"
EXPECTED_SHA256="${4:?sha256 is required}"

APP_NAME="QCSCKP"
ROOT="/opt/original-video-dedup-update"
FILE_NAME="QCSCKP-v${VERSION}-Windows-x64.zip"
RELEASE_ROOT="${ROOT}/downloads/${APP_NAME}"
TARGET="${RELEASE_ROOT}/${VERSION}"
TEMP="${RELEASE_ROOT}/.${VERSION}-$$"
APP_DIR="${ROOT}/apps/${APP_NAME}"
BACKUP_DIR="${ROOT}/backups/${APP_NAME}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

if [[ ! "${VERSION}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "invalid version" >&2
  exit 1
fi

ACTUAL_SHA256="$(sha256sum "${ZIP_PATH}" | awk '{print $1}')"
if [[ "${ACTUAL_SHA256}" != "${EXPECTED_SHA256}" ]]; then
  echo "sha256 mismatch" >&2
  exit 1
fi

python3 - "${MANIFEST_PATH}" "${VERSION}" "${EXPECTED_SHA256}" <<'PY'
import json, pathlib, sys
path = pathlib.Path(sys.argv[1])
version, checksum = sys.argv[2:]
data = json.loads(path.read_text(encoding="utf-8"))
assert data["app_name"] == "QCSCKP"
assert data["version"] == version
assert data["sha256"]["windows_x64"] == checksum
assert isinstance(data["notes"], list) and data["notes"]
assert isinstance(data["force"], bool)
assert data.get("min_supported_version")
PY

install -d -m 0755 "${RELEASE_ROOT}" "${APP_DIR}" "${BACKUP_DIR}"
trap 'rm -rf -- "${TEMP}"' EXIT
install -d -m 0755 "${TEMP}"
install -m 0644 "${ZIP_PATH}" "${TEMP}/${FILE_NAME}"

if [[ -d "${TARGET}" ]]; then
  if [[ ! -f "${TARGET}/${FILE_NAME}" ]] || ! cmp -s "${TEMP}/${FILE_NAME}" "${TARGET}/${FILE_NAME}"; then
    echo "release already exists with different content" >&2
    exit 1
  fi
  rm -rf -- "${TEMP}"
else
  mv -- "${TEMP}" "${TARGET}"
fi

if [[ -f "${APP_DIR}/latest.json" ]]; then
  cp -p -- "${APP_DIR}/latest.json" "${BACKUP_DIR}/latest-${STAMP}.json"
fi
install -m 0644 "${MANIFEST_PATH}" "${APP_DIR}/latest.json.tmp-${STAMP}"
mv -f -- "${APP_DIR}/latest.json.tmp-${STAMP}" "${APP_DIR}/latest.json"
trap - EXIT

echo "QCSCKP_UPDATE_DEPLOYED=true"
echo "version=${VERSION}"
echo "release=${TARGET}/${FILE_NAME}"
echo "manifest=${APP_DIR}/latest.json"
