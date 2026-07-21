"""
飞书自定义机器人 Webhook（群机器人）。

文档：https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot

对外能力（见 ``FeishuWebhook``）：
- **纯文本** ``msg_type: text``
- **富文本** ``msg_type: post``
- **Markdown 卡片** ``msg_type: interactive`` + schema 2.0，正文为 Markdown
- **Markdown 表格卡片**：在卡片内渲染管道表格（由 ``build_markdown_table`` 生成）

其它类型请用 ``send_raw`` 自行组 JSON。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Optional, Sequence, Tuple, Union
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

WEBHOOK_BASE_FEISHU_CN = "https://open.feishu.cn/open-apis/bot/v2/hook/"
WEBHOOK_BASE_LARK_INTL = "https://open.larksuite.com/open-apis/bot/v2/hook/"
DEFAULT_HOOK_BASE_CN = WEBHOOK_BASE_FEISHU_CN

PresetRegion = Literal["cn", "intl"]


class FeishuBotError(Exception):
    def __init__(
        self,
        message: str,
        *,
        http_status: Optional[int] = None,
        code: Optional[int] = None,
        raw_body: Optional[str] = None,
    ):
        super().__init__(message)
        self.http_status = http_status
        self.code = code
        self.raw_body = raw_body


@dataclass
class WebhookSendResult:
    ok: bool
    data: Dict[str, Any]
    error: Optional[str] = None
    http_status: Optional[int] = None
    code: Optional[int] = None


def _decode_body(raw: bytes) -> str:
    if not raw:
        return ""
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _preset_base(preset: PresetRegion) -> str:
    return WEBHOOK_BASE_LARK_INTL if preset == "intl" else WEBHOOK_BASE_FEISHU_CN


def normalize_webhook_url(
    hook_token_or_url: str,
    *,
    base_url: Optional[str] = None,
    preset: PresetRegion = "cn",
) -> str:
    s = (hook_token_or_url or "").strip()
    if not s:
        raise ValueError("webhook 地址或 token 不能为空")
    resolved_base = (base_url or _preset_base(preset)).rstrip("/") + "/"
    if "://" not in s:
        return resolved_base + s.lstrip("/")
    low = s.lower()
    if low.startswith("//"):
        s = "https:" + s
    elif not (low.startswith("http://") or low.startswith("https://")):
        s = "https://" + s.lstrip("/")
    return s.strip()


def _coerce_int(v: Any) -> Optional[int]:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, str) and v.strip().lstrip("-").isdigit():
        try:
            return int(v.strip())
        except ValueError:
            return None
    return None


def _parse_api_code(v: Any) -> Optional[int]:
    c = _coerce_int(v)
    if c is not None:
        return c
    if isinstance(v, str):
        s = v.strip()
        try:
            x = float(s)
            if x.is_integer():
                return int(x)
        except ValueError:
            pass
    return None


def _parse_success(
    body: str,
    http_status: int,
    *,
    strict: bool,
) -> Tuple[bool, Dict[str, Any], Optional[int], Optional[str]]:
    if http_status < 200 or http_status >= 300:
        return False, {}, None, f"HTTP {http_status}"
    if not body or not body.strip():
        return True, {}, None, None
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        if strict:
            return False, {}, None, "响应非 JSON"
        if body.strip().lower() in ("ok", "success", "0"):
            return True, {"_raw": body}, None, None
        return False, {}, None, f"响应非 JSON: {body[:300]}"
    if not isinstance(parsed, dict):
        return False, {}, None, "响应 JSON 非对象"
    if "code" in parsed:
        c = _parse_api_code(parsed.get("code"))
        if c is None:
            msg = "无法解析 code 字段"
            return (False, parsed, None, msg) if strict else (True, parsed, None, None)
        if c != 0:
            return False, parsed, c, str(parsed.get("msg") or parsed.get("message") or parsed)
        return True, parsed, c, None
    if "StatusCode" in parsed:
        c = _parse_api_code(parsed.get("StatusCode"))
        if c is None:
            return (False, parsed, None, "无法解析 StatusCode") if strict else (True, parsed, None, None)
        if c != 0:
            return False, parsed, c, str(parsed.get("StatusMessage") or parsed)
        return True, parsed, c, None
    if "success" in parsed and isinstance(parsed["success"], bool):
        if not parsed["success"]:
            return False, parsed, None, str(parsed.get("message") or parsed.get("msg") or "success=false")
        return True, parsed, None, None
    if strict:
        return False, parsed, None, "响应缺少可识别的成功字段"
    return True, parsed, None, None


def _should_retry_http(status: Optional[int]) -> bool:
    if status is None:
        return False
    return status in (408, 425, 429, 500, 502, 503, 504)


def _clip_str(s: str, max_len: int) -> str:
    s = s.strip()
    if max_len <= 0:
        return ""
    if len(s) <= max_len:
        return s
    return s[: max_len - 1] + "…"


def escape_markdown_table_cell(value: Any) -> str:
    s = str(value).replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    return s.replace("|", "\\|")


def build_markdown_table(
    headers: Sequence[str],
    rows: Sequence[Sequence[Any]],
    *,
    max_rows: int = 200,
) -> str:
    """生成 Markdown 管道表（用于卡片正文或自行拼接）。"""
    if not headers:
        raise ValueError("headers 不能为空")
    hs = [escape_markdown_table_cell(h) for h in headers]
    n = len(hs)
    sep = "| " + " | ".join("---" for _ in range(n)) + " |"
    head = "| " + " | ".join(hs) + " |"
    lines = [head, sep]
    for row in rows[:max_rows]:
        cells = list(row[:n]) + [""] * max(0, n - len(row))
        lines.append("| " + " | ".join(escape_markdown_table_cell(c) for c in cells) + " |")
    if len(rows) > max_rows:
        note = f"*（共 {len(rows)} 行，仅展示前 {max_rows} 行）*"
        note_cells = [note] + [""] * (n - 1)
        lines.append("| " + " | ".join(note_cells) + " |")
    return "\n".join(lines)


def build_interactive_markdown_card(
    title: str,
    markdown_content: str,
    *,
    subtitle: str = "",
    template: str = "blue",
) -> Dict[str, Any]:
    """schema 2.0 卡片，``body.elements`` 内一条 markdown。"""
    return {
        "schema": "2.0",
        "config": {
            "update_multi": True,
            "style": {
                "text_size": {
                    "normal_v2": {
                        "default": "normal",
                        "pc": "normal",
                        "mobile": "heading",
                    }
                }
            },
        },
        "body": {
            "direction": "vertical",
            "padding": "12px 12px 12px 12px",
            "elements": [
                {
                    "tag": "markdown",
                    "content": _clip_str(markdown_content, 20000),
                    "text_align": "left",
                    "text_size": "normal_v2",
                    "margin": "0px 0px 0px 0px",
                },
            ],
        },
        "header": {
            "title": {"tag": "plain_text", "content": _clip_str(title, 200)},
            "subtitle": {"tag": "plain_text", "content": _clip_str(subtitle, 500)},
            "template": template,
            "padding": "12px 12px 12px 12px",
        },
    }


def build_markdown_table_card(
    title: str,
    headers: Sequence[str],
    rows: Sequence[Sequence[Any]],
    *,
    template: str = "blue",
    max_rows: int = 200,
    subtitle: str = "",
    intro_markdown: Optional[str] = None,
) -> Dict[str, Any]:
    """卡片标题 + 可选说明 + Markdown 表格。"""
    parts: List[str] = []
    if intro_markdown:
        parts.append(intro_markdown.strip())
    parts.append(build_markdown_table(headers, rows, max_rows=max_rows))
    body_md = "\n\n".join(parts)[:20000]
    return build_interactive_markdown_card(title, body_md, subtitle=subtitle, template=template)


class FeishuWebhook:
    """飞书 Webhook 客户端：纯文本、富文本、Markdown 卡片、Markdown 表格卡片。"""

    def __init__(
        self,
        hook_token_or_url: str,
        *,
        preset: PresetRegion = "cn",
        base_url: Optional[str] = None,
        timeout: float = 15.0,
        retries: int = 0,
        retry_backoff_sec: float = 0.4,
        strict_response: bool = False,
    ):
        self.webhook_url = normalize_webhook_url(hook_token_or_url, base_url=base_url, preset=preset)
        self.timeout = timeout
        self.retries = max(0, int(retries))
        self.retry_backoff_sec = max(0.0, float(retry_backoff_sec))
        self.strict_response = strict_response

    def _post_once(self, payload: Dict[str, Any]) -> Tuple[int, str]:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = Request(
            self.webhook_url,
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": "qianchuan-promotion-crawl/FeishuWebhook",
            },
        )
        try:
            with urlopen(req, timeout=self.timeout) as resp:
                body = _decode_body(resp.read())
                status = getattr(resp, "status", 200)
                return status, body
        except HTTPError as e:
            body = _decode_body(e.read()) if e.fp else ""
            raise FeishuBotError(body or str(e), http_status=e.code, raw_body=body or None) from e
        except URLError as e:
            reason = getattr(e.reason, "strerror", None) or str(e.reason)
            raise FeishuBotError(f"网络错误: {reason}") from e

    def _post_with_retries(self, payload: Dict[str, Any]) -> Tuple[int, str]:
        last_err: Optional[Exception] = None
        for attempt in range(self.retries + 1):
            try:
                return self._post_once(payload)
            except FeishuBotError as e:
                last_err = e
                retriable = (e.http_status is not None and _should_retry_http(e.http_status)) or (
                    e.http_status is None and "网络" in str(e)
                )
                if not retriable or attempt >= self.retries:
                    raise
                delay = self.retry_backoff_sec * (2**attempt)
                if delay > 0:
                    time.sleep(delay)
        assert last_err is not None
        raise last_err

    def _finalize(
        self,
        http_status: int,
        body: str,
        *,
        raise_on_error: bool,
    ) -> Union[Dict[str, Any], WebhookSendResult]:
        ok, data, code, err = _parse_success(body, http_status, strict=self.strict_response)
        if ok:
            if raise_on_error:
                return data
            return WebhookSendResult(ok=True, data=data, http_status=http_status, code=code)
        if raise_on_error:
            raise FeishuBotError(
                err or "飞书机器人返回失败",
                http_status=http_status,
                code=code,
                raw_body=body[:2000] if body else None,
            )
        return WebhookSendResult(
            ok=False,
            data=data,
            error=err or "unknown",
            http_status=http_status,
            code=code,
        )

    def send_raw(
        self,
        payload: Dict[str, Any],
        *,
        raise_on_error: bool = True,
    ) -> Union[Dict[str, Any], WebhookSendResult]:
        status, body = self._post_with_retries(payload)
        return self._finalize(status, body, raise_on_error=raise_on_error)

    def try_send_raw(self, payload: Dict[str, Any]) -> WebhookSendResult:
        try:
            r = self.send_raw(payload, raise_on_error=False)
            assert isinstance(r, WebhookSendResult)
            return r
        except FeishuBotError as e:
            return WebhookSendResult(
                ok=False,
                data={},
                error=str(e),
                http_status=e.http_status,
                code=e.code,
            )

    # --- 纯文本 ---
    def send_text(self, text: str) -> Dict[str, Any]:
        r = self.send_raw({"msg_type": "text", "content": {"text": text}}, raise_on_error=True)
        assert isinstance(r, dict)
        return r

    def try_send_text(self, text: str) -> WebhookSendResult:
        return self.try_send_raw({"msg_type": "text", "content": {"text": text}})

    # --- 富文本 post ---
    def send_post(self, post: Dict[str, Any], *, locale_key: str = "zh_cn") -> Dict[str, Any]:
        """``post`` 为 ``content.post.<locale_key>`` 对象（标题、段落等）。"""
        r = self.send_raw(
            {"msg_type": "post", "content": {"post": {locale_key: post}}},
            raise_on_error=True,
        )
        assert isinstance(r, dict)
        return r

    def try_send_post(self, post: Dict[str, Any], *, locale_key: str = "zh_cn") -> WebhookSendResult:
        return self.try_send_raw({"msg_type": "post", "content": {"post": {locale_key: post}}})

    # --- Markdown 卡片（schema 2.0）---
    def send_markdown(
        self,
        title: str,
        markdown: str,
        *,
        subtitle: str = "",
        template: str = "blue",
    ) -> Dict[str, Any]:
        card = build_interactive_markdown_card(title, markdown, subtitle=subtitle, template=template)
        r = self.send_raw({"msg_type": "interactive", "card": card}, raise_on_error=True)
        assert isinstance(r, dict)
        return r

    def try_send_markdown(
        self,
        title: str,
        markdown: str,
        *,
        subtitle: str = "",
        template: str = "blue",
    ) -> WebhookSendResult:
        card = build_interactive_markdown_card(title, markdown, subtitle=subtitle, template=template)
        return self.try_send_raw({"msg_type": "interactive", "card": card})

    # --- Markdown 表格（卡片内）---
    def send_markdown_table(
        self,
        title: str,
        headers: Sequence[str],
        rows: Sequence[Sequence[Any]],
        *,
        template: str = "blue",
        max_rows: int = 200,
        subtitle: str = "",
        intro_markdown: Optional[str] = None,
    ) -> Dict[str, Any]:
        card = build_markdown_table_card(
            title,
            headers,
            rows,
            template=template,
            max_rows=max_rows,
            subtitle=subtitle,
            intro_markdown=intro_markdown,
        )
        r = self.send_raw({"msg_type": "interactive", "card": card}, raise_on_error=True)
        assert isinstance(r, dict)
        return r

    def try_send_markdown_table(
        self,
        title: str,
        headers: Sequence[str],
        rows: Sequence[Sequence[Any]],
        *,
        template: str = "blue",
        max_rows: int = 200,
        subtitle: str = "",
        intro_markdown: Optional[str] = None,
    ) -> WebhookSendResult:
        card = build_markdown_table_card(
            title,
            headers,
            rows,
            template=template,
            max_rows=max_rows,
            subtitle=subtitle,
            intro_markdown=intro_markdown,
        )
        return self.try_send_raw({"msg_type": "interactive", "card": card})

    def send_interactive(self, card: Dict[str, Any]) -> Dict[str, Any]:
        """自定义 ``msg_type: interactive`` 的 ``card`` JSON。"""
        r = self.send_raw({"msg_type": "interactive", "card": card}, raise_on_error=True)
        assert isinstance(r, dict)
        return r


# 旧项目中的方法名兼容
FeishuWebhook.send_table = FeishuWebhook.send_markdown_table
FeishuWebhook.try_send_table = FeishuWebhook.try_send_markdown_table
FeishuWebhook.send_markdown_card = FeishuWebhook.send_markdown
FeishuWebhook.try_send_markdown_card = FeishuWebhook.try_send_markdown

FeishuBotHook = FeishuWebhook


def send_text(
    hook_token_or_url: str,
    text: str,
    *,
    preset: PresetRegion = "cn",
    base_url: Optional[str] = None,
    timeout: float = 15.0,
) -> Dict[str, Any]:
    return FeishuWebhook(hook_token_or_url, preset=preset, base_url=base_url, timeout=timeout).send_text(text)


def try_send_text(
    hook_token_or_url: str,
    text: str,
    *,
    preset: PresetRegion = "cn",
    base_url: Optional[str] = None,
    timeout: float = 15.0,
    retries: int = 0,
    retry_backoff_sec: float = 0.4,
    strict_response: bool = False,
) -> WebhookSendResult:
    return FeishuWebhook(
        hook_token_or_url,
        preset=preset,
        base_url=base_url,
        timeout=timeout,
        retries=retries,
        retry_backoff_sec=retry_backoff_sec,
        strict_response=strict_response,
    ).try_send_text(text)
