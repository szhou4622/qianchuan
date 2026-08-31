#!/usr/bin/env python3
import argparse
import json
import mimetypes
import re
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


APP_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
REQUIRED_FIELDS = {
    "app_name", "version", "download_url", "sha256", "notes", "force", "min_supported_version"
}
CONTACT_TEXT_FIELDS = {
    "title",
    "contact_name",
    "contact_role",
    "description",
    "contact_label",
    "contact_value",
    "support_hours",
    "action_label",
    "updated_at",
}


class UpdateHandler(SimpleHTTPRequestHandler):
    server_version = "OVDTUpdateServer/2.0"

    def __init__(self, *args, directory=None, **kwargs):
        self.root = Path(directory).resolve()
        super().__init__(*args, directory=directory, **kwargs)

    def end_headers(self):
        path = urlparse(self.path).path
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Cache-Control",
            "no-store, max-age=0"
            if path in {"/latest.json", "/api/update/latest", "/api/contact"}
            else "public, max-age=3600",
        )
        super().end_headers()

    def send_json(self, status, payload, head=False):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def latest(self, head=False):
        query = parse_qs(urlparse(self.path).query)
        app_name = str((query.get("app_name") or [""])[0]).strip()
        if not APP_NAME_RE.fullmatch(app_name):
            self.send_json(400, {"ok": False, "error": "app_name 无效"}, head)
            return
        manifest_path = self.root / "apps" / app_name / "latest.json"
        channel = str((query.get("channel") or ["production"])[0]).strip()
        if channel not in {"production", "development", "stable"}:
            self.send_json(400, {"ok": False, "error": "channel 无效"}, head)
            return
        if app_name == "QCSCKP":
            channel_path = self.root / "apps" / app_name / "channels" / channel / "latest.json"
            if channel != "production" or channel_path.exists():
                manifest_path = channel_path
        elif channel != "production":
            self.send_json(404, {"ok": False, "error": "该软件未配置此渠道"}, head)
            return
        if not manifest_path.is_file():
            self.send_json(404, {"ok": False, "error": "未找到该软件的更新配置"}, head)
            return
        try:
            manifest = json.loads(manifest_path.read_text("utf-8"))
            missing = sorted(REQUIRED_FIELDS - set(manifest))
            if missing or manifest.get("app_name") != app_name:
                raise ValueError(f"配置字段异常：{','.join(missing) or 'app_name'}")
            if manifest.get("channel", "production") != channel:
                raise ValueError("发布渠道不一致")
            self.send_json(200, manifest, head)
        except Exception as exc:
            self.send_json(500, {"ok": False, "error": str(exc)}, head)

    def contact(self, head=False):
        query = parse_qs(urlparse(self.path).query)
        app_name = str((query.get("app_name") or [""])[0]).strip()
        if not APP_NAME_RE.fullmatch(app_name):
            self.send_json(400, {"ok": False, "error": "app_name 无效"}, head)
            return
        config_path = self.root / "apps" / app_name / "contact.json"
        if not config_path.is_file():
            self.send_json(404, {"ok": False, "error": "该软件暂未配置联系信息"}, head)
            return
        try:
            config = json.loads(config_path.read_text("utf-8"))
            if not isinstance(config, dict) or config.get("app_name") != app_name:
                raise ValueError("联系配置 app_name 不匹配")

            payload = {
                "ok": True,
                "app_name": app_name,
                "enabled": bool(config.get("enabled", True)),
            }
            for field in CONTACT_TEXT_FIELDS:
                payload[field] = str(config.get(field) or "").strip()
            for field in ("qr_image_url", "action_url"):
                value = str(config.get(field) or "").strip()
                if value and urlparse(value).scheme != "https":
                    raise ValueError(f"{field} 必须使用 HTTPS")
                payload[field] = value
            self.send_json(200, payload, head)
        except Exception as exc:
            self.send_json(500, {"ok": False, "error": str(exc)}, head)

    def route(self, head=False):
        path = urlparse(self.path).path
        if path == "/health":
            apps_dir = self.root / "apps"
            apps = sorted(item.name for item in apps_dir.iterdir() if item.is_dir()) if apps_dir.exists() else []
            self.send_json(200, {"ok": True, "service": "ovdt-update", "multi_app": True, "apps": apps}, head)
            return True
        if path == "/api/update/latest":
            self.latest(head)
            return True
        if path == "/api/contact":
            self.contact(head)
            return True
        return False

    def do_GET(self):
        if not self.route(False):
            super().do_GET()

    def do_HEAD(self):
        if not self.route(True):
            super().do_HEAD()

    def list_directory(self, path):
        self.send_error(403, "Directory listing is disabled")
        return None


def main():
    parser = argparse.ArgumentParser(description="Multi-app update server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8792)
    parser.add_argument("--root", default="/opt/original-video-dedup-update")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    mimetypes.add_type("application/json", ".json")
    mimetypes.add_type("application/x-msdownload", ".exe")
    mimetypes.add_type("application/x-apple-diskimage", ".dmg")

    def handler(*handler_args, **handler_kwargs):
        return UpdateHandler(*handler_args, directory=str(root), **handler_kwargs)

    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"update server listening on {args.host}:{args.port}, root={root}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
