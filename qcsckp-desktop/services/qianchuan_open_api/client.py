"""巨量千川 Open API HTTP 客户端。

GET 可针对网络、429、5xx 做有界退避；POST 从不盲目重试。POST 在已发出后没有
得到确定响应时抛 ``ApiWriteOutcomeUnknown``，必须由业务层查询调控任务和操作日志
对账。
"""

from __future__ import annotations

import json
import random
import socket
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
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


class EndpointRateLimiter:
    """分层节流：应用 8 QPS、单账户 4 QPS，并保留 endpoint 级保护。"""

    def __init__(
        self,
        requests_per_second: float = 8.0,
        *,
        account_requests_per_second: float = 4.0,
        endpoint_requests_per_second: float = 8.0,
    ) -> None:
        self.interval = 1.0 / max(0.1, float(requests_per_second))
        self.account_interval = 1.0 / max(
            0.1, float(account_requests_per_second)
        )
        self.endpoint_interval = 1.0 / max(
            0.1, float(endpoint_requests_per_second)
        )
        self._next: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, key: str) -> None:
        with self._lock:
            now = time.monotonic()
            due = self._next.get(key, now)
            delay = max(0.0, due - now)
            self._next[key] = max(now, due) + self.interval
        if delay:
            time.sleep(delay)

    def wait_for_request(self, endpoint: str, advertiser_id: Any = "") -> None:
        """Atomically reserve all applicable quota lanes for one request."""
        account = str(advertiser_id or "").strip()
        lanes: list[tuple[str, float]] = [
            ("application", self.interval),
            (f"endpoint:{endpoint}", self.endpoint_interval),
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
            time.sleep(delay)


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
        if str(code or "").strip().startswith("5"):
            return True
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
        return any(
            phrase in text
            for phrase in (
                "系统开小差",
                "稍后重试",
                "服务繁忙",
                "服务异常",
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
        message = str(payload.get("message") or payload.get("msg") or "千川官方 API 请求失败")
        request_id = self._request_id(payload, headers)
        retry_after = 0.0
        if headers is not None:
            try:
                retry_after = float(headers.get("Retry-After") or 0)
            except (TypeError, ValueError, AttributeError):
                retry_after = 0.0
        kwargs = {
            "code": code,
            "request_id": request_id,
            "endpoint": endpoint,
            "http_status": http_status,
            "retry_after": retry_after,
        }
        if (
            http_status == 429
            or code == "40100"
            or "rate" in message.lower()
            or "频控" in message
            or "请求频繁" in message
        ):
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
        self,
        method: str,
        endpoint: str,
        *,
        query: Optional[Mapping[str, Any]] = None,
        body: Optional[Mapping[str, Any]] = None,
        advertiser_id: Any = "",
    ) -> ApiResponse:
        verb = str(method or "GET").upper()
        if verb not in {"GET", "POST"}:
            raise ValueError("仅支持 GET/POST")
        attempts = self.max_get_attempts if verb == "GET" else 1
        token_refreshed = False
        local_request_uid = f"oe_{uuid.uuid4().hex}"
        last_error: Optional[BaseException] = None
        for attempt in range(1, attempts + 1):
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
                if (
                    verb == "GET"
                    and attempt < attempts
                    and self._is_transient_service_error(exc.code, str(exc))
                ):
                    self._sleep((2 ** (attempt - 1)) + random.random() * 0.25)
                    continue
                raise
            except (URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as exc:
                if verb == "POST":
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

    def post(self, endpoint: str, body: Mapping[str, Any], *, advertiser_id: Any = "") -> ApiResponse:
        return self.request("POST", endpoint, body=body, advertiser_id=advertiser_id)

    @staticmethod
    def extract_items(data: Any) -> list[dict[str, Any]]:
        if isinstance(data, list):
            return [dict(item) for item in data if isinstance(item, Mapping)]
        if not isinstance(data, Mapping):
            return []
        # Ocean Engine uses endpoint-specific collection keys.  Keep the
        # explicit list ordered so pagination never mistakes a nested metric
        # array for the endpoint's primary result set.
        for key in (
            "adv_id_list",
            "account_list",
            "ad_list",
            "ad_material_infos",
            "material_list",
            "product_list",
            "task_list",
            "log_list",
            "logs",
            "advertisers",
            "data_list",
            "items",
            "rows",
            "list",
        ):
            value = data.get(key)
            if isinstance(value, list):
                rows = [dict(item) for item in value if isinstance(item, Mapping)]
                # Some Ocean Engine responses contain both a generic ``list``
                # of bare numeric IDs and a richer endpoint-specific list such
                # as ``adv_id_list``.  A primitive list must not hide the
                # structured rows needed by the normalizer.
                if rows:
                    return rows
        for value in data.values():
            if isinstance(value, Mapping):
                found = QianchuanOpenApiClient.extract_items(value)
                if found:
                    return found
        return []

    @staticmethod
    def _has_more(
        data: Any, *, page: int, page_size: int, item_count: int
    ) -> Optional[bool]:
        if isinstance(data, Mapping):
            for key in ("page_info", "pageInfo", "pagination"):
                info = data.get(key)
                if isinstance(info, Mapping):
                    echoed_size = info.get("page_size") or info.get("pageSize")
                    if echoed_size not in (None, "") and int(echoed_size) != int(page_size):
                        raise ApiRequestError(
                            "千川官方 API 回显分页大小与请求不一致，结果已标记为不完整"
                        )
                    if "has_more" in info:
                        return bool(info.get("has_more"))
                    total_page = info.get("total_page")
                    if total_page is None:
                        total_page = info.get("total_pages")
                    if total_page is not None:
                        return page < max(0, int(total_page))
                    total = info.get("total_number")
                    if total is None:
                        total = info.get("total_num")
                    if total is None:
                        total = info.get("total")
                    if total is None:
                        total = info.get("count")
                    if total is not None:
                        return page * page_size < int(total)
            if "has_more" in data:
                return bool(data.get("has_more"))
        # A short first page is not proof of completion when the endpoint
        # omits pagination metadata or silently caps page_size. Fail closed so
        # callers retain the last complete snapshot.
        return None

    def get_all_pages(
        self,
        endpoint: str,
        query: Mapping[str, Any],
        *,
        advertiser_id: Any = "",
        page_size: int = 100,
        max_pages: int = 1000,
        parallel_workers: int = 1,
        identity_getter: Optional[Callable[[Mapping[str, Any]], Any]] = None,
        verify_stability: bool = False,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        page_limit = max(1, int(max_pages))
        workers = max(1, min(8, int(parallel_workers or 1)))
        first_query = dict(query)
        first_query["page"] = 1
        first_query["page_size"] = page_size
        first_response = self.get(endpoint, first_query, advertiser_id=advertiser_id)
        first_items = self.extract_items(first_response.data)
        first_has_more = self._has_more(
            first_response.data,
            page=1,
            page_size=page_size,
            item_count=len(first_items),
        )
        expected_total: Optional[int] = None
        if isinstance(first_response.data, Mapping):
            for key in ("page_info", "pageInfo", "pagination"):
                info = first_response.data.get(key)
                if not isinstance(info, Mapping):
                    continue
                raw_total = None
                for total_key in (
                    "total_number",
                    "total_num",
                    "total",
                    "count",
                ):
                    if info.get(total_key) not in (None, ""):
                        raw_total = info.get(total_key)
                        break
                if raw_total not in (None, ""):
                    expected_total = max(0, int(raw_total))
                break

        def finalize(
            collected: list[dict[str, Any]], request_ids: list[str]
        ) -> tuple[list[dict[str, Any]], list[str]]:
            if expected_total is not None and len(collected) != expected_total:
                raise ApiRequestError(
                    "千川官方 API 分页记录数与总数不一致，结果已标记为不完整",
                    endpoint=endpoint,
                    request_id=first_response.request_id,
                )
            if identity_getter is not None:
                identities = [
                    str(identity_getter(item) or "").strip() for item in collected
                ]
                if any(not value for value in identities):
                    raise ApiRequestError(
                        "千川官方 API 分页返回了缺少唯一标识的记录",
                        endpoint=endpoint,
                        request_id=first_response.request_id,
                    )
                if len(set(identities)) != len(identities):
                    raise ApiRequestError(
                        "千川官方 API 分页返回重复记录，结果已标记为不完整",
                        endpoint=endpoint,
                        request_id=first_response.request_id,
                    )
            if verify_stability and len(collected) > page_size:
                verify_response = self.get(
                    endpoint, first_query, advertiser_id=advertiser_id
                )
                verify_items = self.extract_items(verify_response.data)
                verify_total: Optional[int] = None
                if isinstance(verify_response.data, Mapping):
                    for key in ("page_info", "pageInfo", "pagination"):
                        info = verify_response.data.get(key)
                        if not isinstance(info, Mapping):
                            continue
                        for total_key in (
                            "total_number",
                            "total_num",
                            "total",
                            "count",
                        ):
                            if info.get(total_key) not in (None, ""):
                                verify_total = int(info[total_key])
                                break
                        break
                if verify_total != expected_total:
                    raise ApiRequestError(
                        "千川官方 API 分页期间总记录数发生变化，已保留上次可信数据",
                        endpoint=endpoint,
                        request_id=verify_response.request_id,
                    )
                if identity_getter is not None:
                    initial_ids = [
                        str(identity_getter(item) or "").strip()
                        for item in first_items
                    ]
                    verify_ids = [
                        str(identity_getter(item) or "").strip()
                        for item in verify_items
                    ]
                    if initial_ids != verify_ids:
                        raise ApiRequestError(
                            "千川官方 API 分页期间排序发生变化，已保留上次可信数据",
                            endpoint=endpoint,
                            request_id=verify_response.request_id,
                        )
                if verify_response.request_id:
                    request_ids.append(verify_response.request_id)
            return collected, request_ids
        if first_has_more is None:
            raise ApiRequestError(
                "千川官方 API 未返回可验证的分页信息，结果已标记为不完整",
                endpoint=endpoint,
                request_id=first_response.request_id,
            )
        if not first_has_more:
            return finalize(
                first_items,
                [first_response.request_id] if first_response.request_id else [],
            )

        total_pages = 0
        if isinstance(first_response.data, Mapping):
            for key in ("page_info", "pageInfo", "pagination"):
                info = first_response.data.get(key)
                if isinstance(info, Mapping):
                    raw_total = info.get("total_page")
                    if raw_total is None:
                        raw_total = info.get("total_pages")
                    if raw_total not in (None, ""):
                        total_pages = int(raw_total)
                    break
        if workers > 1 and total_pages:
            if total_pages > page_limit:
                raise ApiRequestError(
                    "千川官方 API 分页超过安全上限，结果已标记为不完整",
                    endpoint=endpoint,
                    request_id=first_response.request_id,
                )

            def fetch_page(page: int) -> tuple[int, ApiResponse, list[dict[str, Any]]]:
                current = dict(query)
                current["page"] = page
                current["page_size"] = page_size
                response = self.get(endpoint, current, advertiser_id=advertiser_id)
                return page, response, self.extract_items(response.data)

            pages: dict[int, tuple[ApiResponse, list[dict[str, Any]]]] = {
                1: (first_response, first_items)
            }
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="oe-api-page") as pool:
                futures = {pool.submit(fetch_page, page): page for page in range(2, total_pages + 1)}
                for future in as_completed(futures):
                    page, response, items = future.result()
                    pages[page] = (response, items)

            rows: list[dict[str, Any]] = []
            request_ids: list[str] = []
            seen_fingerprints: set[str] = set()
            for page in range(1, total_pages + 1):
                response, items = pages[page]
                fingerprint = json.dumps(items, ensure_ascii=False, sort_keys=True, default=str)
                if fingerprint in seen_fingerprints:
                    raise ApiRequestError(
                        "千川官方 API 分页返回重复页面，目录已标记为不完整",
                        endpoint=endpoint,
                        request_id=response.request_id,
                    )
                if page < total_pages and not items:
                    raise ApiRequestError(
                        "千川官方 API 在分页中间返回空页，结果已标记为不完整",
                        endpoint=endpoint,
                        request_id=response.request_id,
                    )
                seen_fingerprints.add(fingerprint)
                rows.extend(items)
                if response.request_id:
                    request_ids.append(response.request_id)
            return finalize(rows, request_ids)

        rows: list[dict[str, Any]] = []
        request_ids: list[str] = []
        seen_fingerprints: set[str] = set()
        for page in range(1, page_limit + 1):
            if page == 1:
                response, items = first_response, first_items
            else:
                current = dict(query)
                current["page"] = page
                current["page_size"] = page_size
                response = self.get(endpoint, current, advertiser_id=advertiser_id)
                items = self.extract_items(response.data)
            fingerprint = json.dumps(items, ensure_ascii=False, sort_keys=True, default=str)
            if page > 1 and fingerprint in seen_fingerprints:
                raise ApiRequestError(
                    "千川官方 API 分页返回重复页面，目录已标记为不完整",
                    endpoint=endpoint,
                    request_id=response.request_id,
                )
            seen_fingerprints.add(fingerprint)
            rows.extend(items)
            if response.request_id:
                request_ids.append(response.request_id)
            has_more = self._has_more(
                response.data,
                page=page,
                page_size=page_size,
                item_count=len(items),
            )
            if has_more is None:
                raise ApiRequestError(
                    "千川官方 API 未返回可验证的分页信息，结果已标记为不完整",
                    endpoint=endpoint,
                    request_id=response.request_id,
                )
            if not has_more:
                return finalize(rows, request_ids)
            if not items:
                raise ApiRequestError(
                    "千川官方 API 声明仍有下一页但返回空页，结果已标记为不完整",
                    endpoint=endpoint,
                    request_id=response.request_id,
                )
        raise ApiRequestError("千川官方 API 分页超过安全上限，结果已标记为不完整", endpoint=endpoint)
