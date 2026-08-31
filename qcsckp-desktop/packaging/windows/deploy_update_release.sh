#!/usr/bin/env bash
set -euo pipefail

VERSION="${1:?version is required}"
ZIP_PATH="${2:?zip path is required}"
MANIFEST_PATH="${3:?manifest path is required}"
EXPECTED_SHA256="${4:?sha256 is required}"
CHANNEL="${5:?channel is required}"
REVISION="${6:?revision is required}"
case "$CHANNEL" in production|development|stable) ;; *) exit 2;; esac
[[ "$REVISION" =~ ^[1-9][0-9]*$ ]] || exit 2

APP_NAME="QCSCKP"
ROOT="/opt/original-video-dedup-update"
FILE_NAME="QCSCKP-v${VERSION}-${CHANNEL}-r${REVISION}-Windows-x64.zip"
RELEASE_ROOT="${ROOT}/downloads/${APP_NAME}/${CHANNEL}/${VERSION}"
TARGET="${RELEASE_ROOT}/r${REVISION}"
TEMP="${RELEASE_ROOT}/.r${REVISION}-$$"
APP_DIR="${ROOT}/apps/${APP_NAME}/channels/${CHANNEL}"
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

python3 - "${MANIFEST_PATH}" "${VERSION}" "${EXPECTED_SHA256}" "$CHANNEL" "$REVISION" "$ZIP_PATH" <<'PY'
import json, pathlib, sys, zipfile
path = pathlib.Path(sys.argv[1])
version, checksum, channel, revision, archive = sys.argv[2:]
data = json.loads(path.read_text(encoding="utf-8"))
assert data["app_name"] == "QCSCKP"
assert data["version"] == version
assert data['channel'] == channel and data['build_revision'] == int(revision)
with zipfile.ZipFile(archive) as z:
    names=[n for n in z.namelist() if n.endswith('/PACKAGE-MANIFEST.json')]
    assert len(names)==1
    package=json.loads(z.read(names[0]).decode('utf-8-sig'))
    for key in ('app_name','version','channel','build_revision','source_commit'):
        assert package[key]==data[key], key
assert data["sha256"]["windows_x64"] == checksum
assert isinstance(data["notes"], list) and data["notes"]
assert isinstance(data["force"], bool)
assert data.get("min_supported_version")
PY

install -d -m 0755 "${RELEASE_ROOT}" "${APP_DIR}" "${BACKUP_DIR}"
exec 9>"${ROOT}/.publish.lock"
flock -x 9
if [[ "$CHANNEL" == stable && -f "${APP_DIR}/latest.json" ]]; then
  cmp -s "${MANIFEST_PATH}" "${APP_DIR}/latest.json" || { echo 'stable channel is frozen' >&2; exit 1; }
fi
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
  cp -p -- "${APP_DIR}/latest.json" "${BACKUP_DIR}/latest-${CHANNEL}-${STAMP}-$$.json"
fi
install -m 0644 "${MANIFEST_PATH}" "${APP_DIR}/latest.json.tmp-${STAMP}"
mv -f -- "${APP_DIR}/latest.json.tmp-${STAMP}" "${APP_DIR}/latest.json"
if [[ "$CHANNEL" == production ]]; then
  LEGACY="${ROOT}/apps/${APP_NAME}/latest.json"
  [[ ! -f "$LEGACY" ]] || cp -p -- "$LEGACY" "${BACKUP_DIR}/latest-legacy-${STAMP}-$$.json"
  install -m 0644 "${MANIFEST_PATH}" "${LEGACY}.tmp-$$"
  mv -f -- "${LEGACY}.tmp-$$" "$LEGACY"
fi
trap - EXIT

echo "QCSCKP_UPDATE_DEPLOYED=true"
echo "version=${VERSION}"
echo "release=${TARGET}/${FILE_NAME}"
echo "manifest=${APP_DIR}/latest.json"
