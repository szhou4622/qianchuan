"""千川计划识别用只读探针。

只保存脱敏后的页面路径、可见场景信号、计划/商品/素材候选字段和 API 路径。
请求体只经过字段白名单摘要后保存，不保存 Cookie、Token、请求头、完整 URL、
原始请求体或原始响应。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import tempfile
import threading
import time
from copy import deepcopy
from datetime import datetime
from typing import Any, Dict, Iterable, List, Mapping, Optional
from urllib.parse import unquote, urlparse

from services.product_scene_adapter import (
    PRODUCT_AD_LIST_API_PATHS,
    PRODUCT_PLAN_API_PATHS,
    extract_product_scene_snapshot,
    extract_safe_query_identifiers,
    merge_product_scene_snapshots,
    validate_exact_product_plan_payload,
)
from services.plan_system import detect_plan_system

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
    "advId",
    "adv_id",
    "advertiserId",
    "advertiser_id",
    "aavid",
    "accountId",
    "account_id",
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
    "advName",
    "adv_name",
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
ACCOUNT_NAME_KEYS = (
    ("advName", 100),
    ("adv_name", 100),
    ("advertiserName", 95),
    ("advertiser_name", 95),
    ("accountName", 90),
    ("account_name", 90),
    # 千川部分旧接口中的 userInfoName 是登录用户或店铺展示名，
    # 只能作为没有明确广告主名称时的弱兜底。
    ("userInfoName", 10),
    ("user_info_name", 10),
)
ACCOUNT_LIST_PATH = "/ad/api/v1/account/user-list"

# 千川四类计划共用同一个只读目录接口，真正区分计划体系和推广场景的
# 是请求中的数据集与营销目标。目录同步应以这些后台契约为准；页面文字
# 只负责让千川 SPA 建立登录上下文，不能作为计划分类的主证据。
CATALOG_DATASET_CLASS = {
    "overall_roi_promotion_list_for_product": ("product", "global"),
    "site_promotion_list": ("live", "global"),
    "overall_roi_promotion_list_for_product_v2": ("product", "chengfang"),
    "overall_roi_promotion_list_for_live_v2": ("live", "chengfang"),
}
CATALOG_CLASS_DATASET = {
    value: key for key, value in CATALOG_DATASET_CLASS.items()
}
CATALOG_REQUIRED_PATH = "/ad/api/pmc/v1/uni-promotion/ad/list-required"


_PATH_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: Dict[str, threading.RLock] = {}


def _path_write_lock(path: str) -> threading.RLock:
    """同一进程内所有探针实例共享目标文件锁。"""
    key = os.path.normcase(os.path.abspath(path))
    with _PATH_LOCKS_GUARD:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PATH_LOCKS[key] = lock
        return lock


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
    account_candidates: List[Dict[str, Any]] = []
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
                or candidate.get("advId")
                or candidate.get("adv_id")
                or candidate.get("advertiserId")
                or candidate.get("advertiser_id")
                or candidate.get("accountId")
                or candidate.get("account_id")
            )
            account_name = ""
            account_name_source = ""
            account_name_priority = 0
            for name_key, priority in ACCOUNT_NAME_KEYS:
                if candidate.get(name_key):
                    account_name = candidate[name_key]
                    account_name_source = name_key
                    account_name_priority = priority
                    break
            if (
                account_id
                and str(account_id).isdigit()
                and account_name
            ):
                account = {
                    "aavid": str(account_id),
                    "account_name": str(account_name)[:256],
                    "name_source": account_name_source,
                    "name_priority": account_name_priority,
                }
                if (
                    account not in account_candidates
                    and len(account_candidates) < 100
                ):
                    account_candidates.append(account)
            # 千川右上角“切换”面板使用 userAccountInfos，
            # 行内字段只有 id/name，不应被当成广告计划。
            if (
                "userAccountInfos" in path
                and str(candidate.get("id") or "").isdigit()
                and candidate.get("name")
            ):
                account = {
                    "aavid": str(candidate["id"]),
                    "account_name": str(candidate["name"])[:256],
                    "name_source": "userAccountInfos.name",
                    "name_priority": 110,
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
        # 目录刷新、可见登录和采集服务可能各自创建探针。实例锁无法阻止
        # 它们同时写同一个 .tmp；必须按绝对路径共享锁。
        self._lock = _path_write_lock(self.path)
        self._pages: Dict[str, Dict[str, Any]] = {}
        self._apis: Dict[str, Dict[str, Any]] = {}
        self._requests: Dict[str, Dict[str, Any]] = {}
        self._pagination_replays = set()
        self._pagination_inflight = set()
        self._all_status_replays = set()
        self._full_catalog_request_variants = set()
        self._catalog_replay_templates: Dict[
            tuple[str, str, str, str], Dict[str, Any]
        ] = {}
        # 只在内存中保留页面已经成功发出的目录请求模板。模板不会写入
        # probe 文件；随后只替换账户、数据集、场景和分页字段，复用同一
        # 登录会话读取四类目录，避免依赖页面导航文字和 DOM 结构。
        self._catalog_base_templates: Dict[Any, Dict[str, Any]] = {}
        self._attached_page_ids = set()
        self._catalog_context: Dict[str, str] = {}
        # 千川把一份计划目录拆成 required + optional 两段。optional 请求
        # 只携带短期 SessionID，不重复携带推广方式；仅在本次进程内关联，
        # 绝不写入 probe 文件。
        self._catalog_session_context: Dict[
            str, tuple[str, str, str]
        ] = {}
        self._catalog_pending_session_payloads: Dict[
            str, List[Dict[str, Any]]
        ] = {}
        self._catalog_rows: Dict[tuple[str, str, str, str], Dict[str, Any]] = {}
        self._catalog_variants: Dict[
            tuple[str, str, str, str], Dict[str, Any]
        ] = {}
        self._authorized_account_total = 0
        self._authorized_account_pages_seen = set()
        self._authorized_accounts_ui: Dict[str, Dict[str, str]] = {}
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
        with self._lock:
            tmp = ""
            try:
                # 临时文件必须与目标文件处于同一目录，保证 os.replace
                # 仍是原子替换；唯一文件名避免旧版遗留的固定 .tmp 冲突。
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=parent,
                    prefix=f".{os.path.basename(self.path)}.",
                    suffix=".tmp",
                    delete=False,
                ) as handle:
                    tmp = handle.name
                    json.dump(data, handle, ensure_ascii=False, indent=2)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp, self.path)
                tmp = ""
                # v0.1.40 使用的固定临时文件可能在异常退出后留下；它
                # 不再参与写入，仅在未被占用时尽力清理。
                legacy_tmp = self.path + ".tmp"
                try:
                    if os.path.isfile(legacy_tmp):
                        os.remove(legacy_tmp)
                except OSError:
                    pass
            finally:
                if tmp:
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass

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

    def set_catalog_context(
        self,
        *,
        aavid: Any,
        promotion_scene: Any,
        plan_system: Any,
    ) -> None:
        """标记接下来只读列表响应所属的账户、推广方式和计划体系。"""
        aid = str(aavid or "").strip()
        scene = str(promotion_scene or "").strip().lower()
        system = str(plan_system or "").strip().lower()
        self._catalog_context = {
            "aavid": aid if aid.isdigit() else "",
            "promotion_scene": scene if scene in {"live", "product"} else "",
            "plan_system": (
                system if system in {"global", "chengfang"} else "unknown"
            ),
        }

    def reset_catalog_class(
        self,
        *,
        aavid: Any,
        promotion_scene: Any,
        plan_system: Any,
    ) -> None:
        prefix = (
            str(aavid or "").strip(),
            str(promotion_scene or "").strip().lower(),
            str(plan_system or "").strip().lower(),
        )
        self._catalog_rows = {
            key: value
            for key, value in self._catalog_rows.items()
            if key[:3] != prefix
        }
        self._catalog_variants = {
            key: value
            for key, value in self._catalog_variants.items()
            if key[:3] != prefix
        }

    @staticmethod
    def _request_catalog_scene(body: Any) -> str:
        if not isinstance(body, Mapping):
            return ""
        params = body.get("Params")
        params = params if isinstance(params, Mapping) else {}
        ad_filter = params.get("AdFilter")
        ad_filter = ad_filter if isinstance(ad_filter, Mapping) else {}
        raw = (
            ad_filter.get("MarGoal")
            or ad_filter.get("marGoal")
            or body.get("mar_goal")
            or body.get("marGoal")
        )
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return ""
        return "product" if value == 1 else ("live" if value == 2 else "")

    @staticmethod
    def _request_catalog_dataset(body: Any) -> str:
        if not isinstance(body, Mapping):
            return ""
        params = body.get("Params")
        params = params if isinstance(params, Mapping) else body.get("params")
        params = params if isinstance(params, Mapping) else {}
        value = (
            body.get("dataSetKey")
            or body.get("DataSetKey")
            or body.get("SophonxDataSetKey")
            or params.get("dataSetKey")
            or params.get("DataSetKey")
            or params.get("SophonxDataSetKey")
            or ""
        )
        return str(value or "").strip()

    @classmethod
    def _request_catalog_class(cls, body: Any) -> tuple[str, str]:
        """Return (promotion_scene, plan_system) from explicit API fields."""
        dataset = cls._request_catalog_dataset(body)
        mapped = CATALOG_DATASET_CLASS.get(dataset)
        if mapped:
            return mapped
        return cls._request_catalog_scene(body), "unknown"

    @staticmethod
    def _request_catalog_aavid(body: Any) -> str:
        if not isinstance(body, Mapping):
            return ""
        value = str(
            body.get("aavid")
            or body.get("aadvid")
            or body.get("advertiserId")
            or ""
        ).strip()
        return value if value.isdigit() else ""

    @staticmethod
    def _request_catalog_session_id(body: Any) -> str:
        if not isinstance(body, Mapping):
            return ""
        return str(
            body.get("SessionID")
            or body.get("sessionId")
            or body.get("session_id")
            or ""
        ).strip()[:256]

    @staticmethod
    def _response_catalog_session_id(payload: Any) -> str:
        if not isinstance(payload, Mapping):
            return ""
        data = payload.get("data")
        if not isinstance(data, Mapping):
            return ""
        return str(
            data.get("sessionId")
            or data.get("SessionID")
            or data.get("session_id")
            or ""
        ).strip()[:256]

    def _catalog_response_context(
        self,
        body: Any,
        payload: Any,
        *,
        fallback_aavid: Any = "",
    ) -> tuple[str, str, str]:
        """Resolve required/optional list segments without current-tab guessing."""
        aid = self._request_catalog_aavid(body) or str(fallback_aavid or "").strip()
        scene, request_system = self._request_catalog_class(body)
        payload_system = detect_plan_system(payload=payload)
        system = (
            request_system
            if request_system in {"global", "chengfang"}
            else (
                payload_system
                if payload_system in {"global", "chengfang"}
                else str(
                    self._catalog_context.get("plan_system") or "unknown"
                )
            )
        )
        request_session = self._request_catalog_session_id(body)
        if not scene and request_session:
            linked = self._catalog_session_context.get(request_session)
            if linked and (not aid or aid == linked[0]):
                return linked
        if (
            aid.isdigit()
            and scene in {"live", "product"}
            and system in {"global", "chengfang"}
        ):
            response_session = self._response_catalog_session_id(payload)
            if response_session:
                self._catalog_session_context[response_session] = (
                    aid,
                    scene,
                    system,
                )
            return aid, scene, system
        return "", "", "unknown"

    @staticmethod
    def _set_catalog_request_identity(
        body: Dict[str, Any],
        *,
        aavid: str,
        promotion_scene: str,
        plan_system: str,
    ) -> Dict[str, Any]:
        """Derive one read-only class request from a real browser request."""
        scene = str(promotion_scene or "").strip().lower()
        system = str(plan_system or "").strip().lower()
        dataset = CATALOG_CLASS_DATASET.get((scene, system), "")
        if not dataset:
            raise ValueError("不支持的计划目录分类")
        result = deepcopy(body)
        aid = str(aavid or "").strip()

        for key in ("aavid", "aadvid", "advertiserId"):
            if key in result or key == "aavid":
                result[key] = aid
                break
        mar_goal = 1 if scene == "product" else 2
        if any(key in result for key in ("mar_goal", "marGoal", "MarGoal")):
            key = next(
                key
                for key in ("mar_goal", "marGoal", "MarGoal")
                if key in result
            )
            result[key] = mar_goal

        dataset_written = False
        for key in ("dataSetKey", "DataSetKey", "SophonxDataSetKey"):
            if key in result:
                result[key] = dataset
                dataset_written = True
        params = result.get("Params")
        params = params if isinstance(params, dict) else result.get("params")
        if isinstance(params, dict):
            for key in ("dataSetKey", "DataSetKey", "SophonxDataSetKey"):
                if key in params:
                    params[key] = dataset
                    dataset_written = True
            ad_filter = params.get("AdFilter")
            ad_filter = (
                ad_filter
                if isinstance(ad_filter, dict)
                else params.get("adFilter")
            )
            if isinstance(ad_filter, dict):
                goal_key = "MarGoal" if "MarGoal" in ad_filter else "marGoal"
                ad_filter[goal_key] = mar_goal
        if not dataset_written:
            result["dataSetKey"] = dataset

        # 乘方目录需要该只读契约标记；传统全域不沿用乘方值。
        for key in ("adlabScene", "AdlabScene"):
            if key in result:
                result[key] = 1 if system == "chengfang" else 0
        for key in ("isOverallRoi", "IsOverallRoi"):
            if key in result:
                result[key] = 1 if system == "chengfang" else 0
        for key in ("smartBidType", "SmartBidType"):
            if key in result and system == "chengfang":
                result[key] = 0

        if "page" in result:
            result["page"] = 1
        if "Page" in result:
            result["Page"] = 1
        if "page_size" in result:
            result["page_size"] = max(100, int(result.get("page_size") or 0))
        if "pageSize" in result:
            result["pageSize"] = max(100, int(result.get("pageSize") or 0))
        if "PageSize" in result:
            result["PageSize"] = max(100, int(result.get("PageSize") or 0))
        return result

    async def fetch_catalog_class_from_backend(
        self,
        page: Any,
        *,
        aavid: Any,
        promotion_scene: Any,
        plan_system: Any,
        timeout_seconds: float = 12.0,
    ) -> bool:
        """Read a complete catalog class through the observed backend API."""
        aid = str(aavid or "").strip()
        scene = str(promotion_scene or "").strip().lower()
        system = str(plan_system or "").strip().lower()
        exact_key = (aid, scene, system)
        deadline = time.monotonic() + max(0.2, float(timeout_seconds))
        while time.monotonic() < deadline:
            if self.catalog_class_status(
                aavid=aid,
                promotion_scene=scene,
                plan_system=system,
            ).get("complete"):
                return True
            has_class_templates = any(
                isinstance(key, tuple) and key and str(key[0]) == aid
                for key in self._catalog_base_templates
            )
            if exact_key in self._catalog_base_templates or (
                aid in self._catalog_base_templates
                and not has_class_templates
            ):
                break
            await asyncio.sleep(0.1)
        # 优先复用页面真实发出的同类请求。商品和直播列表的
        # 请求形状不同，不能像旧逻辑那样用“最后一条请求”同时
        # 派生四类目录，否则会把已返回的商品计划误判为空。
        template = self._catalog_base_templates.get(exact_key)
        if not isinstance(template, Mapping):
            # 兼容旧测试及旧页面仅观察到一种模板的情形。
            template = self._catalog_base_templates.get(aid)
        if not isinstance(template, Mapping):
            return False
        source_body = template.get("body")
        if not isinstance(source_body, dict):
            return False
        send_body = self._set_catalog_request_identity(
            source_body,
            aavid=aid,
            promotion_scene=scene,
            plan_system=system,
        )
        marker = (CATALOG_REQUIRED_PATH, aid, scene, system)
        await self._execute_full_catalog_replay(
            page=page,
            url=str(template.get("url") or CATALOG_REQUIRED_PATH),
            send_body=send_body,
            marker=marker,
        )
        return bool(
            self.catalog_class_status(
                aavid=aid,
                promotion_scene=scene,
                plan_system=system,
            ).get("complete")
        )

    def has_backend_catalog_template(self, aavid: Any) -> bool:
        aid = str(aavid or "").strip()
        return aid in self._catalog_base_templates or any(
            isinstance(key, tuple) and key and str(key[0]) == aid
            for key in self._catalog_base_templates
        )

    @classmethod
    def _request_page_number(cls, body: Any) -> int:
        page_params = cls._request_page_params(body)
        raw = None
        if page_params:
            raw = page_params.get("Page")
            if raw in (None, ""):
                raw = page_params.get("page")
        elif isinstance(body, Mapping):
            raw = body.get("page")
            if raw in (None, ""):
                raw = body.get("Page")
        try:
            return max(1, int(raw or 1))
        except (TypeError, ValueError):
            return 1

    @classmethod
    def _catalog_variant_key(
        cls,
        path: str,
        body: Any,
    ) -> str:
        if not isinstance(body, Mapping):
            return str(path)
        stable = deepcopy(dict(body))
        page_params = cls._request_page_params(stable)
        if page_params:
            if "Page" in page_params:
                page_params["Page"] = 1
            if "page" in page_params:
                page_params["page"] = 1
        else:
            if "page" in stable:
                stable["page"] = 1
            if "Page" in stable:
                stable["Page"] = 1
        # 飞书/千川会话类字段不参与筛选变体身份，也绝不落盘。
        for key in list(stable):
            if any(part in str(key).lower() for part in SENSITIVE_KEY_PARTS):
                stable.pop(key, None)
        digest = hashlib.sha256(
            json.dumps(
                stable,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:20]
        return f"{path}|{digest}"

    def _record_catalog_payload(
        self,
        *,
        path: str,
        body: Any,
        http_status: int,
        payload: Any,
        aavid: Any,
        promotion_scene: Any,
        plan_system: Any,
        full_catalog: bool = False,
    ) -> None:
        """Record one catalog response against an immutable scan context.

        Responses issued by ``page.evaluate(fetch(...))`` can reach the normal
        Playwright response callback after the scanner has already switched to
        another tab.  Recording the replay result directly prevents those
        all-status rows from being attributed to the next catalog class.
        """
        aid = str(aavid or "").strip()
        scene = str(promotion_scene or "").strip().lower()
        system = str(plan_system or "").strip().lower()
        if (
            not aid.isdigit()
            or scene not in {"live", "product"}
            or system not in {"global", "chengfang"}
            or not isinstance(body, Mapping)
        ):
            return
        variant = self._catalog_variant_key(path, body)
        variant_key = (aid, scene, system, variant)
        page_number = self._request_page_number(body)
        current = dict(self._catalog_variants.get(variant_key) or {})
        pages = {
            int(value)
            for value in current.get("seen_pages") or []
            if str(value).isdigit()
        }
        pages.add(page_number)
        business_error = (
            str(payload.get("message") or "")
            if isinstance(payload, Mapping)
            and payload.get("status_code") not in (None, 0)
            else ""
        )
        current.update(
            {
                "path": str(path or ""),
                "status": int(http_status or 0),
                "total_pages": self._response_total_pages(payload),
                "seen_pages": sorted(pages),
                "full_catalog": bool(full_catalog),
                "error": (
                    ""
                    if int(http_status or 0) == 200 and not business_error
                    else (business_error or f"HTTP {http_status}")[:500]
                ),
            }
        )
        self._catalog_variants[variant_key] = current
        snapshot = extract_product_scene_snapshot(payload)
        for row in snapshot.get("ad_rows") or []:
            if not isinstance(row, Mapping):
                continue
            ad_id = str(row.get("ad_id") or "").strip()
            if not ad_id.isdigit():
                continue
            self._catalog_rows[(aid, scene, system, ad_id)] = {
                "aavid": aid,
                "ad_id": ad_id,
                "plan_name": str(row.get("ad_name") or "").strip()[:256],
                "promotion_scene": scene,
                "plan_system": system,
                "platform_status": row.get("platform_status") or "unknown",
                "product_ids": list(row.get("product_ids") or []),
            }

    def _record_catalog_response_segment(
        self,
        *,
        path: str,
        body: Any,
        http_status: int,
        payload: Any,
        fallback_aavid: Any = "",
    ) -> None:
        """Join required/optional response segments regardless of arrival order."""
        aid, scene, system = self._catalog_response_context(
            body,
            payload,
            fallback_aavid=fallback_aavid,
        )
        request_session = self._request_catalog_session_id(body)
        if not (
            aid.isdigit()
            and scene in {"live", "product"}
            and system in {"global", "chengfang"}
        ):
            if request_session:
                pending = self._catalog_pending_session_payloads.setdefault(
                    request_session,
                    [],
                )
                pending.append(
                    {
                        "path": str(path or ""),
                        "body": deepcopy(body) if isinstance(body, Mapping) else {},
                        "http_status": int(http_status or 0),
                        "payload": payload,
                    }
                )
                del pending[:-4]
                if len(self._catalog_pending_session_payloads) > 20:
                    oldest = next(iter(self._catalog_pending_session_payloads))
                    self._catalog_pending_session_payloads.pop(oldest, None)
            return
        variant = self._catalog_variant_key(path, body)
        self._record_catalog_payload(
            path=path,
            body=body,
            http_status=http_status,
            payload=payload,
            aavid=aid,
            promotion_scene=scene,
            plan_system=system,
            full_catalog=(
                self._is_full_catalog_request(body)
                or variant in self._full_catalog_request_variants
            ),
        )
        response_session = self._response_catalog_session_id(payload)
        if not response_session:
            return
        for segment in self._catalog_pending_session_payloads.pop(
            response_session,
            [],
        ):
            segment_body = segment.get("body")
            if not isinstance(segment_body, Mapping):
                continue
            self._record_catalog_payload(
                path=str(segment.get("path") or ""),
                body=segment_body,
                http_status=int(segment.get("http_status") or 0),
                payload=segment.get("payload"),
                aavid=aid,
                promotion_scene=scene,
                plan_system=system,
                full_catalog=False,
            )

    def catalog_rows(
        self,
        *,
        aavid: Any,
        promotion_scene: Any,
        plan_system: Any,
    ) -> List[Dict[str, Any]]:
        prefix = (
            str(aavid or "").strip(),
            str(promotion_scene or "").strip().lower(),
            str(plan_system or "").strip().lower(),
        )
        rows = [
            dict(row)
            for key, row in self._catalog_rows.items()
            if key[:3] == prefix
        ]
        rows.sort(key=lambda item: (str(item.get("plan_name") or ""), item["ad_id"]))
        return rows

    def catalog_class_status(
        self,
        *,
        aavid: Any,
        promotion_scene: Any,
        plan_system: Any,
    ) -> Dict[str, Any]:
        prefix = (
            str(aavid or "").strip(),
            str(promotion_scene or "").strip().lower(),
            str(plan_system or "").strip().lower(),
        )
        variants = [
            dict(item)
            for key, item in self._catalog_variants.items()
            if key[:3] == prefix
        ]
        if not variants:
            return {
                "complete": False,
                "variants": 0,
                "message": "未观察到该分类的计划列表接口",
            }
        full_variants = [
            item for item in variants if bool(item.get("full_catalog"))
        ]
        complete = bool(full_variants) and all(
            not item.get("error")
            and set(item.get("seen_pages") or [])
            >= set(range(1, int(item.get("total_pages") or 1) + 1))
            for item in full_variants
        )
        return {
            "complete": complete,
            "variants": len(variants),
            "message": (
                ""
                if complete
                else (
                    "尚未取得无状态排除条件的完整计划列表"
                    if not full_variants
                    else "该分类存在未完成分页或接口错误"
                )
            ),
        }

    async def discover_authorized_accounts(
        self,
        page: Any,
        *,
        timeout_ms: int = 30_000,
    ) -> List[Dict[str, str]]:
        """通过千川只读“切换账户”面板滚动取得完整授权账户目录。"""
        switch_text = "\u5207\u6362"
        try:
            account_button = page.locator("#navigator-right-account")
            await account_button.wait_for(state="visible", timeout=timeout_ms)
            await account_button.hover()
            switch = page.get_by_text(switch_text, exact=True)
            await switch.last.wait_for(state="visible", timeout=timeout_ms)
            await switch.last.click()
            scroll = page.locator(
                ".qc-ui-navigator-account-list "
                ".tools-vmok-plugin-infinite-scroll"
            )
            await scroll.wait_for(state="visible", timeout=timeout_ms)
            stable = 0
            last_count = -1
            for _ in range(60):
                count = await page.locator(
                    ".qc-ui-navigator-account-item"
                ).count()
                if count == last_count:
                    stable += 1
                else:
                    stable = 0
                    last_count = count
                expected_total = max(
                    0,
                    int(self._authorized_account_total or 0),
                )
                if (
                    expected_total
                    and count >= expected_total
                    and stable >= 2
                ):
                    break
                if not expected_total and stable >= 6:
                    break
                await scroll.evaluate(
                    "(node) => { node.scrollTop = node.scrollHeight; }"
                )
                await page.wait_for_timeout(450)
            raw = await page.locator(
                ".qc-ui-navigator-account-item"
            ).evaluate_all(
                """(nodes) => nodes.map((node) => ({
                    name: (
                        node.querySelector('.account-name')?.getAttribute('title')
                        || node.querySelector('.account-name')?.textContent
                        || ''
                    ).trim(),
                    idText: (
                        node.querySelector('.account-id')?.textContent || ''
                    ).trim()
                }))"""
            )
        except Exception:
            raw = []
        result: Dict[str, Dict[str, str]] = {}
        for item in raw or []:
            match = re.search(r"(\d{8,})", str((item or {}).get("idText") or ""))
            aid = match.group(1) if match else ""
            name = _safe_text((item or {}).get("name"), 256)
            if aid and name:
                result[aid] = {"aavid": aid, "account_name": name}
        self._authorized_accounts_ui = dict(result)
        for item in self.authorized_accounts():
            aid = str(item.get("aavid") or "")
            if aid and aid not in result:
                result[aid] = item
        return list(result.values())

    def authorized_account_catalog_status(self) -> Dict[str, Any]:
        observed = len(
            self._authorized_accounts_ui or {
                str(item.get("aavid") or ""): item
                for item in self.authorized_accounts()
                if str(item.get("aavid") or "")
            }
        )
        total = max(0, int(self._authorized_account_total or 0))
        pages = sorted(int(value) for value in self._authorized_account_pages_seen)
        return {
            "complete": bool(total) and observed >= total,
            "observed": observed,
            "total": total,
            "pages_seen": pages,
        }

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
        result: Dict[str, Dict[str, Any]] = {
            aid: {
                "aavid": aid,
                "account_name": str(item.get("account_name") or "")[:256],
                "name_priority": 120,
            }
            for aid, item in getattr(
                self, "_authorized_accounts_ui", {}
            ).items()
            if aid.isdigit() and str(item.get("account_name") or "").strip()
        }
        for item in list(self._apis.values()) + list(self._requests.values()):
            for account in item.get("account_candidates") or []:
                aid = str((account or {}).get("aavid") or "").strip()
                name = str((account or {}).get("account_name") or "").strip()
                priority = int((account or {}).get("name_priority") or 0)
                current = result.get(aid)
                if (
                    aid.isdigit()
                    and name
                    and (
                        current is None
                        or priority >= int(current.get("name_priority") or 0)
                    )
                ):
                    result[aid] = {
                        "aavid": aid,
                        "account_name": name[:256],
                        "name_priority": priority,
                    }
        return [
            {
                "aavid": str(item["aavid"]),
                "account_name": str(item["account_name"]),
            }
            for item in result.values()
        ]

    def latest_observed_aavid(self) -> str:
        """返回最近一次只读请求明确携带的当前千川账户 ID。"""
        latest_value = ""
        latest_key = ("", -1)
        observations = list(self._requests.values()) + list(self._apis.values())
        for index, item in enumerate(observations):
            identifiers = item.get("identifiers") or {}
            value = str(identifiers.get("aavid") or "").strip()
            if not value.isdigit():
                continue
            key = (str(item.get("observed_at") or ""), index)
            if key >= latest_key:
                latest_key = key
                latest_value = value
        return latest_value

    def latest_observed_detail_ad_id(self, aavid: Any = "") -> str:
        """返回最近一次计划详情请求明确携带的主计划 ID。

        账户选择模式只用这个信号确认“用户已经主动打开计划详情”。计划列表
        响应即使包含很多 adId 也不会命中，避免工具刚打开列表就误关 Chrome。
        """
        expected_aavid = str(aavid or "").strip()
        latest_value = ""
        latest_key = ("", -1)
        detail_paths = {value.lower() for value in PRODUCT_PLAN_API_PATHS}
        observations = list(self._requests.values()) + list(self._apis.values())
        for index, item in enumerate(observations):
            path = str(item.get("path") or "").strip().lower()
            is_detail_request = (
                path in detail_paths
                or "ad-detail" in path
                or "/ad/detail" in path
                or path.endswith("/get-config")
            )
            if not is_detail_request:
                continue

            identifiers = item.get("identifiers") or {}
            item_aavids = {
                str(identifiers.get("aavid") or "").strip(),
            }
            detail_ids = {
                str(identifiers.get("ad_id") or "").strip(),
                str(identifiers.get("adId") or "").strip(),
            }
            for field in item.get("fields") or []:
                field_key = str((field or {}).get("key") or "")
                field_value = str(
                    (field or {}).get("value") or ""
                ).strip()
                if field_key in {
                    "aavid",
                    "advId",
                    "adv_id",
                    "advertiserId",
                    "advertiser_id",
                    "accountId",
                    "account_id",
                }:
                    item_aavids.add(field_value)
                elif field_key in {"adId", "ad_id"}:
                    detail_ids.add(field_value)
            for candidate in item.get("plan_candidates") or []:
                detail_ids.add(
                    str(
                        (candidate or {}).get("adId")
                        or (candidate or {}).get("ad_id")
                        or ""
                    ).strip()
                )
            snapshot_plan = (
                (item.get("product_snapshot") or {}).get("plan") or {}
            )
            detail_ids.add(str(snapshot_plan.get("ad_id") or "").strip())

            item_aavids = {value for value in item_aavids if value.isdigit()}
            if (
                expected_aavid
                and item_aavids
                and expected_aavid not in item_aavids
            ):
                continue
            values = sorted(value for value in detail_ids if value.isdigit())
            if len(values) != 1:
                continue
            key = (str(item.get("observed_at") or ""), index)
            if key >= latest_key:
                latest_key = key
                latest_value = values[0]
        return latest_value

    async def current_account_name(self, page: Any) -> str:
        """只读当前导航栏中已选账户的名称，不展开或导入授权账户目录。"""
        if page is None:
            return ""
        selectors = (
            "#navigator-right-account .account-name",
            "#navigator-right-account [title]",
            "#navigator-right-account",
        )
        for selector in selectors:
            try:
                locator = page.locator(selector).first
                title = str(await locator.get_attribute("title") or "").strip()
                text = str(await locator.inner_text(timeout=1500) or "").strip()
            except Exception:
                continue
            candidate = title or text
            if not candidate:
                continue
            candidate = re.sub(r"\s+", " ", candidate)
            candidate = re.sub(
                r"(?:账户\s*ID|ID)\s*[:：]?\s*\d{8,}",
                "",
                candidate,
                flags=re.IGNORECASE,
            ).strip(" -|")
            if candidate:
                return candidate[:256]
        return ""

    async def wait_for_product_pagination(self, timeout: float = 5.0) -> None:
        """等待已发现的商品计划列表分页只读请求完成。"""
        deadline = time.monotonic() + max(0.1, float(timeout))
        while self._pagination_inflight and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        # page.evaluate 返回后，Playwright 的 response 回调可能仍在解析最后一页。
        await asyncio.sleep(0.1)

    async def verify_catalog_plans(
        self,
        page: Any,
        *,
        aavid: Any,
        promotion_scene: Any,
        plan_system: Any,
        max_plans: int = 500,
    ) -> Dict[str, Any]:
        """逐条读取精确详情；只有账户、计划、场景、体系都一致才返回 verified。"""
        aid = str(aavid or "").strip()
        scene = str(promotion_scene or "").strip().lower()
        expected_system = str(plan_system or "").strip().lower()
        rows = self.catalog_rows(
            aavid=aid,
            promotion_scene=scene,
            plan_system=expected_system,
        )[: max(1, int(max_plans))]
        verified: List[Dict[str, Any]] = []
        rejected: List[Dict[str, str]] = []
        endpoint = (
            "/ad/api/creation/v1/ad/ad-detail-plus"
            if scene == "product"
            else "/ad/api/creation/v1/ad/ad-detail-basic"
        )
        for row in rows:
            ad_id = str(row.get("ad_id") or "").strip()
            try:
                response = await asyncio.wait_for(
                    page.evaluate(
                    """async ({endpoint, aavid, adId, timeoutMs}) => {
                        const query = new URLSearchParams({
                            aavid,
                            adid: adId
                        });
                        const controller = new AbortController();
                        const timer = setTimeout(() => controller.abort(), timeoutMs);
                        try {
                            const result = await fetch(
                                `${endpoint}?${query.toString()}`,
                                {credentials: 'include', signal: controller.signal}
                            );
                            let payload = null;
                            try { payload = await result.json(); } catch (_) {}
                            return {status: result.status, payload};
                        } finally {
                            clearTimeout(timer);
                        }
                    }""",
                    {
                        "endpoint": endpoint,
                        "aavid": aid,
                        "adId": ad_id,
                        "timeoutMs": 8_000,
                    },
                    ),
                    timeout=12.0,
                )
                payload = (
                    response.get("payload")
                    if isinstance(response, Mapping)
                    else None
                )
                if (
                    not isinstance(response, Mapping)
                    or int(response.get("status") or 0) != 200
                    or not isinstance(payload, Mapping)
                ):
                    raise RuntimeError(
                        f"HTTP {response.get('status') if isinstance(response, Mapping) else 0}"
                    )
                reason = validate_exact_product_plan_payload(
                    payload,
                    expected_ad_id=ad_id,
                    require_delivering=False,
                )
                if reason:
                    raise RuntimeError(reason)
                data = payload.get("data")
                data = data if isinstance(data, Mapping) else {}
                detail = data.get("adDetailInfo")
                detail = detail if isinstance(detail, Mapping) else {}
                actual_account = str(
                    detail.get("advId")
                    or detail.get("aavid")
                    or detail.get("advertiserId")
                    or ""
                ).strip()
                if actual_account and actual_account != aid:
                    raise RuntimeError(
                        f"精确详情账户不匹配：期望 {aid}，实际 {actual_account}"
                    )
                raw_goal = detail.get("marGoal")
                if raw_goal not in (None, ""):
                    try:
                        actual_scene = (
                            "product"
                            if int(raw_goal) == 1
                            else ("live" if int(raw_goal) == 2 else "")
                        )
                    except (TypeError, ValueError):
                        actual_scene = ""
                    if actual_scene and actual_scene != scene:
                        raise RuntimeError(
                            f"精确详情推广方式不匹配：期望 {scene}，实际 {actual_scene}"
                        )
                detected_system = detect_plan_system(payload=payload)
                if (
                    detected_system != "unknown"
                    and detected_system != expected_system
                ):
                    raise RuntimeError(
                        "精确详情计划体系与分类页面不一致："
                        f"{detected_system} != {expected_system}"
                    )
                snapshot = extract_product_scene_snapshot(payload)
                plan = snapshot.get("plan") or {}
                verified.append(
                    {
                        **row,
                        "plan_name": str(
                            plan.get("plan_name")
                            or row.get("plan_name")
                            or ""
                        ).strip()[:256],
                        "platform_status": (
                            plan.get("platform_status")
                            or plan.get("delivery_name")
                            or row.get("platform_status")
                            or "unknown"
                        ),
                        "verification_state": "verified",
                        "detail_snapshot": snapshot,
                    }
                )
            except Exception as exc:
                reason = _safe_text(exc, 500)
                resolved = any(
                    marker in reason
                    for marker in (
                        "精确详情账户不匹配",
                        "精确详情推广方式不匹配",
                        "精确详情计划体系与分类页面不一致",
                    )
                )
                rejected.append(
                    {
                        "ad_id": ad_id,
                        "reason": reason,
                        "resolved": resolved,
                    }
                )
        resolved_rejections = sum(
            1 for item in rejected if item.get("resolved")
        )
        return {
            "verified": verified,
            "rejected": rejected,
            "candidate_count": len(rows),
            "complete": len(rows) == len(verified) + resolved_rejections,
        }

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
                    "advId",
                    "adv_id",
                    "advertiserId",
                    "advertiser_id",
                    "accountId",
                    "account_id",
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
            request_key = self._observation_key(path, post_data)
            self._requests[request_key] = {
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
            if path == ACCOUNT_LIST_PATH and isinstance(payload, Mapping):
                account_data = payload.get("data")
                if isinstance(account_data, Mapping):
                    try:
                        self._authorized_account_total = max(
                            self._authorized_account_total,
                            int(account_data.get("totalCount") or 0),
                        )
                    except (TypeError, ValueError):
                        pass
                    try:
                        self._authorized_account_pages_seen.add(
                            int(account_data.get("currentPage") or 1)
                        )
                    except (TypeError, ValueError):
                        pass
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
                or product_snapshot.get("ad_rows")
                or product_snapshot.get("materials")
            ):
                item["product_snapshot"] = product_snapshot
            post_data = getattr(response.request, "post_data", None)
            response_key = self._observation_key(path, post_data)
            self._apis[response_key] = item
            if path in PRODUCT_AD_LIST_API_PATHS:
                try:
                    request_body = json.loads(post_data or "{}")
                except (TypeError, ValueError, json.JSONDecodeError):
                    request_body = {}
                if path == CATALOG_REQUIRED_PATH and isinstance(
                    request_body, dict
                ):
                    template_aavid = (
                        self._request_catalog_aavid(request_body)
                        or str(
                            item.get("identifiers", {}).get("aavid") or ""
                        ).strip()
                    )
                    if template_aavid.isdigit():
                        # 内存模板只用于同一登录会话内派生四类只读请求，
                        # 不写入 probe 文件，也不包含请求头或 Cookie。
                        template = {
                            "url": str(response.url or ""),
                            "body": deepcopy(request_body),
                        }
                        template_scene, template_system = (
                            self._request_catalog_class(request_body)
                        )
                        if (
                            template_scene in {"live", "product"}
                            and template_system in {"global", "chengfang"}
                        ):
                            self._catalog_base_templates[
                                (
                                    template_aavid,
                                    template_scene,
                                    template_system,
                                )
                            ] = template
                        # 仅作为旧页面兼容模板，不再被后续的其他
                        # 场景请求覆盖。
                        self._catalog_base_templates.setdefault(
                            template_aavid,
                            template,
                        )
                self._record_catalog_response_segment(
                    path=path,
                    body=request_body,
                    http_status=int(response.status),
                    payload=payload,
                    fallback_aavid=(
                        str(item.get("identifiers", {}).get("aavid") or "")
                        or str(self._catalog_context.get("aavid") or "")
                    ),
                )
            self._flush()
            if path in PRODUCT_AD_LIST_API_PATHS:
                await self._replay_all_status_variant(
                    response,
                    post_data=post_data,
                )
                await self._replay_remaining_product_pages(
                    response,
                    payload,
                    post_data=post_data,
                )
        except Exception:
            return

    @staticmethod
    def _observation_key(path: str, post_data: Any) -> str:
        """区分同一路径的筛选条件和分页，不保留原始请求体。"""
        raw = str(post_data or "")
        if not raw:
            return str(path)
        digest = hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()
        return f"{path}|{digest[:20]}"

    @staticmethod
    def _request_page_params(body: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(body, dict):
            return None
        params = body.get("Params")
        if not isinstance(params, dict):
            params = body.get("params")
        if not isinstance(params, dict):
            return None
        page_params = params.get("PageParams")
        if not isinstance(page_params, dict):
            page_params = params.get("pageParams")
        return page_params if isinstance(page_params, dict) else None

    @staticmethod
    def _catalog_ad_filter(body: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(body, dict):
            return None
        params = body.get("Params")
        if not isinstance(params, dict):
            params = body.get("params")
        if not isinstance(params, dict):
            return None
        value = params.get("AdFilter")
        if not isinstance(value, dict):
            value = params.get("adFilter")
        return value if isinstance(value, dict) else None

    @classmethod
    def _is_full_catalog_request(cls, body: Any) -> bool:
        if not isinstance(body, dict):
            return False
        ad_filter = cls._catalog_ad_filter(body)
        if ad_filter is None:
            if bool(body.get("__qcsckp_full_catalog__")):
                return True
            # 千川商品目录目前还会发出平铺分页请求。start/end_time
            # 只控制指标窗口，不会排除计划；只要账户、推广方式和分页均
            # 明确，且不存在任何状态筛选字段，就可以作为完整目录证据。
            normalized_keys = {
                re.sub(r"[^a-z0-9]", "", str(key).lower())
                for key in body
            }
            restrictive = {
                "notinecpadstatuses",
                "ecpadstatuses",
                "adstatuses",
                "havingfilter",
            }
            has_pagination = (
                ("page" in body or "Page" in body)
                and any(
                    key in body
                    for key in ("page_size", "pageSize", "PageSize", "limit")
                )
            )
            return bool(
                has_pagination
                and cls._request_catalog_aavid(body)
                and cls._request_catalog_scene(body)
                and not (normalized_keys & restrictive)
            )
        restrictive_keys = {
            "NotInEcpAdStatuses",
            "notInEcpAdStatuses",
            "EcpAdStatuses",
            "ecpAdStatuses",
        }
        params = body.get("Params")
        params = params if isinstance(params, dict) else body.get("params")
        return bool(body.get("__qcsckp_full_catalog__")) and not any(
            key in ad_filter for key in restrictive_keys
        ) and not (
            isinstance(params, dict)
            and ("HavingFilter" in params or "havingFilter" in params)
        )

    async def _replay_all_status_variant(
        self,
        response: Any,
        *,
        post_data: Any,
    ) -> None:
        """基于页面真实请求派生无状态排除/无消耗门槛的只读目录请求。"""
        try:
            body = json.loads(post_data or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            return
        ad_filter = self._catalog_ad_filter(body)
        if ad_filter is None or self._is_full_catalog_request(body):
            return
        params = body.get("Params")
        params_key = "Params"
        if not isinstance(params, dict):
            params = body.get("params")
            params_key = "params"
        if not isinstance(params, dict):
            return
        marker = (
            str(urlparse(str(response.url or "")).path),
            self._request_catalog_aavid(body),
            self._request_catalog_scene(body),
            str(self._catalog_context.get("plan_system") or "unknown"),
        )
        if marker in self._all_status_replays:
            return
        self._all_status_replays.add(marker)
        replay_body = deepcopy(body)
        replay_filter = self._catalog_ad_filter(replay_body)
        replay_params = replay_body.get(params_key)
        if not isinstance(replay_filter, dict) or not isinstance(replay_params, dict):
            return
        for key in (
            "NotInEcpAdStatuses",
            "notInEcpAdStatuses",
            "EcpAdStatuses",
            "ecpAdStatuses",
        ):
            replay_filter.pop(key, None)
        replay_params.pop("HavingFilter", None)
        replay_params.pop("havingFilter", None)
        replay_body["__qcsckp_full_catalog__"] = True
        page_params = self._request_page_params(replay_body)
        if page_params is not None:
            if "Page" in page_params:
                page_params["Page"] = 1
            else:
                page_params["page"] = 1
            if "PageSize" in page_params:
                page_params["PageSize"] = 100
            elif "pageSize" in page_params:
                page_params["pageSize"] = 100
        frame = getattr(response.request, "frame", None)
        page = getattr(frame, "page", None)
        if page is None:
            return
        # __qcsckp_full_catalog__ 仅是本地证据标记，发给平台前移除；
        # response 回调通过正在执行的 marker 识别这次派生请求。
        send_body = deepcopy(replay_body)
        send_body.pop("__qcsckp_full_catalog__", None)
        full_variant = self._catalog_variant_key(
            str(urlparse(str(response.url or "")).path),
            send_body,
        )
        self._full_catalog_request_variants.add(full_variant)
        replay_url = str(response.url or "")
        self._catalog_replay_templates[marker] = {
            "url": replay_url,
            "body": deepcopy(send_body),
        }
        await self._execute_full_catalog_replay(
            page=page,
            url=replay_url,
            send_body=send_body,
            marker=marker,
        )

    async def _execute_full_catalog_replay(
        self,
        *,
        page: Any,
        url: str,
        send_body: Dict[str, Any],
        marker: tuple[str, str, str, str],
    ) -> None:
        """Run a stored all-status request after the catalog page is stable."""
        async def fetch_and_record(body_to_send: Dict[str, Any]) -> Dict[str, Any]:
            result = await page.evaluate(
                """async ({url, body}) => {
                const result = await fetch(url, {
                    method: 'POST',
                    credentials: 'include',
                    headers: {'content-type': 'application/json;charset=UTF-8'},
                    body: JSON.stringify(body)
                });
                let payload = null;
                try { payload = await result.json(); } catch (_) {}
                return {status: result.status, payload};
            }""",
                {"url": str(url or ""), "body": body_to_send},
            )
            result = result if isinstance(result, Mapping) else {}
            payload = result.get("payload")
            self._record_catalog_payload(
                path=marker[0],
                body=body_to_send,
                http_status=int(result.get("status") or 0),
                payload=payload,
                aavid=marker[1],
                promotion_scene=marker[2],
                plan_system=marker[3],
                full_catalog=True,
            )
            return {"status": int(result.get("status") or 0), "payload": payload}

        first = await fetch_and_record(send_body)
        first_payload = first.get("payload")
        if (
            int(first.get("status") or 0) != 200
            or (
                isinstance(first_payload, Mapping)
                and first_payload.get("status_code") not in (None, 0)
            )
        ):
            return
        total_pages = self._response_total_pages(first_payload)
        for page_number in range(2, total_pages + 1):
            next_body = deepcopy(send_body)
            if not self._set_request_page(next_body, page_number):
                break
            page_result = await fetch_and_record(next_body)
            page_payload = page_result.get("payload")
            if (
                int(page_result.get("status") or 0) != 200
                or (
                    isinstance(page_payload, Mapping)
                    and page_payload.get("status_code") not in (None, 0)
                )
            ):
                break
        self._flush()

    async def replay_full_catalog(
        self,
        page: Any,
        *,
        aavid: Any,
        promotion_scene: Any,
        plan_system: Any,
    ) -> bool:
        """Retry captured read-only templates outside the response callback.

        The first attempt happens while the page is still navigating and can
        be cancelled by the browser.  The scanner calls this method after the
        list tab settles so that filtered default requests are reliably
        expanded to all statuses.
        """
        prefix = (
            str(aavid or "").strip(),
            str(promotion_scene or "").strip().lower(),
            str(plan_system or "").strip().lower(),
        )
        templates = [
            (marker, dict(template))
            for marker, template in self._catalog_replay_templates.items()
            if marker[1:] == prefix
        ]
        for marker, template in templates:
            body = template.get("body")
            url = str(template.get("url") or "")
            if not isinstance(body, dict) or not url:
                continue
            try:
                await self._execute_full_catalog_replay(
                    page=page,
                    url=url,
                    send_body=deepcopy(body),
                    marker=marker,
                )
            except Exception:
                continue
        return bool(
            self.catalog_class_status(
                aavid=prefix[0],
                promotion_scene=prefix[1],
                plan_system=prefix[2],
            ).get("complete")
        )

    @classmethod
    def _set_request_page(cls, body: Any, page_number: int) -> bool:
        page_params = cls._request_page_params(body)
        if page_params is not None:
            key = "Page" if "Page" in page_params else "page"
            page_params[key] = int(page_number)
            return True
        if isinstance(body, dict):
            key = "page" if "page" in body else ("Page" if "Page" in body else "")
            if key:
                body[key] = int(page_number)
                return True
        return False

    @staticmethod
    def _response_total_pages(payload: Any) -> int:
        queue = [payload]
        seen = 0
        while queue and seen < 80:
            value = queue.pop(0)
            seen += 1
            if isinstance(value, dict):
                lowered = {
                    str(key).casefold(): one for key, one in value.items()
                }
                for key in ("totalpage", "totalpages"):
                    if key in lowered:
                        try:
                            return max(1, int(lowered[key]))
                        except (TypeError, ValueError):
                            return 1
                if (
                    "totalcount" in lowered
                    and (
                        "pagesize" in lowered
                        or "page_size" in lowered
                        or "limit" in lowered
                    )
                ):
                    try:
                        total = max(0, int(lowered["totalcount"]))
                        size = max(
                            1,
                            int(
                                lowered.get("pagesize")
                                or lowered.get("page_size")
                                or lowered.get("limit")
                            ),
                        )
                        return max(1, (total + size - 1) // size)
                    except (TypeError, ValueError):
                        pass
                for one in value.values():
                    if isinstance(one, (dict, list)):
                        queue.append(one)
            elif isinstance(value, list):
                queue.extend(value[:20])
        return 1

    async def _replay_remaining_product_pages(
        self,
        response: Any,
        payload: Any,
        *,
        post_data: Any,
    ) -> None:
        """沿用当前只读列表请求，补读剩余分页；不保存请求体或响应原文。"""
        if not post_data:
            return
        try:
            body = json.loads(post_data)
        except (TypeError, ValueError, json.JSONDecodeError):
            return
        current_page = self._request_page_number(body)
        if not self._set_request_page(deepcopy(body), current_page):
            return
        total_pages = self._response_total_pages(payload)
        if total_pages <= current_page:
            return
        variant_body = deepcopy(body)
        base_body = deepcopy(variant_body)
        if not self._set_request_page(base_body, 1):
            return
        variant_hash = hashlib.sha256(
            json.dumps(
                base_body,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:20]
        marker = (str(urlparse(str(response.url or "")).path), variant_hash)
        if marker in self._pagination_replays:
            return
        self._pagination_replays.add(marker)
        self._pagination_inflight.add(marker)
        try:
            frame = getattr(response.request, "frame", None)
            page = getattr(frame, "page", None)
            if page is None:
                return
            for page_number in range(current_page + 1, total_pages + 1):
                replay_body = deepcopy(body)
                if not self._set_request_page(replay_body, page_number):
                    break
                replay_result = await page.evaluate(
                    """async ({url, body}) => {
                        const result = await fetch(url, {
                            method: 'POST',
                            credentials: 'include',
                            headers: {'content-type': 'application/json;charset=UTF-8'},
                            body: JSON.stringify(body)
                        });
                        let payload = null;
                        try { payload = await result.json(); } catch (_) {}
                        return {status: result.status, payload};
                    }""",
                    {"url": str(response.url or ""), "body": replay_body},
                )
                # page.evaluate 发起的 fetch 仍会触发 Playwright response 事件；
                # 若平台拒绝签名或业务失败，额外记录错误，避免把分页误报为完整。
                if (
                    not isinstance(replay_result, Mapping)
                    or int(replay_result.get("status") or 0) != 200
                    or (
                        isinstance(replay_result.get("payload"), Mapping)
                        and replay_result["payload"].get("status_code")
                        not in (None, 0)
                    )
                ):
                    break
        finally:
            self._pagination_inflight.discard(marker)
