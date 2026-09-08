"""巨量千川 Open API HTTP 客户端。

GET 可针对网络、429、5xx 做有界退避；POST 从不盲目重试。POST 在已发出后没有
得到确定响应时抛 ``ApiWriteOutcomeUnknown``，必须由业务层查询调控任务和操作日志
对账。
"""

from __future__ import annotations

import json
import inspect
import random
import socket
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from config import QIANCHUAN_OFFICIAL_API_BASE_URL
from .errors import (
    ApiPermissionError,
    ApiRateLimitError,
    ApiRequestError,
    ApiTokenError,
    ApiWriteOutcomeUnknown,
)
from .token_provider import TokenProvider, get_default_token_provider
from .collection_context import CollectionContext, current_collection_context, request_fingerprint, use_collection_context
from .managed_workers import run_bounded
from .pagination_evidence import help_evidence, page_evidence, safe_error_message


@dataclass(frozen=True)
class ApiResponse:
    data: Any
    raw: Mapping[str, Any]
    request_id: str
    code: str = "0"
    message: str = ""
    request_uid: str = ""


def _redact(value: Any) -> Any:
    secret_keys = {
        "access_token",
        "refresh_token",
        "app_secret",
        "secret",
        "authorization",
        "access-token",
    }
    if isinstance(value, Mapping):
        return {
            str(key): ("<redacted>" if str(key).lower() in secret_keys else _redact(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


DEFAULT_ENDPOINT_QPS: dict[str, float] = {
    "/open_api/oauth2/advertiser/get/": 1.0,
    "/open_api/v1.0/qianchuan/shop/advertiser/list/": 2.0,
    "/open_api/2/ebp/advertiser/list/": 2.0,
    "/open_api/2/advertiser/public_info/": 2.0,
    "/open_api/v1.0/qianchuan/uni_promotion/list/": 15.0,
    "/open_api/v1.0/qianchuan/uni_promotion/ad/detail/": 6.0,
    "/open_api/v1.0/qianchuan/uni_promotion/ad/material/get/": 6.0,
    "/open_api/v1.0/qianchuan/uni_promotion/ad/product/get/": 10.0,
    "/open_api/v1.0/qianchuan/report/uni_promotion/config/get/": 3.0,
    "/open_api/v1.0/qianchuan/report/uni_promotion/data/get/": 20.0,
    "/open_api/v1.0/qianchuan/uni_promotion/ad/control_task/list/": 4.0,
    "/open_api/v1.0/qianchuan/tools/log_search/": 10.0,
    "/open_api/v1.0/qianchuan/uni_promotion/ad/control_task/create/": 1.0,
    "/open_api/v1.0/qianchuan/uni_promotion/ad/control_task/update/": 1.0,
    "/open_api/v1.0/qianchuan/uni_promotion/ad/control_task/status/update/": 1.0,
    "/open_api/v1.0/qianchuan/uni_promotion/ad/control_task/budget/update/": 1.0,
    "/open_api/v1.0/qianchuan/uni_promotion/ad/control_task/duration/update/": 1.0,
}


class EndpointRateLimiter:
    """按已核实的官方配额分层节流，并预留安全余量。"""

    def __init__(
        self,
        requests_per_second: float = 12.0,
        *,
        account_requests_per_second: float = 2.0,
        endpoint_requests_per_second: float = 4.0,
        endpoint_requests_per_second_map: Optional[Mapping[str, float]] = None,
    ) -> None:
        self.interval = 1.0 / max(0.1, float(requests_per_second))
        self.account_interval = 1.0 / max(
            0.1, float(account_requests_per_second)
        )
        self.endpoint_interval = 1.0 / max(
            0.1, float(endpoint_requests_per_second)
        )
        endpoint_qps = dict(DEFAULT_ENDPOINT_QPS)
        endpoint_qps.update(
            {
                str(key): float(value)
                for key, value in dict(
                    endpoint_requests_per_second_map or {}
                ).items()
                if str(key) and float(value) > 0
            }
        )
        self.endpoint_intervals = {
            key: 1.0 / max(0.1, value)
            for key, value in endpoint_qps.items()
        }
        self._next: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, key: str) -> None:
        with self._lock:
            now = time.monotonic()
            due = self._next.get(key, now)
            delay = max(0.0, due - now)
            self._next[key] = max(now, due) + self.interval
        if delay:
            ctx = current_collection_context()
            ctx.wait(delay, "rate_limit") if ctx is not None else time.sleep(delay)

    def wait_for_request(self, endpoint: str, advertiser_id: Any = "") -> None:
        """Atomically reserve all applicable quota lanes for one request."""
        account = str(advertiser_id or "").strip()
        endpoint_interval = self.endpoint_intervals.get(
            str(endpoint or ""), self.endpoint_interval
        )
        lanes: list[tuple[str, float]] = [
            ("application", self.interval),
            (f"endpoint:{endpoint}", endpoint_interval),
        ]
        if account:
            lanes.append((f"account:{account}", self.account_interval))
        with self._lock:
            now = time.monotonic()
            due = max((self._next.get(key, now) for key, _ in lanes), default=now)
            delay = max(0.0, due - now)
            for key, interval in lanes:
                self._next[key] = due + interval
        if delay:
            ctx = current_collection_context()
            ctx.wait(delay, "rate_limit") if ctx is not None else time.sleep(delay)


class QianchuanOpenApiClient:
    def __init__(
        self,
        token_provider: Optional[TokenProvider] = None,
        *,
        base_url: str = QIANCHUAN_OFFICIAL_API_BASE_URL,
        timeout: float = 30.0,
        max_get_attempts: int = 4,
        rate_limiter: Optional[EndpointRateLimiter] = None,
        audit_sink: Optional[Callable[[dict[str, Any]], None]] = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.token_provider = token_provider or get_default_token_provider()
        self.base_url = str(base_url or "").rstrip("/")
        self.timeout = max(1.0, float(timeout))
        self.max_get_attempts = max(1, int(max_get_attempts))
        self.rate_limiter = rate_limiter or EndpointRateLimiter()
        self.audit_sink = audit_sink
        self._sleep = sleep

    @staticmethod
    def _query_value(value: Any) -> str:
        if isinstance(value, (list, tuple, dict)):
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        return str(value)

    def _url(self, endpoint: str, query: Optional[Mapping[str, Any]]) -> str:
        if not str(endpoint).startswith("/open_api/"):
            raise ValueError("官方 API endpoint 必须以 /open_api/ 开头")
        pairs = []
        for key, value in (query or {}).items():
            if value is None or value == "":
                continue
            pairs.append((str(key), self._query_value(value)))
        suffix = "?" + urlencode(pairs) if pairs else ""
        return self.base_url + endpoint + suffix

    @staticmethod
    def _decode(raw: bytes) -> Mapping[str, Any]:
        try:
            value = json.loads(raw.decode("utf-8-sig")) if raw else {}
        except Exception as exc:
            raise ApiRequestError("千川官方 API 返回了无法解析的响应") from exc
        if not isinstance(value, Mapping):
            raise ApiRequestError("千川官方 API 响应不是对象")
        return value

    @staticmethod
    def _request_id(payload: Mapping[str, Any], headers: Any = None) -> str:
        for key in ("request_id", "requestId", "log_id", "logId"):
            if payload.get(key):
                return str(payload[key])
        data = payload.get("data")
        if isinstance(data, Mapping):
            for key in ("request_id", "requestId", "log_id", "logId"):
                if data.get(key):
                    return str(data[key])
        if headers is not None:
            for key in ("X-Tt-Logid", "X-Request-Id", "x-tt-logid"):
                try:
                    value = headers.get(key)
                except Exception:
                    value = None
                if value:
                    return str(value)
        return ""

    @staticmethod
    def _is_token_error(code: str, message: str) -> bool:
        text = f"{code} {message}".lower()
        return any(word in text for word in ("access_token", "access token", "token expired", "token失效", "token过期"))

    @staticmethod
    def _is_permission_error(code: str, message: str) -> bool:
        text = f"{code} {message}".lower()
        return any(word in text for word in ("permission", "无权限", "权限未开通", "not authorized", "unauthorized"))

    @staticmethod
    def _is_transient_service_error(code: str, message: str) -> bool:
        """Return whether a successful HTTP response describes a transient fault.

        Ocean Engine sometimes returns HTTP 200 with a non-zero business code
        for a temporary service failure.  These responses are safe to retry for
        GET requests only; POST requests must still be reconciled instead of
        being sent again.
        """

        text = f"{code} {message}".lower()
        return str(code or "").strip().startswith("5") or any(
            phrase in text
            for phrase in (
                "系统开小差",
                "稍后重试",
                "服务繁忙",
                "服务异常",
                "下游依赖服务相关错误",
                "system busy",
                "service unavailable",
                "temporarily unavailable",
                "internal server error",
            )
        )

    def _raise_api_error(
        self,
        payload: Mapping[str, Any],
        *,
        endpoint: str,
        http_status: Optional[int] = None,
        headers: Any = None,
    ) -> None:
        code = str(payload.get("code") or payload.get("err_no") or "")
        message = safe_error_message(payload.get("message") or payload.get("msg") or "千川官方 API 请求失败")
        request_id = self._request_id(payload, headers)
        retry_after = 0.0
        if headers is not None:
            try:
                retry_after = float(headers.get("Retry-After") or 0)
            except (TypeError, ValueError, AttributeError):
                retry_after = 0.0
        details = help_evidence(payload.get("help_message"))
        if help_evidence(message).get("parameter_error"):
            details["parameter_error"] = True
        kwargs = {
            "code": code,
            "request_id": request_id,
            "endpoint": endpoint,
            "http_status": http_status,
            "retry_after": retry_after,
            "help_message": details,
        }
        lowered_message = message.lower()
        explicit_rate_limit = any(
            marker in lowered_message
            for marker in (
                "rate limit",
                "rate_limit",
                "too many requests",
                "request frequency exceeded",
            )
        ) or any(
            marker in message
            for marker in ("频控", "请求频繁", "请求过于频繁", "频率超限")
        )
        if http_status == 429 or code == "40110" or explicit_rate_limit:
            raise ApiRateLimitError(message, **kwargs)
        if http_status == 401:
            raise ApiTokenError(message, **kwargs)
        if self._is_token_error(code, message):
            raise ApiTokenError(message, **kwargs)
        if http_status == 403 or self._is_permission_error(code, message):
            raise ApiPermissionError("千川官方 API 权限未开通或账户未授权", **kwargs)
        raise ApiRequestError(message, **kwargs)

    def _audit(self, event: dict[str, Any]) -> None:
        if not self.audit_sink:
            return
        try:
            self.audit_sink(_redact(event))
        except Exception:
            # 审计失败不能改变平台请求结果；调用方的业务流水仍会记录。
            return

    def request(
        self, method: str, endpoint: str, *, query=None, body=None, advertiser_id="", before_send=None
    ) -> ApiResponse:
        verb = str(method or "GET").upper()
        if verb == "POST":
            # Keep the established one-send / unknown-outcome write contract.
            return self._request_post_legacy(verb, endpoint, query=query, body=body,
                                             advertiser_id=advertiser_id, before_send=before_send)
        if verb != "GET":
            raise ValueError("仅支持 GET/POST")
        context = current_collection_context() or CollectionContext()
        with use_collection_context(context):
            return self._request_get(endpoint, query=query, advertiser_id=advertiser_id,
                                     before_send=before_send, context=context)

    def _read_get_response(self, response, context: CollectionContext) -> bytes:
        read1 = getattr(response, "read1", None)
        if not callable(read1):
            # Compatibility with simple response doubles; real HTTPResponse
            # provides read1, so production reads check the deadline per chunk.
            raw = response.read()
            context.check_active("after_response_read")
            if len(raw) > 16 * 1024 * 1024:
                raise ApiRequestError("单页响应超过安全容量", code="client_response_size")
            return raw
        chunks, size = [], 0
        while True:
            context.check_active("response_read")
            sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
            if sock is not None:
                sock.settimeout(max(0.001, min(self.timeout, context.remaining_seconds())))
            chunk = read1(64 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > 16 * 1024 * 1024:
                raise ApiRequestError("单页响应超过安全容量", code="client_response_size")
            chunks.append(chunk)
        context.check_active("after_response_read")
        return b"".join(chunks)

    def _request_get(self, endpoint, *, query, advertiser_id, before_send, context):
        attempts = min(4, self.max_get_attempts, context.max_request_attempts)
        logical_uid = f"oe_call_{uuid.uuid4().hex}"
        page_key = request_fingerprint(endpoint, query, advertiser_id=advertiser_id) if "page" in (query or {}) else ""
        refreshed = False
        rejected_revision = None
        last_error = None
        for attempt in range(1, attempts + 1):
            context.check_active("before_token")
            uid = f"oe_{uuid.uuid4().hex}"
            state = {"sent": False, "page_attempt": None}
            attempt_started = time.monotonic()
            decoded, status, response_headers, bundle = {}, None, None, None
            outcome, error_code, request_id = "failed", "", ""
            error = None
            result = None
            try:
                token_kwargs = {"force_refresh": refreshed}
                if rejected_revision is not None:
                    signature = inspect.signature(self.token_provider.get_token)
                    if "rejected_token_revision" in signature.parameters or any(
                            p.kind == inspect.Parameter.VAR_KEYWORD for p in signature.parameters.values()):
                        token_kwargs["rejected_token_revision"] = rejected_revision
                bundle = run_bounded(lambda: self.token_provider.get_token(**token_kwargs), context=context, lane="token")
                context.check_active("after_token")
                request = Request(self._url(endpoint, query), headers={"Access-Token": bundle.access_token,
                                  "Accept": "application/json"}, method="GET")

                def transport():
                    wait = getattr(self.rate_limiter, "wait_for_request", None)
                    if callable(wait):
                        wait(endpoint, advertiser_id)
                    else:
                        self.rate_limiter.wait(f"{endpoint}:{str(advertiser_id or '')}")
                    context.check_active("before_http_send")
                    if before_send is not None:
                        before_send()
                    context.check_active("before_http_send")
                    context.progress("http_send", endpoint=endpoint, page=(query or {}).get("page"),
                                     attempt=attempt, request_uid=uid)
                    context.check_active("before_http_send")
                    if page_key:
                        state["page_attempt"] = context.reserve_page_attempt(page_key)
                    state.update(sent=True, sent_at=time.time())
                    try:
                        with urlopen(request, timeout=max(0.001, min(self.timeout, context.remaining_seconds()))) as response:
                            raw = self._read_get_response(response, context)
                            return self._decode(raw), int(getattr(response, "status", 200) or 200), response.headers
                    except HTTPError as response:
                        try:
                            raw = self._read_get_response(response, context)
                            try:
                                payload = self._decode(raw)
                            except ApiRequestError:
                                payload = {"message": f"HTTP {response.code}"}
                            return payload, int(response.code), response.headers
                        finally:
                            response.close()

                def gated_transport():
                    scope_key = getattr(context, "pagination_info", {}).get("scope_fingerprint", page_key)
                    # Old cancelled IO retains its slot until it really exits;
                    # a rescan cannot pile three more sends on top of it.
                    with context.io_slot(scope_key):
                        return transport()
                decoded, status, response_headers = run_bounded(gated_transport, context=context, lane="io")
                context.check_active("after_http")
                code = str(decoded.get("code") or "0")
                request_id = self._request_id(decoded, response_headers)
                if status < 200 or status >= 300 or code not in {"", "0"}:
                    self._raise_api_error(decoded, endpoint=endpoint, http_status=status, headers=response_headers)
                outcome, error_code = "success", code
                result = ApiResponse(data=decoded.get("data", decoded), raw=decoded, request_id=request_id,
                                   code=code, message=safe_error_message(decoded.get("message")), request_uid=uid)
            except BaseException as exc:
                error = last_error = exc
                error_code = str(getattr(exc, "code", "") or getattr(exc, "http_status", "") or type(exc).__name__)
                request_id = str(getattr(exc, "request_id", "") or request_id)
                if isinstance(exc, ApiRequestError):
                    exc.request_uid = exc.request_uid or uid
            finally:
                page_options = getattr(context, "pagination_info", {})
                evidence = page_evidence(decoded.get("data", decoded),
                    items_key=page_options.get("items_key"), identity_getter=page_options.get("identity_getter")) if decoded else {}
                evidence.update(scope_fingerprint=page_options.get("scope_fingerprint", page_key),
                                batch_id=page_options.get("batch_id", ""))
                self._audit({"request_uid": uid, "endpoint": endpoint, "method": "GET",
                    "aavid": str(advertiser_id or ""), "request": {"query": query or {}, "body": {}},
                    "request_id": request_id, "status": outcome, "error_code": error_code,
                    "permission_status": "denied" if isinstance(error, (ApiTokenError, ApiPermissionError)) else "granted" if outcome == "success" else "unknown",
                    "response": {"code": error_code, "message": safe_error_message(decoded.get("message") or decoded.get("msg") or str(error or "")),
                        "help_message": help_evidence(decoded.get("help_message")), "pagination": evidence,
                        "attempt": attempt, "page_attempt": state["page_attempt"], "http_attempt": state["sent"],
                        "sent_at": state.get("sent_at"), "elapsed_ms": int((time.monotonic() - attempt_started) * 1000),
                        "logical_request_uid": logical_uid}})
            context.check_active("after_http_attempt")
            if result is not None:
                return result
            if str(getattr(error, "code", "")) == "authorization_context_changed":
                context.cancel("API授权上下文已变化，旧请求作废")
                raise error
            if isinstance(error, ApiTokenError) and state["sent"] and not refreshed and attempt < attempts:
                refreshed = True
                rejected_revision = getattr(bundle, "token_revision", None)
                continue
            retryable = isinstance(error, (URLError, socket.timeout, TimeoutError, ConnectionError, OSError)) or (
                isinstance(error, ApiRateLimitError)) or (
                isinstance(error, ApiRequestError) and not isinstance(error, (ApiTokenError, ApiPermissionError))
                and str(error.code) != "400153"
                and ((getattr(error, "http_status", None) or 0) >= 500 or self._is_transient_service_error(error.code, str(error))))
            if retryable and attempt < attempts:
                delay = max(float(getattr(error, "retry_after", 0) or 0), 2 ** (attempt - 1) + random.random() * 0.25)
                if self._sleep is time.sleep:
                    context.wait(delay, "http_retry_backoff")
                else:
                    run_bounded(lambda: self._sleep(delay), context=context, lane="io")
                continue
            if isinstance(error, (URLError, socket.timeout, TimeoutError, ConnectionError, OSError)):
                raise ApiRequestError("千川官方 API 网络请求失败", endpoint=endpoint, request_uid=uid) from error
            raise error
        raise last_error or ApiRequestError("千川官方 API 请求失败", endpoint=endpoint)

    def _request_post_legacy(
        self,
        method: str,
        endpoint: str,
        *,
        query: Optional[Mapping[str, Any]] = None,
        body: Optional[Mapping[str, Any]] = None,
        advertiser_id: Any = "",
        before_send: Optional[Callable[[], None]] = None,
    ) -> ApiResponse:
        verb = str(method or "GET").upper()
        if verb not in {"GET", "POST"}:
            raise ValueError("仅支持 GET/POST")
        attempts = self.max_get_attempts if verb == "GET" else 1
        token_refreshed = False
        local_request_uid = f"oe_{uuid.uuid4().hex}"
        last_error: Optional[BaseException] = None
        for attempt in range(1, attempts + 1):
            write_started = False
            wait_for_request = getattr(
                self.rate_limiter, "wait_for_request", None
            )
            if callable(wait_for_request):
                wait_for_request(endpoint, advertiser_id)
            else:
                # Compatibility with narrow test doubles and older embedders.
                self.rate_limiter.wait(
                    f"{endpoint}:{str(advertiser_id or '')}"
                )
            try:
                bundle = self.token_provider.get_token(force_refresh=token_refreshed)
                payload = None
                headers = {"Access-Token": bundle.access_token, "Accept": "application/json"}
                if body is not None:
                    payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                    headers["Content-Type"] = "application/json"
                request = Request(
                    self._url(endpoint, query),
                    data=payload,
                    headers=headers,
                    method=verb,
                )
                # Authorization may have changed during limiter/token waits.
                # The callback commits its short local transaction before any
                # socket operation; a failed callback must never send a POST.
                if before_send is not None:
                    before_send()
                write_started = verb == "POST"
                with urlopen(request, timeout=self.timeout) as response:
                    decoded = self._decode(response.read())
                    status = int(getattr(response, "status", 200) or 200)
                    code = str(decoded.get("code") or "0")
                    if status < 200 or status >= 300 or code not in {"", "0"}:
                        self._audit(
                            {
                                "request_uid": local_request_uid,
                                "endpoint": endpoint,
                                "method": verb,
                                "aavid": str(advertiser_id or ""),
                                "request": {"query": query or {}, "body": body or {}},
                                "request_id": self._request_id(decoded, response.headers),
                                "status": "failed",
                                "error_code": code or str(status),
                                "response": {
                                    "code": code or str(status),
                                    "message": str(decoded.get("message") or decoded.get("msg") or ""),
                                    "help_message": str(decoded.get("help_message") or ""),
                                },
                                "permission_status": (
                                    "denied"
                                    if status in {401, 403}
                                    or self._is_permission_error(code, str(decoded.get("message") or decoded.get("msg") or ""))
                                    else "unknown"
                                ),
                            }
                        )
                        self._raise_api_error(decoded, endpoint=endpoint, http_status=status, headers=response.headers)
                    request_id = self._request_id(decoded, response.headers)
                    result = ApiResponse(
                        data=decoded.get("data", decoded),
                        raw=decoded,
                        request_id=request_id,
                        code=code,
                        message=str(decoded.get("message") or ""),
                        request_uid=local_request_uid,
                    )
                    self._audit(
                        {
                            "request_uid": local_request_uid,
                            "endpoint": endpoint,
                            "method": verb,
                            "aavid": str(advertiser_id or ""),
                            "request": {"query": query or {}, "body": body or {}},
                            "request_id": request_id,
                            "status": "success",
                            "error_code": code,
                        }
                    )
                    return result
            except HTTPError as exc:
                try:
                    decoded = self._decode(exc.read())
                except ApiRequestError:
                    decoded = {"message": f"HTTP {exc.code}"}
                request_id = self._request_id(decoded, exc.headers)
                error_code = str(decoded.get("code") or decoded.get("err_no") or exc.code)
                if verb == "POST" and int(exc.code) >= 500:
                    error = ApiWriteOutcomeUnknown(
                        "千川官方 API 写请求结果未知，禁止直接重试，正在等待对账",
                        request_id=request_id,
                        endpoint=endpoint,
                        http_status=int(exc.code),
                        request_uid=local_request_uid,
                    )
                    self._audit({"request_uid": local_request_uid, "endpoint": endpoint, "method": verb, "aavid": str(advertiser_id or ""), "request": {"query": query or {}, "body": body or {}}, "request_id": request_id, "status": "unknown", "error_code": str(exc.code)})
                    raise error from exc
                self._audit(
                    {
                        "request_uid": local_request_uid,
                        "endpoint": endpoint,
                        "method": verb,
                        "aavid": str(advertiser_id or ""),
                        "request": {"query": query or {}, "body": body or {}},
                        "request_id": request_id,
                        "status": "failed",
                        "error_code": error_code,
                        "response": {
                            "code": error_code,
                            "message": str(decoded.get("message") or decoded.get("msg") or ""),
                            "help_message": str(decoded.get("help_message") or ""),
                        },
                        "permission_status": (
                            "denied"
                            if int(exc.code) in {401, 403}
                            or self._is_permission_error(error_code, str(decoded.get("message") or decoded.get("msg") or ""))
                            else "unknown"
                        ),
                    }
                )
                try:
                    self._raise_api_error(decoded, endpoint=endpoint, http_status=int(exc.code), headers=exc.headers)
                except ApiTokenError as token_error:
                    if verb == "GET" and not token_refreshed:
                        token_refreshed = True
                        last_error = token_error
                        continue
                    raise
                except ApiRateLimitError as rate_error:
                    last_error = rate_error
                    if verb == "GET" and attempt < attempts:
                        self._sleep(
                            max(
                                rate_error.retry_after,
                                (2 ** (attempt - 1)) + random.random() * 0.25,
                            )
                        )
                        continue
                    raise
            except ApiTokenError as exc:
                if verb == "GET" and not token_refreshed:
                    token_refreshed = True
                    last_error = exc
                    continue
                raise
            except ApiRateLimitError as exc:
                last_error = exc
                if verb == "GET" and attempt < attempts:
                    self._sleep(
                        max(
                            exc.retry_after,
                            (2 ** (attempt - 1)) + random.random() * 0.25,
                        )
                    )
                    continue
                raise
            except ApiRequestError as exc:
                last_error = exc
                if verb == "POST" and self._is_transient_service_error(
                    exc.code, str(exc)
                ):
                    # The platform accepted the HTTP request but its downstream
                    # service timed out.  The write may already exist, so never
                    # resubmit it; persist an unknown outcome for read-only
                    # reconciliation by the caller.
                    self._audit(
                        {
                            "request_uid": local_request_uid,
                            "endpoint": endpoint,
                            "method": verb,
                            "aavid": str(advertiser_id or ""),
                            "request": {"query": query or {}, "body": body or {}},
                            "request_id": exc.request_id,
                            "status": "unknown",
                            "error_code": exc.code or "transient_service",
                            "response": {
                                "code": exc.code,
                                "message": str(exc),
                            },
                        }
                    )
                    raise ApiWriteOutcomeUnknown(
                        "千川官方 API 内部处理超时，禁止重复提交，正在查询是否已创建",
                        code=exc.code,
                        request_id=exc.request_id,
                        endpoint=endpoint,
                        request_uid=local_request_uid,
                    ) from exc
                if (
                    verb == "GET"
                    and attempt < attempts
                    and self._is_transient_service_error(exc.code, str(exc))
                ):
                    self._sleep((2 ** (attempt - 1)) + random.random() * 0.25)
                    continue
                raise
            except (URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as exc:
                if verb == "POST" and write_started:
                    error = ApiWriteOutcomeUnknown(
                        "千川官方 API 写请求结果未知，禁止直接重试，正在等待对账",
                        endpoint=endpoint,
                        request_uid=local_request_uid,
                    )
                    self._audit({"request_uid": local_request_uid, "endpoint": endpoint, "method": verb, "aavid": str(advertiser_id or ""), "request": {"query": query or {}, "body": body or {}}, "status": "unknown", "error_code": "network"})
                    raise error from exc
                last_error = exc
                if attempt < attempts:
                    self._sleep((2 ** (attempt - 1)) + random.random() * 0.25)
                    continue
                self._audit(
                    {
                        "request_uid": local_request_uid,
                        "endpoint": endpoint,
                        "method": verb,
                        "aavid": str(advertiser_id or ""),
                        "request": {"query": query or {}, "body": body or {}},
                        "status": "failed",
                        "error_code": "network",
                    }
                )
                raise ApiRequestError("千川官方 API 网络请求失败", endpoint=endpoint, request_uid=local_request_uid) from exc
        if isinstance(last_error, Exception):
            raise last_error
        raise ApiRequestError("千川官方 API 请求失败", endpoint=endpoint)

    def get(self, endpoint: str, query: Optional[Mapping[str, Any]] = None, *, advertiser_id: Any = "") -> ApiResponse:
        return self.request("GET", endpoint, query=query, advertiser_id=advertiser_id)

    def post(self, endpoint: str, body: Mapping[str, Any], *, advertiser_id: Any = "",
             before_send: Optional[Callable[[], None]] = None) -> ApiResponse:
        return self.request("POST", endpoint, body=body, advertiser_id=advertiser_id,
                            **({"before_send": before_send} if before_send is not None else {}))

    @staticmethod
    def extract_items(data: Any, *, items_key: Optional[str] = None) -> list[dict[str, Any]]:
        from .pagination import extract_items
        return extract_items(data, items_key=items_key)

    @staticmethod
    def _has_more(data: Any, *, page: int, page_size: int, item_count: int) -> Optional[bool]:
        from .pagination import metadata
        return metadata(data, page=page, page_size=page_size, item_count=item_count).has_more

    def get_all_pages(
        self, endpoint: str, query: Mapping[str, Any], *, advertiser_id: Any = "",
        page_size: int = 100, max_pages: int = 1000, parallel_workers: int = 1,
        identity_getter: Optional[Callable[[Mapping[str, Any]], Any]] = None,
        verify_stability: bool = False, items_key: Optional[str] = None,
        pagination_context: Optional[CollectionContext] = None,
        progress_callback: Optional[Callable[[dict[str, Any]], None]] = None,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        from .pagination import collect_pages
        return collect_pages(self, endpoint, query, advertiser_id=advertiser_id,
                             page_size=page_size, max_pages=max_pages,
                             parallel_workers=parallel_workers, identity_getter=identity_getter,
                             verify_stability=verify_stability, items_key=items_key,
                             pagination_context=pagination_context, progress_callback=progress_callback)
