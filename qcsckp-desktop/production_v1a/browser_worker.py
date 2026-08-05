"""唯一 Chrome 所有者和千川只读传输。"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any

from .adapters.base import ReadTransport
from .security import (
    PlatformNetworkGuard,
    protect_for_current_windows_user,
    unprotect_for_current_windows_user,
)
from .storage import RuntimeDatabase, StorageWriter
from .timeutils import utc_iso

QIANCHUAN_ORIGIN = "https://qianchuan.jinritemai.com"


class BrowserWorkerError(RuntimeError):
    pass


class LoginRequired(BrowserWorkerError):
    pass


class UserActionBlocked(BrowserWorkerError):
    pass


def find_google_chrome() -> Path:
    override = (os.getenv("QCSCKP_CHROME_PATH") or "").strip()
    candidates = [Path(override)] if override else []
    for env_name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        root = os.getenv(env_name)
        if root:
            candidates.append(Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe")
    for candidate in candidates:
        if candidate.is_file() and candidate.name.lower() == "chrome.exe":
            return candidate.resolve()
    raise BrowserWorkerError("未找到本机 Google Chrome，请先安装 Chrome 或配置 QCSCKP_CHROME_PATH")


class NullReadTransport(ReadTransport):
    """用于不启动浏览器的 schema/API 单元测试。"""

    def request(self, method: str, endpoint_path: str, **_kwargs) -> dict[str, Any]:
        raise LoginRequired(f"browser_not_ready: {method} {endpoint_path}")


class PlaywrightBrowserWorker(ReadTransport):
    """在 JobWorker 线程中创建和使用；禁止从 UI/HTTP 线程直接调用。"""

    def __init__(
        self,
        database: RuntimeDatabase,
        writer: StorageWriter,
        guard: PlatformNetworkGuard,
    ):
        self.database = database
        self.writer = writer
        self.guard = guard
        self._owner_thread_id: int | None = None
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._tool_user_id: str | None = None

    def bind_owner_thread(self) -> None:
        current = threading.get_ident()
        if self._owner_thread_id is None:
            self._owner_thread_id = current
        elif self._owner_thread_id != current:
            raise BrowserWorkerError("Browser Worker ownership violation")

    def _assert_owner(self) -> None:
        self.bind_owner_thread()

    def _identity(self, tool_user_id: str) -> dict[str, Any]:
        identity = self.database.query_one(
            "SELECT * FROM qianchuan_identity WHERE tool_user_id=?",
            (tool_user_id,),
        )
        if not identity:
            raise LoginRequired("qianchuan_identity_missing")
        return identity

    def open_visible_login_and_capture(
        self,
        tool_user_id: str,
        *,
        require_account_selection: bool,
        timeout_seconds: int = 600,
        progress=None,
    ) -> dict[str, Any]:
        self._assert_owner()
        self.close()
        from playwright.sync_api import sync_playwright

        chrome = find_google_chrome()
        identity = self._identity(tool_user_id)
        profile_path = Path(str(identity["profile_path"]))
        profile_path.mkdir(parents=True, exist_ok=True)
        playwright = sync_playwright().start()
        context = None
        account_snapshot: dict[str, Any] | None = None
        try:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile_path),
                executable_path=str(chrome),
                headless=False,
                viewport=None,
                args=["--start-maximized"],
            )
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(QIANCHUAN_ORIGIN, wait_until="domcontentloaded", timeout=60_000)
            deadline = time.monotonic() + timeout_seconds
            last_message = "请在 Chrome 中登录千川"
            while time.monotonic() < deadline:
                if progress:
                    progress(0, 1, last_message)
                current_url = page.url
                if self._captcha_or_risk(page):
                    last_message = "检测到验证码或风控，请在 Chrome 中处理"
                    time.sleep(1)
                    continue
                aavid = self._aavid_from_url(current_url)
                if aavid:
                    account_snapshot = self._fetch_account_info_in_page(page, aavid)
                    if account_snapshot:
                        if require_account_selection:
                            break
                        last_message = "已识别登录状态，正在安全保存会话"
                        break
                if not require_account_selection and "login" not in current_url.lower():
                    # 登录主页未必带 aavid；页面进入千川域名即可保存会话，账户在后续添加时选择。
                    if urllib.parse.urlparse(current_url).netloc.endswith("jinritemai.com"):
                        break
                if require_account_selection:
                    last_message = "请切换到要添加的千川账户，并打开任意计划详情"
                time.sleep(0.75)
            else:
                raise UserActionBlocked("千川登录或账户选择超时")
            storage_state = context.storage_state()
            protected = protect_for_current_windows_user(
                json.dumps(storage_state, ensure_ascii=False, separators=(",", ":"))
            )
            now = utc_iso()
            self.writer.execute(
                """
                UPDATE qianchuan_identity
                SET encrypted_storage_state=?, cookie_updated_at=?,
                    login_status='authenticated', blocked_reason=NULL,
                    last_verified_at=?, updated_at=?
                WHERE tool_user_id=?
                """,
                (protected, now, now, now, tool_user_id),
            )
            if progress:
                progress(1, 1, "登录状态已保存，Chrome 即将自动关闭")
            return {
                "login_status": "authenticated",
                "chrome_path": str(chrome),
                "account": account_snapshot,
            }
        finally:
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass
            playwright.stop()

    def prepare_headless(self, tool_user_id: str) -> None:
        self._assert_owner()
        if self._page is not None and self._tool_user_id == tool_user_id:
            return
        self.close()
        identity = self._identity(tool_user_id)
        protected = str(identity.get("encrypted_storage_state") or "")
        if not protected:
            raise LoginRequired("千川登录状态不存在")
        try:
            storage_state = json.loads(unprotect_for_current_windows_user(protected))
        except Exception as exc:
            raise LoginRequired("千川登录状态无法解密，请重新登录") from exc
        from playwright.sync_api import sync_playwright

        chrome = find_google_chrome()
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(
            executable_path=str(chrome),
            headless=True,
        )
        self._context = self._browser.new_context(storage_state=storage_state)
        self._page = self._context.new_page()
        self._page.goto(QIANCHUAN_ORIGIN, wait_until="domcontentloaded", timeout=60_000)
        if self._captcha_or_risk(self._page):
            self._mark_login_blocked(tool_user_id, "captcha_or_risk")
            self.close()
            raise UserActionBlocked("检测到验证码或风控，请打开可见 Chrome 重新登录")
        self._tool_user_id = tool_user_id

    def request(
        self,
        method: str,
        endpoint_path: str,
        *,
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        timeout_seconds: float = 30.0,
    ) -> dict[str, Any]:
        self._assert_owner()
        self.guard.assert_allowed(method, endpoint_path)
        if self._page is None:
            raise LoginRequired("Browser Worker 尚未绑定工具用户")
        url = endpoint_path
        if query:
            url += "?" + urllib.parse.urlencode(
                {key: str(value) for key, value in query.items() if value is not None}
            )
        elif body and body.get("aavid"):
            url += "?" + urllib.parse.urlencode({"aavid": str(body["aavid"])})
        result: dict[str, Any] = {}
        for attempt in range(4):
            result = self._page.evaluate(
                """
                async ({url, method, body, timeoutMs}) => {
                  const controller = new AbortController();
                  const timer = setTimeout(() => controller.abort(), timeoutMs);
                  try {
                    const response = await fetch(url, {
                      method,
                      credentials: 'include',
                      headers: body ? {'content-type': 'application/json;charset=UTF-8'} : {},
                      body: body ? JSON.stringify(body) : undefined,
                      signal: controller.signal,
                    });
                    const text = await response.text();
                    return {
                      ok: response.ok,
                      status: response.status,
                      contentType: response.headers.get('content-type') || '',
                      retryAfter: response.headers.get('retry-after') || '',
                      text,
                    };
                  } finally {
                    clearTimeout(timer);
                  }
                }
                """,
                {
                    "url": url,
                    "method": method.upper(),
                    "body": body,
                    "timeoutMs": int(timeout_seconds * 1000),
                },
            )
            if int(result.get("status") or 0) != 429 or attempt == 3:
                break
            time.sleep(self.retry_delay_for_429(attempt, result.get("retryAfter")))
        text = str(result.get("text") or "")
        if result.get("status") in {401, 403} or "login" in str(result.get("contentType") or "").lower():
            if self._tool_user_id:
                self._mark_login_blocked(self._tool_user_id, "session_expired")
            raise LoginRequired("千川登录状态已失效")
        if not result.get("ok"):
            raise BrowserWorkerError(f"千川只读请求失败 HTTP {result.get('status')}")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            if "<html" in text.lower():
                if self._tool_user_id:
                    self._mark_login_blocked(self._tool_user_id, "session_expired")
                raise LoginRequired("千川返回登录页面，请重新登录") from exc
            raise BrowserWorkerError("schema_changed: 千川响应不是JSON") from exc
        return payload

    @staticmethod
    def retry_delay_for_429(attempt: int, retry_after: Any = None) -> float:
        """Honor a short Retry-After while keeping the Browser Worker responsive."""
        try:
            requested = float(str(retry_after).strip())
        except (TypeError, ValueError):
            requested = float(2**max(attempt, 0))
        return min(max(requested, 0.1), 10.0)

    def close(self) -> None:
        # 只允许所有者线程主动关闭；进程退出兜底时对象可能尚未绑定。
        if self._owner_thread_id is not None and self._owner_thread_id != threading.get_ident():
            return
        for obj in (self._page, self._context, self._browser):
            if obj is not None:
                try:
                    obj.close()
                except Exception:
                    pass
        self._page = self._context = self._browser = None
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                pass
        self._playwright = None
        self._tool_user_id = None

    def _mark_login_blocked(self, tool_user_id: str, reason: str) -> None:
        self.writer.execute(
            """
            UPDATE qianchuan_identity
            SET login_status='blocked', blocked_reason=?, updated_at=?
            WHERE tool_user_id=?
            """,
            (reason, utc_iso(), tool_user_id),
        )

    @staticmethod
    def _aavid_from_url(url: str) -> str | None:
        parsed = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(parsed.query)
        for key in ("aavid", "aadvid", "advId"):
            value = (query.get(key) or [None])[0]
            if value and str(value).isdigit():
                return str(value)
        match = re.search(r"(?:aavid|aadvid|advId)[=/](\d{6,})", url)
        return match.group(1) if match else None

    @staticmethod
    def _captcha_or_risk(page) -> bool:
        try:
            text = page.locator("body").inner_text(timeout=1000)[:5000]
        except Exception:
            return False
        lowered = text.lower()
        return any(
            marker in lowered
            for marker in ("验证码", "安全验证", "访问过于频繁", "risk verification")
        )

    @staticmethod
    def _fetch_account_info_in_page(page, aavid: str) -> dict[str, Any] | None:
        result = page.evaluate(
            """
            async ({aavid}) => {
              const response = await fetch(`/ad/api/v1/account/user/info?aavid=${encodeURIComponent(aavid)}`, {credentials:'include'});
              if (!response.ok) return null;
              const payload = await response.json();
              return payload?.data?.accountInfo || null;
            }
            """,
            {"aavid": aavid},
        )
        if not isinstance(result, dict):
            return None
        if str(result.get("advId") or "") != aavid or not str(result.get("advName") or "").strip():
            return None
        return {"aavid": aavid, "account_name": str(result["advName"]).strip()}
