"""Loopback-only HTTP facade for the contact-author UI."""

from __future__ import annotations

import json
import mimetypes
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import urlsplit

from services.contact_config import ContactConfigService


_PREVIEW_HTML = """<!doctype html>
<html lang=\"zh-CN\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width\">
<title>联系作者预览</title><style>
body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0f172a;color:#e2e8f0;font:14px system-ui}
.card{width:240px;padding:18px;border:1px solid #334155;border-radius:16px;background:#111c31;box-shadow:0 20px 50px #02061780;text-align:center}
button{width:100%;padding:11px;border:1px solid #334155;border-radius:10px;background:#1e293b;color:#e2e8f0;font-weight:650;cursor:pointer}
img{display:none;width:100%;margin-top:14px;border-radius:12px;background:white}.msg{margin-top:12px;color:#94a3b8;min-height:20px}
</style></head><body><div class=\"card\"><button id=\"contact\">联系作者</button><img id=\"image\" alt=\"联系作者\"><div class=\"msg\" id=\"msg\">悬停、聚焦或点击查看</div></div>
<script>
let loaded=false;const b=document.querySelector('#contact'),i=document.querySelector('#image'),m=document.querySelector('#msg');
async function load(){if(loaded)return;loaded=true;m.textContent='正在读取联系方式…';try{const r=await fetch('/api/contact',{cache:'no-store'}),c=await r.json();if(c.status==='disabled'||c.status==='missing_image'){i.style.display='none';m.textContent=c.message;return}const u=c.display_image_url||c.qr_image_url;if(!u)throw new Error('missing image');const n=new Image;n.onload=()=>{i.src=u;i.style.display='block';m.textContent=''};n.onerror=()=>{m.textContent='联系方式图片加载失败'};n.src=u}catch(e){m.textContent='暂时无法读取联系方式'}}
for(const e of ['mouseenter','focus','click'])b.addEventListener(e,load);
</script></body></html>""".encode("utf-8")


class ContactLocalHttpServer:
    def __init__(
        self,
        service: Optional[ContactConfigService] = None,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
    ) -> None:
        self.service = service or ContactConfigService()
        self.host = host
        self.port = int(port)
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def base_url(self) -> str:
        if self._server is None:
            return ""
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def contact_url(self) -> str:
        return f"{self.base_url}/api/contact" if self.base_url else ""

    @property
    def preview_url(self) -> str:
        return f"{self.base_url}/contact-preview" if self.base_url else ""

    def _make_handler(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "QCSCKP-Contact/1"

            def log_message(self, _format: str, *_args: Any) -> None:
                return

            def _send_bytes(
                self,
                status: int,
                payload: bytes,
                content_type: str,
                *,
                cache_control: str = "no-store",
            ) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", cache_control)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(payload)

            def do_OPTIONS(self) -> None:  # noqa: N802
                self.send_response(204)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.end_headers()

            def do_GET(self) -> None:  # noqa: N802
                path = urlsplit(self.path).path
                if path == "/api/contact":
                    try:
                        result = dict(owner.service.get_contact_config())
                    except Exception:
                        # Keep the local facade deterministic and do not expose
                        # filesystem/network exception details to the UI.
                        result = {
                            "app_name": owner.service.app_name,
                            "enabled": True,
                            "qr_image_url": "",
                            "updated_at": "",
                            "source": "builtin",
                            "cached": False,
                            "status": "fallback",
                            "message": "",
                            "use_builtin_image": True,
                        }
                    if bool(result.pop("use_builtin_image", False)):
                        result["display_image_url"] = (
                            owner.base_url + "/api/contact/builtin-image"
                        )
                    else:
                        result["display_image_url"] = str(
                            result.get("qr_image_url") or ""
                        )
                    payload = json.dumps(
                        result,
                        ensure_ascii=False,
                        sort_keys=True,
                    ).encode("utf-8")
                    self._send_bytes(200, payload, "application/json; charset=utf-8")
                    return
                if path == "/api/contact/builtin-image":
                    try:
                        with open(owner.service.fallback_image_file, "rb") as handle:
                            payload = handle.read()
                    except OSError:
                        self._send_bytes(
                            404,
                            b'{"error":"fallback image missing"}',
                            "application/json; charset=utf-8",
                        )
                        return
                    content_type = (
                        mimetypes.guess_type(owner.service.fallback_image_file)[0]
                        or "application/octet-stream"
                    )
                    self._send_bytes(
                        200,
                        payload,
                        content_type,
                        cache_control="public, max-age=3600",
                    )
                    return
                if path == "/contact-preview":
                    self._send_bytes(200, _PREVIEW_HTML, "text/html; charset=utf-8")
                    return
                self._send_bytes(
                    404,
                    b'{"error":"not found"}',
                    "application/json; charset=utf-8",
                )

        return Handler

    def start(self) -> str:
        if self._server is not None:
            return self.contact_url
        server = ThreadingHTTPServer(
            (self.host, self.port),
            self._make_handler(),
        )
        server.daemon_threads = True
        self._server = server
        self._thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.2},
            daemon=True,
            name="qcsckp-contact-http",
        )
        self._thread.start()
        return self.contact_url

    def stop(self) -> None:
        server = self._server
        thread = self._thread
        self._server = None
        self._thread = None
        if server is None:
            return
        server.shutdown()
        server.server_close()
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
