"""Complete-or-raise pagination with bounded dispatch and an immutable scope."""
from __future__ import annotations

import copy
import math
import threading
import uuid
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from .collection_context import CollectionContext, current_collection_context, request_fingerprint, use_collection_context
from .errors import ApiRequestError, PaginationIntegrityError, PaginationDriftError
from .managed_workers import PAGINATION_WORKERS
from .pagination_evidence import fingerprint, page_evidence

PRIMARY_KEYS = ("adv_id_list", "account_list", "ad_list", "ad_material_infos", "material_list",
                "product_list", "task_list", "log_list", "logs", "advertisers", "data_list", "items", "rows", "list")


def extract_items(data: Any, *, items_key: Optional[str] = None) -> list[dict[str, Any]]:
    if items_key is not None:
        value = data
        for part in items_key.split("."):
            if not isinstance(value, Mapping) or part not in value:
                raise PaginationIntegrityError("响应缺少已声明的主列表", code="client_items_missing", pagination={"items_key": items_key})
            value = value[part]
        if not isinstance(value, list) or any(not isinstance(row, Mapping) for row in value):
            raise PaginationIntegrityError("响应主列表类型不正确", code="client_items_type", pagination={"items_key": items_key})
        return [dict(row) for row in value]
    if isinstance(data, list):
        if any(not isinstance(row, Mapping) for row in data):
            raise PaginationIntegrityError("响应主列表含无效记录", code="client_items_type")
        return [dict(row) for row in data]
    if not isinstance(data, Mapping):
        return []
    for key in PRIMARY_KEYS:
        value = data.get(key)
        if isinstance(value, list):
            if any(not isinstance(row, Mapping) for row in value):
                raise PaginationIntegrityError("响应主列表含无效记录", code="client_items_type",
                                               pagination={"items_key": key})
            return [dict(row) for row in value]
    # Only protocol wrappers are eligible. Never recurse through arbitrary
    # summary/metadata mappings looking for an unrelated array.
    for wrapper in ("data", "result"):
        if isinstance(data.get(wrapper), Mapping):
            return extract_items(data[wrapper])
    return []


def _integer(info: Mapping, *names: str) -> Optional[int]:
    found = None
    for name in names:
        value = info.get(name)
        if value in (None, ""):
            continue
        try:
            if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
                raise ValueError()
            parsed = int(value)
            if parsed < 0:
                raise ValueError()
        except (TypeError, ValueError, OverflowError):
            raise PaginationIntegrityError("分页元数据不是有效非负整数", code="client_page_metadata", pagination={"field": name})
        if found is not None and found != parsed:
            raise PaginationIntegrityError("分页元数据同义字段相互矛盾", code="client_page_metadata", pagination={"field": name})
        found = parsed
    return found


@dataclass(frozen=True)
class PageMetadata:
    total: Optional[int]
    pages: Optional[int]
    has_more: Optional[bool]


def metadata(data: Any, *, page: int, page_size: int, item_count: int) -> PageMetadata:
    if not isinstance(data, Mapping):
        return PageMetadata(None, None, None)
    info = next((data[key] for key in ("page_info", "pageInfo", "pagination")
                 if isinstance(data.get(key), Mapping)), {})
    echoed_page = _integer(info, "page", "page_index", "pageIndex")
    echoed_size = _integer(info, "page_size", "pageSize")
    if echoed_page is not None and echoed_page != page:
        raise PaginationIntegrityError("千川官方 API 回显页码与请求不一致", code="client_page_echo")
    if echoed_size is not None and echoed_size != page_size:
        raise PaginationIntegrityError("千川官方 API 回显分页大小与请求不一致，结果已标记为不完整", code="client_page_size")
    if item_count > page_size:
        raise PaginationIntegrityError("主列表行数超过请求页容量", code="client_page_capacity")
    total = _integer(info, "total_number", "total_num", "total", "count")
    pages = _integer(info, "total_page", "total_pages")
    if pages == 0 and (item_count or (total is not None and total > 0)):
        raise PaginationIntegrityError("零总页数与非空结果矛盾", code="client_page_metadata")
    if pages is not None and total is not None:
        expected_pages = math.ceil(total / page_size)
        if pages != expected_pages and not (total == 0 and pages == 1):
            raise PaginationIntegrityError("分页总页数与总记录数及页容量矛盾", code="client_page_metadata")
    if pages is not None and page > max(1, pages):
        raise PaginationIntegrityError("响应页码超过声明的总页数", code="client_page_metadata")
    has_more = None
    if "has_more" in info or "has_more" in data:
        raw = info.get("has_more") if "has_more" in info else data["has_more"]
        if not isinstance(raw, (bool, int)) or raw not in (True, False, 0, 1):
            raise PaginationIntegrityError("has_more 类型无效", code="client_page_metadata")
        has_more = bool(raw)
    computed = page < pages if pages is not None else page * page_size < total if total is not None else None
    if has_more is not None and computed is not None and has_more != computed:
        raise PaginationIntegrityError("分页结束标记与总页数矛盾", code="client_page_metadata")
    return PageMetadata(total, pages, has_more if has_more is not None else computed)


