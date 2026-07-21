"""
钉钉自定义机器人 Webhook（群机器人）。

文档：https://open.dingtalk.com/document/orgapp/custom-bot-creation-and-installation

对外能力（见 ``DingtalkWebhook``）：
- **Markdown**：``msgtype: markdown``，``title`` + ``text``
- 多列数据使用 **卡片列表**（分隔线 + 小标题 + 字段列表），避免管道表在钉钉里被压成竖排单字。
- **标题**：与飞书卡片主标题一致请用 ``build_dingtalk_notification_title``（勿用 ``[关键词]`` 形式，易被钉钉拆成两段样式）。
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from hook.feishu_bot import WebhookSendResult, _decode_body, _should_retry_http


def build_dingtalk_notification_title(keyword: str) -> str:
    """
    钉钉消息标题（对应飞书卡片 header.title 那一行）。

    与 ``utils.common.build_webhook_push_title`` 同源，但去掉 ``[关键词] `` 形式，
    改为 ``关键词 · 主标题``，避免钉钉把括号内外渲染成两段样式。
    """
    from utils.common import build_webhook_push_title

    raw = build_webhook_push_title(keyword).strip()
    if len(raw) >= 2 and raw[0] == "[" and "] " in raw:
        end = raw.index("] ")
        kw_part = raw[1:end].strip()
        rest = raw[end + 2 :].strip()
        if kw_part:
            out = f"{kw_part} · {rest}"
        else:
            out = rest
    else:
        out = raw
    return out[:128]


def _dingtalk_cell_plain(v: Any) -> str:
    s = str(v).replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    return s.replace("\t", " ").strip()


def _dingtalk_safe_md_value(s: str, *, max_len: int = 480) -> str:
    """避免破坏 Markdown 的 *、行首 #，并限制单字段长度。"""
    t = _dingtalk_cell_plain(s).replace("*", "·")
    if t.startswith("#"):
        t = "·" + t.lstrip("#")
    if len(t) > max_len:
        t = t[: max_len - 1] + "…"
    return t


def build_dingtalk_card_markdown(
    headers: Sequence[str],
    rows: Sequence[Sequence[Any]],
    *,
    max_rows: int = 200,
) -> str:
    """卡片式 Markdown：每条记录一块，适合手机端阅读。"""
    hs = [_dingtalk_cell_plain(h) for h in headers]
    n = len(hs)
    if n == 0:
        return "_（无列）_"
    if not rows:
        return "_（无数据）_"

    blocks: List[str] = []
    for i, row in enumerate(rows[:max_rows]):
        cells = list(row[:n]) + [""] * max(0, n - len(row))
        lines = [
            "---",
            f"#### Top.{i + 1}",
            "",
        ]
        for h, c in zip(hs, cells):
            hn = _dingtalk_safe_md_value(h, max_len=64)
            lines.append(f"- **{hn}**：{_dingtalk_safe_md_value(str(c))}")
        blocks.append("\n".join(lines))

    body = "\n\n".join(blocks)
    if len(rows) > max_rows:
        body += f"\n\n*共 {len(rows)} 条，仅展示前 {max_rows} 条。*"
    return body


def format_dingtalk_push_body(
    headers: Sequence[str],
    rows: Sequence[Sequence[Any]],
    *,
    headline: str = "",
    data_time_line: str = "",
    subtitle: str = "",
    intro_markdown: Optional[str] = None,
    max_rows: int = 200,
) -> str:
    """
    组装钉钉推送正文。

    - 业务推送（千川 Top15）：传入 headline + data_time_line，顶部为两行说明，再跟 Top.1.. 数据块。
    - 联调/旧用法：仅传 subtitle / intro_markdown 时，仍为引用副标题 + 引言 + 卡片。
    """
    parts: List[str] = []
    if (headline or "").strip():
        parts.append((headline or "").strip())
        if (data_time_line or "").strip():
            parts.append((data_time_line or "").strip())
    else:
        if subtitle:
            parts.append(f"> **{subtitle.strip()}**")
        if intro_markdown:
            parts.append(intro_markdown.strip())
    parts.append(build_dingtalk_card_markdown(headers, rows, max_rows=max_rows))
    return "\n\n".join(parts)[:20000]


class DingtalkBotError(Exception):
    def __init__(
        self,
        message: str,
        *,
        http_status: Optional[int] = None,
        errcode: Optional[int] = None,
        raw_body: Optional[str] = None,
    ):
        super().__init__(message)
        self.http_status = http_status
        self.errcode = errcode
        self.raw_body = raw_body


