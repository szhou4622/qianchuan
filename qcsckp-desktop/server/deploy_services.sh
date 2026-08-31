#!/usr/bin/env bash
set -euo pipefail
SOURCE="$(cd -- "$(dirname -- "$0")" && pwd -P)"
[[ "$SOURCE" == /tmp/qcsckp-channel-server-* ]] || { echo 'Unexpected staging directory'; exit 2; }
UPDATE=/opt/original-video-dedup-update/update_server.py
NGINX=/etc/nginx/sites-available/license-update.conf
[[ "$(sha256sum "$UPDATE" | cut -d' ' -f1)" == 677dd3417a2854ad4d22d771973fa6a587aebb324de20f3c5c0c053ba70ecb53 ]] || { echo 'Update server changed concurrently; abort'; exit 2; }
[[ "$(sha256sum "$NGINX" | cut -d' ' -f1)" == be18115ca7ab383f6b06404dae55ba4bedfbf57285195724cc76313c2772b64b ]] || { echo 'Nginx changed concurrently; abort'; exit 2; }
[[ ! -e /etc/systemd/system/qcsckp-diagnostics.service && ! -e /etc/nginx/conf.d/qcsckp-observability.conf ]] || { echo 'Existing diagnostics deployment; inspect before updating'; exit 2; }
[[ -z "$(ss -ltnH 'sport = :8797')" ]] || { echo 'Port 8797 occupied'; exit 2; }
BACKUP="/opt/backups/qcsckp-channels-$(date -u +%Y%m%dT%H%M%SZ)-$$"
install -d -m 0700 "$BACKUP"
cp -p "$UPDATE" "$BACKUP/update_server.py"
cp -p "$NGINX" "$BACKUP/license-update.conf"
cp -p /opt/original-video-dedup-update/apps/QCSCKP/latest.json "$BACKUP/latest.json"
rollback() {
    systemctl disable --now qcsckp-diagnostics.service 2>/dev/null || true
    cp -p "$BACKUP/update_server.py" "$UPDATE"
    cp -p "$BACKUP/license-update.conf" "$NGINX"
    rm -f /etc/nginx/conf.d/qcsckp-observability.conf /etc/systemd/system/qcsckp-diagnostics.service
    systemctl daemon-reload
    systemctl restart ovdt-update.service
    nginx -t && systemctl reload nginx
    echo "Rollback performed; backup=$BACKUP" >&2
}
trap rollback ERR
install -d -m 0755 /opt/qcsckp-diagnostics
install -m 0644 "$SOURCE/diagnostics_server.py" /opt/qcsckp-diagnostics/diagnostics_server.py
install -m 0644 "$SOURCE/qcsckp-diagnostics.service" /etc/systemd/system/qcsckp-diagnostics.service
install -m 0644 "$SOURCE/qcsckp-observability.conf" /etc/nginx/conf.d/qcsckp-observability.conf
install -m 0644 "$SOURCE/license-update.conf" "$NGINX"
install -m 0644 "$SOURCE/update_server.py" "$UPDATE"
/usr/bin/python3 -m py_compile "$UPDATE" /opt/qcsckp-diagnostics/diagnostics_server.py
nginx -t
systemctl daemon-reload
systemctl enable --now qcsckp-diagnostics.service
systemctl restart ovdt-update.service
systemctl reload nginx
for attempt in 1 2 3 4 5; do
    if curl --fail --silent http://127.0.0.1:8797/health && curl --fail --silent http://127.0.0.1:8792/health; then break; fi
    sleep 1
done
curl --fail --silent http://127.0.0.1:8797/health
curl --fail --silent 'http://127.0.0.1:8792/api/update/latest?app_name=QCSCKP'
systemctl is-active ovdt-license ovdt-update qcsckp-diagnostics nginx
trap - ERR
echo "SERVICES_DEPLOYED backup=$BACKUP"