def _identity(row, getter):
    value = getter(row)
    if value is None or value == "" or (isinstance(value, str) and not value.strip()):
        raise PaginationIntegrityError("千川官方 API 分页返回了缺少唯一标识的记录", code="client_identity_missing")
    return fingerprint(value)


def collect_pages(client, endpoint, query, *, advertiser_id="", page_size=100, max_pages=1000,
                  parallel_workers=1, identity_getter=None, verify_stability=False,
                  items_key=None, pagination_context=None, progress_callback=None):
    parent = pagination_context or current_collection_context() or CollectionContext()
    frozen = copy.deepcopy(dict(query))
    frozen["page_size"] = int(page_size)
    frozen.pop("page", None)
    scope = request_fingerprint(endpoint, frozen, advertiser_id=advertiser_id)
    while True:
        try:
            return _scan(client, endpoint, frozen, advertiser_id=advertiser_id,
                         page_size=int(page_size), max_pages=max(1, int(max_pages)),
                         workers=max(1, min(3, int(parallel_workers or 1))), getter=identity_getter,
                         verify=verify_stability, items_key=items_key, parent=parent, scope=scope,
                         progress_callback=progress_callback)
        except PaginationDriftError:
            if not parent.reserve_rescan(scope):
                raise
            parent.progress("pagination_rescan", scope_fingerprint=scope)


