"""Explicit offline build check; never enter the business UI or load user credentials."""
from __future__ import annotations

import io
import importlib.metadata
import json
import logging
import os
from pathlib import Path
import socket
import sys
import tempfile
import time
from unittest.mock import patch


def _local(address):
    return isinstance(address, tuple) and str(address[0]).lower() in {'127.0.0.1', '::1', 'localhost'}


def _probe():
    import lark_oapi
    import baseopensdk
    import baseopensdk.api.base.v1
    import webview
    import pystray
    from lark_oapi.ws import Client
    from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTrigger
    from PIL import Image
    from playwright.sync_api import sync_playwright
    from services.qianchuan_open_api.token_provider import _oauth_callback_query
    from utils.common import require_executable_path

    modules = [lark_oapi, baseopensdk, baseopensdk.api.base.v1, webview, pystray]
    if getattr(sys, 'frozen', False):
        for module in modules:
            assert 'PyiFrozen' in type(module.__loader__).__name__, 'SDK was not loaded from the frozen archive'
    assert Client and P2CardActionTrigger
    versions = {name: importlib.metadata.version(name) for name in ('lark-oapi', 'baseopensdk')}
    assert lark_oapi.Client.builder().app_id('synthetic-probe').app_secret('synthetic-secret').build()
    image = io.BytesIO()
    Image.new('RGB', (2, 2), 'blue').save(image, format='PNG')
    assert image.getvalue().startswith(b'\x89PNG')
    captured = []
    expected_state = 'synthetic-packaging-probe'
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=require_executable_path(), headless=True, timeout=15000)
        try:
            context = browser.new_context()
            def route_request(route, request):
                callback = _oauth_callback_query(expected_state, request.url)
                if callback:
                    captured.append(callback)
                    route.abort()
                elif request.url == 'http://packaging-probe.invalid/authorize':
                    route.fulfill(content_type='text/html', body=(
                        "<script>location.href='http://callback-probe.invalid/?auth_code=synthetic-code&state="
                        + expected_state + "'</script>"))
                else:
                    route.abort()
            context.route('**/*', route_request)
            page = context.new_page()
            try:
                page.goto('http://packaging-probe.invalid/authorize', wait_until='domcontentloaded', timeout=10000)
            except Exception:
                # A script-triggered redirect may interrupt the first goto
                # before the route callback is delivered to Python.
                pass
            deadline = time.monotonic() + 5
            while not captured and time.monotonic() < deadline:
                page.wait_for_timeout(50)
            assert captured, 'Synthetic authorization redirect was not captured'
        finally:
            browser.close()
    return {'success': True, 'frozen': bool(getattr(sys, 'frozen', False)),
            'sdk_imports_from_archive': True, 'sdk_versions': versions, 'image_plugin_working': True,
            'real_headless_chrome_launched': True, 'synthetic_authorization_redirect_intercepted': True,
            'business_ui_started': False, 'real_authorization_requested': False}


def main(arguments=None):
    arguments = list(sys.argv[1:] if arguments is None else arguments)
    if len(arguments) != 1:
        return 2
    report = Path(arguments[0]).resolve()
    if report.exists():
        return 2
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    def connect(sock, address):
        if not _local(address):
            raise AssertionError('External network is forbidden in the packaging probe')
        return original_connect(sock, address)
    def connect_ex(sock, address):
        if not _local(address):
            raise AssertionError('External network is forbidden in the packaging probe')
        return original_connect_ex(sock, address)
    with tempfile.TemporaryDirectory(prefix='qcsckp-packaged-probe-') as directory:
        # Set these before importing config. Never read the real app data tree.
        with patch.dict(os.environ, {'QCSCKP_HOME': directory, 'QCSCKP_DATA_DIR': str(Path(directory) / 'data')}), patch.object(
            socket.socket, 'connect', connect
        ), patch.object(socket.socket, 'connect_ex', connect_ex):
            try:
                result = _probe()
            except Exception as exc:
                result = {'success': False, 'error_type': type(exc).__name__, 'detail': str(exc)[:500]}
            finally:
                logging.shutdown()
    report.parent.mkdir(parents=True, exist_ok=True)
    with report.open('x', encoding='utf-8') as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
    return 0 if result['success'] else 1
