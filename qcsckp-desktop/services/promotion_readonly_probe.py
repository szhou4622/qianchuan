"""千川计划识别用只读探针。

只保存脱敏后的页面路径、可见场景信号、计划/商品/素材候选字段和 API 路径。
请求体只经过字段白名单摘要后保存，不保存 Cookie、Token、请求头、完整 URL、
原始请求体或原始响应。
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import unquote, urlparse

from services.product_scene_adapter import (
    PRODUCT_AD_LIST_API_PATHS,
    PRODUCT_PLAN_API_PATHS,
    extract_product_scene_snapshot,
    extract_safe_query_identifiers,
    merge_product_scene_snapshots,
)

SENSITIVE_KEY_PARTS = (
    "token",
    "cookie",
    "secret",
    "sign",
    "auth",
    "ticket",
    "session",
    "password",
)
ID_KEYS = {
    "id",
    "adId",
    "ad_id",
    "advertiserId",
    "aavid",
    "materialId",
    "productId",
    "goodsId",
    "taskId",
}
NAME_KEYS = {
    "adName",
    "planName",
    "name",
    "productName",
    "goodsName",
    "materialName",
    "title",
    "advertiserName",
    "advertiser_name",
    "userInfoName",
    "user_info_name",
    "accountName",
    "account_name",
}
SCENE_KEYS = {
    "scene",
    "promotionScene",
    "creativeType",
    "marketingGoal",
    "promotionType",
    "planSystem",
    "plan_system",
    "deliverySystem",
    "delivery_system",
    "promotionSystem",
    "promotion_system",
    "isChengfang",
    "is_chengfang",
    "chengfang",
}
PLAN_HINT_KEYS = {
    "budget",
    "status",
    "deliveryStatus",
    "adDeliveryType",
    "adDeliveryName",
    "ecpRoi2Goal",
    "roiGoal",
    "bid",
}


def _safe_text(value: Any, limit: int = 160) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    return re.sub(r"\s+", " ", text)[:limit]


def _allowed_value(key: str, value: Any) -> Optional[str]:
    lower = str(key or "").lower()
    if any(part in lower for part in SENSITIVE_KEY_PARTS):
        return None
    if key not in ID_KEYS | NAME_KEYS | SCENE_KEYS:
        return None
    if isinstance(value, (dict, list, tuple, set)):
        return None
    text = _safe_text(value)
    return text or None


def summarize_json(payload: Any, *, max_nodes: int = 240) -> Dict[str, Any]:
    """从响应中提取少量允许字段，不保留原始 JSON。"""
    queue: List[tuple[str, Any]] = [("$", payload)]
    fields: List[Dict[str, str]] = []
    plan_candidates: List[Dict[str, Any]] = []
    account_candidates: List[Dict[str, str]] = []
    seen_fields = set()
    nodes = 0

    while queue and nodes < max_nodes:
        path, value = queue.pop(0)
        nodes += 1
        if isinstance(value, dict):
            candidate: Dict[str, str] = {}
            for key, one in value.items():
                allowed = _allowed_value(str(key), one)
                if allowed is not None:
                    marker = (str(key), allowed)
                    if marker not in seen_fields and len(fields) < 120:
                        seen_fields.add(marker)
                        fields.append(
                            {
                                "path": path[-160:],
                                "key": str(key),
                                "value": allowed,
                            }
                        )
                    candidate[str(key)] = allowed
            has_plan_id = bool(
                candidate.get("adId")
                or candidate.get("ad_id")
                or (
                    candidate.get("id")
                    and any(k in value for k in PLAN_HINT_KEYS)
                )
            )
            has_plan_name = bool(
                candidate.get("adName")
                or candidate.get("planName")
                or candidate.get("name")
            )
            if has_plan_id and (has_plan_name or any(k in value for k in PLAN_HINT_KEYS)):
                plan = {
                    key: candidate[key]
                    for key in (
                        "adId",
                        "ad_id",
                        "id",
                        "adName",
                        "planName",
                        "name",
                        "scene",
                        "promotionScene",
                        "creativeType",
                        "marketingGoal",
                        "promotionType",
                    )
                    if candidate.get(key)
                }
                if plan and plan not in plan_candidates and len(plan_candidates) < 30:
                    plan_candidates.append(plan)
            account_id = (
                candidate.get("aavid")
                or candidate.get("advertiserId")
            )
            account_name = (
                candidate.get("advertiserName")
                or candidate.get("advertiser_name")
                or candidate.get("userInfoName")
                or candidate.get("user_info_name")
                or candidate.get("accountName")
                or candidate.get("account_name")
            )
            if (
                account_id
                and str(account_id).isdigit()
                and account_name
            ):
                account = {
                    "aavid": str(account_id),
                    "account_name": str(account_name)[:256],
                }
                if (
                    account not in account_candidates
                    and len(account_candidates) < 100
                ):
                    account_candidates.append(account)
            for key, one in value.items():
                lower = str(key).lower()
                if any(part in lower for part in SENSITIVE_KEY_PARTS):
                    continue
                if isinstance(one, (dict, list)):
                    queue.append((f"{path}.{key}", one))
        elif isinstance(value, list):
            for index, one in enumerate(value[:80]):
                if isinstance(one, (dict, list)):
                    queue.append((f"{path}[{index}]", one))

    return {
        "top_keys": sorted(str(k) for k in payload.keys())[:80]
        if isinstance(payload, dict)
        else [],
        "fields": fields,
        "plan_candidates": plan_candidates,
        "account_candidates": account_candidates,
        "nodes_scanned": nodes,
    }


def summarize_page(url: str, page_text: str) -> Dict[str, Any]:
    """生成不含完整 URL 和正文的页面摘要。"""
    parsed = urlparse(str(url or "").strip())
    decoded = unquote(str(url or ""))
    text = str(page_text or "")

    def ids(patterns: Iterable[str]) -> List[str]:
        found: List[str] = []
        for pattern in patterns:
            for value in re.findall(pattern, text, flags=re.IGNORECASE):
                item = str(value or "").strip()
                if item and item not in found:
                    found.append(item)
        return found[:30]

    names: List[str] = []
    for pattern in (
        r"(?:计划名称|广告名称)\s*[：:]\s*([^\r\n]{1,120})",
        r"(?:计划名称|广告名称)\s+([^\r\n]{1,120})",
    ):
        for value in re.findall(pattern, text):
            item = _safe_text(value)
            if item and item not in names:
                names.append(item)

    return {
        "host": parsed.netloc,
        "path": parsed.path,
        "scene_markers": {
            "product_race_url": "productrace" in decoded.lower(),
            "live_race_url": "liverace" in decoded.lower(),
            "push_product": "推商品" in text,
            "product_select": "商品自选" in text,
            "product_global": "商品全域" in text,
            "push_live": "推直播" in text,
            "material_retarget": "素材追投" in text,
            "control_tools": "调控工具" in text,
        },
        "visible_plan_ids": ids(
            (
                r"(?:计划ID|广告ID|计划编号)\s*[：:#]?\s*(\d{8,})",
                r"\badId\s*[：:=]\s*(\d{8,})",
            )
        ),
        "visible_aavids": ids(
            (
                r"(?:账户ID|广告主ID)\s*[：:#]?\s*(\d{8,})",
                r"\baavid\s*[：:=]\s*(\d{8,})",
            )
        ),
        "visible_product_ids": ids(
            (r"(?:商品ID|商品编号)\s*[：:#]?\s*(\d{6,})",)
        ),
        "visible_material_ids": ids(
            (r"(?:素材ID|视频ID)\s*[：:#]?\s*(\d{6,})",)
        ),
        "visible_plan_names": names[:20],
    }


class PromotionReadOnlyProbe:
    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        self._lock = threading.Lock()
        self._pages: Dict[str, Dict[str, Any]] = {}
        self._apis: Dict[str, Dict[str, Any]] = {}
        self._requests: Dict[str, Dict[str, Any]] = {}
        self._attached_page_ids = set()
        self._write(
            {
                "version": 1,
                "started_at": self._now(),
                "updated_at": self._now(),
                "pages": [],
                "apis": [],
                "requests": [],
            }
        )

    @staticmethod
    def _now() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _write(self, data: Dict[str, Any]) -> None:
        parent = os.path.dirname(self.path)
        os.makedirs(parent, exist_ok=True)
        tmp = self.path + ".tmp"
        with self._lock:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)

    def _flush(self) -> None:
        self._write(
            {
                "version": 1,
                "updated_at": self._now(),
                "pages": list(self._pages.values())[-20:],
                "apis": list(self._apis.values())[-80:],
                "requests": list(self._requests.values())[-80:],
            }
        )

    def attach(self, page: Any) -> None:
        if page is None:
            return
        marker = id(page)
        if marker in self._attached_page_ids:
            return
        self._attached_page_ids.add(marker)
        page.on("request", self._on_request)
        page.on("response", self._on_response)

    def latest_product_snapshot(self) -> Dict[str, Any]:
        items = [
            item
            for item in self._apis.values()
            if item.get("path")
            in (PRODUCT_PLAN_API_PATHS | PRODUCT_AD_LIST_API_PATHS)
        ]
        # _apis is keyed by API path. Updating an existing path does not move it
        # to the end of the dict, so insertion order can leave a stale plan
        # snapshot after the newly opened detail response. Merge by observation
        # time to ensure the most recently returned plan wins.
        items.sort(key=lambda item: str(item.get("observed_at") or ""))
        snapshots = [item.get("product_snapshot") or {} for item in items]
        return merge_product_scene_snapshots(snapshots)

    def authorized_accounts(self) -> List[Dict[str, str]]:
        """返回只读响应中明确同时出现账户ID和账户名称的候选账户。"""
        result: Dict[str, Dict[str, str]] = {}
        for item in list(self._apis.values()) + list(self._requests.values()):
            for account in item.get("account_candidates") or []:
                aid = str((account or {}).get("aavid") or "").strip()
                name = str((account or {}).get("account_name") or "").strip()
                if aid.isdigit() and name:
                    result[aid] = {
                        "aavid": aid,
                        "account_name": name[:256],
                    }
        return list(result.values())

    def confirmed_product_target(self) -> Optional[Dict[str, Any]]:
        """返回当前商品全域页已由主计划接口确认的目标。"""
        product_context = False
        for page in reversed(list(self._pages.values())):
            if page.get("path") != "/uni-prom":
                continue
            markers = page.get("scene_markers") or {}
            product_context = bool(
                markers.get("product_select")
                and markers.get("product_global")
                and markers.get("control_tools")
            )
            break
        if not product_context:
            return None

        snapshot = self.latest_product_snapshot()
        plan = snapshot.get("plan")
        if not isinstance(plan, dict) or not str(plan.get("ad_id") or "").isdigit():
            return None

        aavids = set()
        for item in list(self._apis.values()) + list(self._requests.values()):
            identifiers = item.get("identifiers") or {}
            aavid = str(identifiers.get("aavid") or "").strip()
            if aavid.isdigit():
                aavids.add(aavid)
            for field in item.get("fields") or []:
                if str(field.get("key") or "") not in {
                    "aavid",
                    "advertiserId",
                }:
                    continue
                value = str(field.get("value") or "").strip()
                if value.isdigit():
                    aavids.add(value)
        if len(aavids) != 1:
            return None
        return {
            "aavid": next(iter(aavids)),
            "ad_id": str(plan["ad_id"]),
            "plan_name": str(plan.get("plan_name") or "")[:256],
            "promotion_scene": "product",
            "plan_system": str(plan.get("plan_system") or "unknown"),
            "snapshot": snapshot,
        }

    async def observe_page(self, page: Any) -> None:
        if page is None:
            return
        try:
            url = str(page.url or "")
            text = await page.locator("body").inner_text(timeout=3000)
        except Exception:
            return
        summary = summarize_page(url, text)
        summary["observed_at"] = self._now()
        key = f"{summary.get('host')}|{summary.get('path')}|{json.dumps(summary.get('scene_markers'), sort_keys=True)}"
        self._pages[key] = summary
        self._flush()

    async def _on_request(self, request: Any) -> None:
        try:
            parsed = urlparse(str(request.url or ""))
            if parsed.netloc != "qianchuan.jinritemai.com":
                return
            path = parsed.path
            if not path.startswith("/ad/api/"):
                return
            identifiers = extract_safe_query_identifiers(str(request.url or ""))
            summary: Dict[str, Any] = {
                "top_keys": [],
                "fields": [],
                "plan_candidates": [],
                "account_candidates": [],
                "nodes_scanned": 0,
            }
            post_data = request.post_data
            if post_data:
                try:
                    summary = summarize_json(json.loads(post_data), max_nodes=120)
                except (TypeError, ValueError, json.JSONDecodeError):
                    pass
            self._requests[path] = {
                "path": path,
                "method": str(request.method or ""),
                "observed_at": self._now(),
                "identifiers": identifiers,
                **summary,
            }
            self._flush()
        except Exception:
            return

    async def _on_response(self, response: Any) -> None:
        try:
            parsed = urlparse(str(response.url or ""))
            if parsed.netloc != "qianchuan.jinritemai.com":
                return
            path = parsed.path
            if not path.startswith("/ad/api/"):
                return
            content_type = str((await response.all_headers()).get("content-type") or "")
            if "json" not in content_type.lower():
                return
            payload = await response.json()
            summary = summarize_json(payload)
            product_snapshot = (
                extract_product_scene_snapshot(payload)
                if path in (PRODUCT_PLAN_API_PATHS | PRODUCT_AD_LIST_API_PATHS)
                else {}
            )
            item = {
                "path": path,
                "status": int(response.status),
                "method": str(response.request.method or ""),
                "observed_at": self._now(),
                "identifiers": extract_safe_query_identifiers(
                    str(response.url or "")
                ),
                **summary,
            }
            if (
                product_snapshot.get("plan")
                or product_snapshot.get("products")
                or product_snapshot.get("materials")
            ):
                item["product_snapshot"] = product_snapshot
            self._apis[path] = item
            self._flush()
        except Exception:
            return