def _scan(client, endpoint, frozen, *, advertiser_id, page_size, max_pages, workers, getter,
          verify, items_key, parent, scope, progress_callback):
    context = parent.child()
    context.pagination_info = {"items_key": items_key, "identity_getter": getter,
                               "scope_fingerprint": scope, "batch_id": uuid.uuid4().hex}
    pending = {}
    results = {}
    failures: set[int] = set()
    failures_lock = threading.Lock()
    rechecked = set()
    first = None
    first_response = None
    fingerprints = set()
    identities = set()
    received_fingerprints = set()
    received_identities = set()
    request_ids = []
    collected = []

    def accept(page, response, rows, meta):
        digest = fingerprint(rows)
        if digest in received_fingerprints:
            raise PaginationDriftError("千川官方 API 分页返回重复页面，目录已标记为不完整", code="client_duplicate_page",
                                       pagination={"page": page, "page_fingerprint": digest})
        if getter is not None:
            for row in rows:
                key = _identity(row, getter)
                if key in received_identities:
                    raise PaginationDriftError("千川官方 API 分页返回重复记录，结果已标记为不完整", code="client_duplicate_identity",
                                               pagination={"page": page, "duplicate_identity_hash": key})
                received_identities.add(key)
        received_fingerprints.add(digest)
        results[page] = (response, rows, meta)

    def progress(phase, **details):
        context.progress(phase, scope_fingerprint=scope, **details)
        if progress_callback:
            try:
                progress_callback({"phase": phase, "scope_fingerprint": scope, **details})
            except Exception:
                pass

    def fetch(page, *, recheck=False):
        read_context = context.child() if recheck else context
        read_context.pagination_info = context.pagination_info
        if recheck:
            read_context.max_request_attempts = 1
        try:
            with use_collection_context(read_context):
                read_context.check_active("before_page")
                response = client.get(endpoint, copy.deepcopy(dict(frozen, page=page)), advertiser_id=advertiser_id)
                read_context.check_active("after_page")
                rows = extract_items(response.data, items_key=items_key)
                meta = metadata(response.data, page=page, page_size=page_size, item_count=len(rows))
                if meta.has_more is None:
                    raise PaginationIntegrityError("千川官方 API 未返回可验证的分页信息，结果已标记为不完整", code="client_page_metadata")
                if first is not None:
                    if (first.total is None) != (meta.total is None) or (first.pages is None) != (meta.pages is None):
                        raise PaginationIntegrityError("分页总数元信息字段缺失或结构与首屏不一致", code="client_page_metadata_missing")
                    if (first.total is not None and meta.total != first.total) or (first.pages is not None and meta.pages != first.pages):
                        raise PaginationDriftError("千川官方 API 分页期间总记录数发生变化，已保留上次可信数据", code="client_total_drift")
                if not rows and meta.has_more:
                    raise PaginationIntegrityError("千川官方 API 声明仍有下一页但返回空页，结果已标记为不完整", code="client_empty_page")
                return response, rows, meta
        except BaseException:
            with failures_lock:
                failures.add(page)
            raise

    def resolve(page, task):
        try:
            return task.result()
        except ApiRequestError as exc:
            material_endpoint = str(endpoint).rstrip("/").endswith("/uni_promotion/ad/material/get")
            if (str(exc.code) == "400153" and material_endpoint and page > 1 and first_response is not None
                    and page not in rechecked and not exc.help_message.get("parameter_error")):
                rechecked.add(page)
                progress("pagination_page_recheck", page=page, error_code="400153")
                retry = PAGINATION_WORKERS.submit(lambda: fetch(page, recheck=True), context=context)
                result = retry.result()
                with failures_lock:
                    failures.discard(page)
                return result
            raise

    try:
        first_task = PAGINATION_WORKERS.submit(lambda: fetch(1), context=context)
        first_response, first_rows, first = first_task.result()
        total_pages = first.pages
        if total_pages is None and first.total is not None:
            total_pages = max(1, math.ceil(first.total / page_size))
        if total_pages is not None and total_pages > max_pages:
            raise PaginationIntegrityError("千川官方 API 分页超过安全上限，结果已标记为不完整", code="client_page_limit")
        accept(1, first_response, first_rows, first)
        next_page = 2
        if not first.has_more:
            total_pages = 1
        if total_pages is None:
            workers = 1
        while True:
            with failures_lock:
                paused = bool(failures)
            while not paused and len(pending) < workers and next_page <= (total_pages or max_pages):
                context.check_active("page_dispatch")
                page = next_page
                pending[page] = PAGINATION_WORKERS.submit(lambda p=page: fetch(p), context=context)
                next_page += 1
                with failures_lock:
                    paused = bool(failures)
            if not pending:
                break
            ready = [page for page, task in pending.items() if task.done()]
            if not ready:
                context.wait(0.02, "page_wait")
                continue
            # Handle every completed failure before dispatching more pages.
            for page in ready:
                task = pending.pop(page)
                response, rows, meta = resolve(page, task)
                accept(page, response, rows, meta)
                progress("pagination_page_complete", page=page, actual_list_count=len(rows))
                if total_pages is None and not meta.has_more:
                    total_pages = page
            if total_pages is not None and next_page > total_pages and not pending:
                break
        if total_pages is None or max(results) < total_pages:
            raise PaginationIntegrityError("千川官方 API 分页超过安全上限，结果已标记为不完整", code="client_page_limit")
        for page in range(1, total_pages + 1):
            response, rows, meta = results[page]
            digest = fingerprint(rows)
            if digest in fingerprints:
                raise PaginationDriftError("千川官方 API 分页返回重复页面，目录已标记为不完整", code="client_duplicate_page")
            fingerprints.add(digest)
            if not rows and page < total_pages:
                raise PaginationIntegrityError("千川官方 API 在分页中间返回空页，结果已标记为不完整", code="client_empty_page")
            if getter is not None:
                for row in rows:
                    key = _identity(row, getter)
                    if key in identities:
                        raise PaginationDriftError("千川官方 API 分页返回重复记录，结果已标记为不完整", code="client_duplicate_identity")
                    identities.add(key)
            collected.extend(rows)
            if response.request_id:
                request_ids.append(response.request_id)
        if first.total is not None and len(collected) != first.total:
            raise PaginationDriftError("千川官方 API 分页记录数与总数不一致，结果已标记为不完整", code="client_count_mismatch",
                                           pagination={"expected_total": first.total, "actual_count": len(collected)})
        if verify and total_pages > 1:
            task = PAGINATION_WORKERS.submit(lambda: fetch(1), context=context)
            response, rows, meta = task.result()
            if getter is not None:
                if [_identity(row, getter) for row in rows] != [_identity(row, getter) for row in first_rows]:
                    raise PaginationDriftError("千川官方 API 分页期间排序发生变化，已保留上次可信数据", code="client_order_drift")
            elif fingerprint(rows) != fingerprint(first_rows):
                raise PaginationDriftError("千川官方 API 首屏复核发生变化", code="client_order_drift")
            if response.request_id:
                request_ids.append(response.request_id)
        context.check_active("pagination_complete")
        progress("pagination_complete", actual_list_count=len(collected), page_count=total_pages)
        return collected, request_ids
    except BaseException as exc:
        if isinstance(exc, ApiRequestError):
            exc.endpoint = exc.endpoint or str(endpoint)
            exc.request_id = exc.request_id or str(getattr(first_response, "request_id", ""))
            exc.pagination.update(scope_fingerprint=scope, completed_pages=sorted(results),
                                  requested_page_size=page_size, phase="pagination_incomplete")
        context.cancel("本批分页已失败，迟到页作废")
        for task in pending.values():
            task.cancel()
        raise