def normalize_dingtalk_webhook_url(hook_token_or_url: str) -> str:
    s = (hook_token_or_url or "").strip()
    if not s:
        raise ValueError("webhook 地址或 access_token 不能为空")
    if "://" not in s:
        return "https://oapi.dingtalk.com/robot/send?access_token=" + s.lstrip("/")
    low = s.lower()
    if low.startswith("//"):
        s = "https:" + s
    elif not (low.startswith("http://") or low.startswith("https://")):
        s = "https://" + s.lstrip("/")
    return s.strip()


def _parse_dingtalk_body(body: str, http_status: int) -> Tuple[bool, Dict[str, Any], Optional[str]]:
    if http_status < 200 or http_status >= 300:
        return False, {}, f"HTTP {http_status}"
    if not body or not body.strip():
        return True, {}, None
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return False, {}, f"响应非 JSON: {body[:300]}"
    if not isinstance(parsed, dict):
        return False, {}, "响应 JSON 非对象"
    errcode = parsed.get("errcode")
    try:
        ec = int(errcode) if errcode is not None else 0
    except (TypeError, ValueError):
        ec = -1
    if ec != 0:
        return False, parsed, str(parsed.get("errmsg") or body)
    return True, parsed, None


class DingtalkWebhook:
    """钉钉 Webhook：Markdown（卡片列表；勿用多列管道表）。"""

    def __init__(
        self,
        hook_token_or_url: str,
        *,
        timeout: float = 15.0,
        retries: int = 0,
        retry_backoff_sec: float = 0.4,
    ):
        self.webhook_url = normalize_dingtalk_webhook_url(hook_token_or_url)
        self.timeout = timeout
        self.retries = max(0, int(retries))
        self.retry_backoff_sec = max(0.0, float(retry_backoff_sec))

    def _post_once(self, payload: Dict[str, Any]) -> Tuple[int, str]:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = Request(
            self.webhook_url,
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": "qianchuan-promotion-crawl/DingtalkWebhook",
            },
        )
        try:
            with urlopen(req, timeout=self.timeout) as resp:
                body = _decode_body(resp.read())
                status = getattr(resp, "status", 200)
                return status, body
        except HTTPError as e:
            body = _decode_body(e.read()) if e.fp else ""
            raise DingtalkBotError(body or str(e), http_status=e.code, raw_body=body or None) from e
        except URLError as e:
            reason = getattr(e.reason, "strerror", None) or str(e.reason)
            raise DingtalkBotError(f"网络错误: {reason}") from e

    def _post_with_retries(self, payload: Dict[str, Any]) -> Tuple[int, str]:
        last_err: Optional[Exception] = None
        for attempt in range(self.retries + 1):
            try:
                return self._post_once(payload)
            except DingtalkBotError as e:
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
        ok, data, err = _parse_dingtalk_body(body, http_status)
        if ok:
            if raise_on_error:
                return data
            return WebhookSendResult(ok=True, data=data, http_status=http_status, code=0)
        if raise_on_error:
            raise DingtalkBotError(
                err or "钉钉机器人返回失败",
                http_status=http_status,
                errcode=data.get("errcode") if isinstance(data, dict) else None,
                raw_body=body[:2000] if body else None,
            )
        errcode = None
        if isinstance(data, dict) and data.get("errcode") is not None:
            try:
                errcode = int(data["errcode"])
            except (TypeError, ValueError):
                errcode = None
        return WebhookSendResult(
            ok=False,
            data=data if isinstance(data, dict) else {},
            error=err or "unknown",
            http_status=http_status,
            code=errcode,
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
        except DingtalkBotError as e:
            return WebhookSendResult(
                ok=False,
                data={},
                error=str(e),
                http_status=e.http_status,
                code=e.errcode,
            )

    def try_send_markdown(self, title: str, text: str) -> WebhookSendResult:
        title = (title or "").strip()[:128]
        text = (text or "").strip()[:20000]
        payload = {
            "msgtype": "markdown",
            "markdown": {"title": title, "text": text},
        }
        return self.try_send_raw(payload)

    def try_send_markdown_table(
        self,
        title: str,
        headers: Sequence[str],
        rows: Sequence[Sequence[Any]],
        *,
        max_rows: int = 200,
        headline: str = "",
        data_time_line: str = "",
        subtitle: str = "",
        intro_markdown: Optional[str] = None,
    ) -> WebhookSendResult:
        """兼容旧调用名；内部为卡片列表，非 Markdown 表格。"""
        text = format_dingtalk_push_body(
            headers,
            rows,
            headline=headline,
            data_time_line=data_time_line,
            subtitle=subtitle,
            intro_markdown=intro_markdown,
            max_rows=max_rows,
        )
        return self.try_send_markdown(title, text)


DingtalkBotHook = DingtalkWebhook
