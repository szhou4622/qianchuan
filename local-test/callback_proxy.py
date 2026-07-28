#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只公开飞书卡片回调路径的本地反向代理。"""
from __future__ import annotations

import argparse
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


CALLBACK_PATH = "/api/feishu/card_callback.php"
FORWARDED_HEADERS = {
    "content-type",
    "x-lark-request-timestamp",
    "x-lark-request-nonce",
    "x-lark-signature",
}


class CallbackProxyHandler(BaseHTTPRequestHandler):
    server_version = "QCSCKPLocalCallback/1.0"

    def _reject(self, status: int, message: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(message)))
        self.end_headers()
        self.wfile.write(message)

    def do_GET(self) -> None:  # noqa: N802
        self._reject(404, b"not found")

    def do_POST(self) -> None:  # noqa: N802
        if urlsplit(self.path).path != CALLBACK_PATH:
            self._reject(404, b"not found")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._reject(400, b"invalid content length")
            return
        if length <= 0 or length > 1024 * 1024:
            self._reject(400, b"invalid request body")
            return
        raw = self.rfile.read(length)
        upstream_headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() in FORWARDED_HEADERS
        }
        connection = http.client.HTTPConnection(
            self.server.upstream_host, self.server.upstream_port, timeout=20
        )
        try:
            connection.request("POST", CALLBACK_PATH, body=raw, headers=upstream_headers)
            response = connection.getresponse()
            payload = response.read()
            self.send_response(response.status)
            self.send_header(
                "Content-Type",
                response.getheader("Content-Type", "application/json; charset=utf-8"),
            )
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except Exception:
            self._reject(502, b"local api unavailable")
        finally:
            connection.close()

    def log_message(self, _format: str, *_args: object) -> None:
        return


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", type=int, default=8788)
    parser.add_argument("--upstream-host", default="127.0.0.1")
    parser.add_argument("--upstream-port", type=int, default=8787)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.listen_host, args.listen_port), CallbackProxyHandler)
    server.upstream_host = args.upstream_host
    server.upstream_port = args.upstream_port
    server.serve_forever()


if __name__ == "__main__":
    main()
