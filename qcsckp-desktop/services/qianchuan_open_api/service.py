"""千川官方 API 的业务级封装。"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Iterable, Mapping, Optional

from config import ALLOW_LIVE_OFFICIAL_API_WRITES
from .client import ApiResponse, QianchuanOpenApiClient
from .errors import ApiRequestError, OfficialApiWriteDisabled
from .normalizers import (
    build_metric_unit_map,
    id_number,
    normalize_account,
    normalize_control_task,
    normalize_material,
    normalize_operation_log,
    normalize_plan,
    normalize_product,
    require_digit_id,
    stable_material_set,
    text_id,
)


class QianchuanOfficialApiService:
    AUTHORIZED_ADVERTISERS = "/open_api/oauth2/advertiser/get/"
    SHOP_ADVERTISERS = "/open_api/v1.0/qianchuan/shop/advertiser/list/"
    ENTERPRISE_ADVERTISERS = "/open_api/2/ebp/advertiser/list/"
    ADVERTISER_PUBLIC_INFO = "/open_api/2/advertiser/public_info/"
    PLAN_LIST = "/open_api/v1.0/qianchuan/uni_promotion/list/"
    PLAN_DETAIL = "/open_api/v1.0/qianchuan/uni_promotion/ad/detail/"
    PLAN_MATERIALS = "/open_api/v1.0/qianchuan/uni_promotion/ad/material/get/"
    PLAN_PRODUCTS = "/open_api/v1.0/qianchuan/uni_promotion/ad/product/get/"
    REPORT_CONFIG = "/open_api/v1.0/qianchuan/report/uni_promotion/config/get/"
    REPORT_DATA = "/open_api/v1.0/qianchuan/report/uni_promotion/get/"
    CONTROL_LIST = "/open_api/v1.0/qianchuan/uni_promotion/ad/control_task/list/"
    CONTROL_CREATE = "/open_api/v1.0/qianchuan/uni_promotion/ad/control_task/create/"
    CONTROL_UPDATE = "/open_api/v1.0/qianchuan/uni_promotion/ad/control_task/update/"
    CONTROL_STATUS = "/open_api/v1.0/qianchuan/uni_promotion/ad/control_task/status/update/"
    CONTROL_BUDGET = "/open_api/v1.0/qianchuan/uni_promotion/ad/control_task/budget/update/"
    CONTROL_DURATION = "/open_api/v1.0/qianchuan/uni_promotion/ad/control_task/duration/update/"
    OPERATION_LOGS = "/open_api/v1.0/qianchuan/tools/log_search/"

    MARKETING_GOALS = ("LIVE_PROM_GOODS", "VIDEO_PROM_GOODS")

    def __init__(
        self,
        client: Optional[QianchuanOpenApiClient] = None,
        *,
        allow_writes: Optional[bool] = None,
    ) -> None:
        self.client = client or QianchuanOpenApiClient()
        self.allow_writes = ALLOW_LIVE_OFFICIAL_API_WRITES if allow_writes is None else bool(allow_writes)

    def _require_writes(self) -> None:
        if not self.allow_writes:
            raise OfficialApiWriteDisabled(
                "千川官方 API 真实写入未开启；当前只允许读取、规则判断和飞书确认排队"
            )

    def list_authorized_accounts(self) -> list[dict[str, Any]]:
        response = self.client.get(self.AUTHORIZED_ADVERTISERS)
        rows = self.client.extract_items(response.data)
        return [item for item in (normalize_account(row) for row in rows) if item["advertiser_id"]]

    def list_shop_advertisers(self, shop_id: Any, *, permission: str = "QC_AWEME") -> list[dict[str, Any]]:
        sid = require_digit_id(shop_id, "shop_id")
        rows, _ = self.client.get_all_pages(
            self.SHOP_ADVERTISERS,
            {"shop_id": sid, "permission": [permission]},
            page_size=100,
        )
        return [item for item in (normalize_account(row) for row in rows) if item["advertiser_id"]]

    def list_enterprise_advertisers(
        self,
        enterprise_organization_id: Any,
        *,
        account_source: str = "QIANCHUAN",
    ) -> list[dict[str, Any]]:
        """Expand an authorized enterprise/BP subject to final ad accounts."""
        enterprise_id = require_digit_id(
            enterprise_organization_id, "enterprise_organization_id"
        )
        source = str(account_source or "").strip().upper()
        if source not in {"AD", "LOCAL", "QIANCHUAN"}:
            raise ValueError("account_source must be AD, LOCAL, or QIANCHUAN")
        rows, _ = self.client.get_all_pages(
            self.ENTERPRISE_ADVERTISERS,
            {
                "enterprise_organization_id": enterprise_id,
                "account_source": source,
            },
            page_size=100,
        )
        return [
            item
            for item in (normalize_account(row) for row in rows)
            if item["advertiser_id"]
        ]

    def list_advertiser_public_info(
        self, advertiser_ids: Iterable[Any]
    ) -> list[dict[str, Any]]:
        """Return authoritative public names for up to 100 advertisers per call."""
        ids = list(dict.fromkeys(require_digit_id(value, "advertiser_id") for value in advertiser_ids))
        result: list[dict[str, Any]] = []
        for offset in range(0, len(ids), 100):
            batch = ids[offset : offset + 100]
            response = self.client.get(
                self.ADVERTISER_PUBLIC_INFO,
                {"advertiser_ids": batch},
            )
            result.extend(
                item
                for item in (
                    normalize_account(row)
                    for row in QianchuanOpenApiClient.extract_items(response.data)
                )
                if item["advertiser_id"]
            )
        return result

    def list_business_accounts(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Resolve OAuth subjects to final Qianchuan advertiser accounts.

        A shop subject is not itself an advertiser.  It must be expanded through
        ``shop/advertiser/list`` before any plan or write API is called.
        """
        authorized = self.list_authorized_accounts()
        resolved: dict[str, dict[str, Any]] = {}
        evidence: dict[str, Any] = {"complete": True, "subjects": []}
        for subject in authorized:
            role = str(subject.get("role") or "").upper()
            subject_id = text_id(subject.get("advertiser_id"))
            shop_id = text_id(subject.get("shop_id"))
            is_shop = "SHOP" in role or bool(shop_id)
            if not is_shop:
                is_enterprise = any(
                    marker in role for marker in ("ENTERPRISE", "BP_OPERATOR")
                )
                if subject_id and is_enterprise:
                    try:
                        rows = self.list_enterprise_advertisers(subject_id)
                        for row in rows:
                            row = dict(row)
                            row["enterprise_organization_id"] = subject_id
                            resolved[text_id(row.get("advertiser_id"))] = row
                        evidence["subjects"].append(
                            {
                                "subject_id": subject_id,
                                "role": role,
                                "resolved": len(rows),
                                "type": "enterprise",
                                "account_source": "QIANCHUAN",
                            }
                        )
                    except Exception as exc:
                        evidence["complete"] = False
                        evidence["subjects"].append(
                            {
                                "subject_id": subject_id,
                                "role": role,
                                "resolved": 0,
                                "type": "enterprise",
                                "account_source": "QIANCHUAN",
                                "error": str(exc),
                            }
                        )
                    continue
                is_final_advertiser = "ADVERTISER" in role and not any(
                    marker in role for marker in ("OPERATOR", "ENTERPRISE", "AGENT", "BP")
                )
                if subject_id and is_final_advertiser:
                    resolved[subject_id] = subject
                    evidence["subjects"].append(
                        {"subject_id": subject_id, "role": role, "resolved": 1, "type": "advertiser"}
                    )
                else:
                    # Enterprise/BP/operator identities are OAuth subjects,
                    # not final Qianchuan advertiser accounts.  Presenting one
                    # in the selector leads to an account that plan APIs cannot
                    # read, so keep it as evidence only.
                    evidence["subjects"].append(
                        {
                            "subject_id": subject_id,
                            "role": role,
                            "resolved": 0,
                            "type": "unsupported_subject",
                            "reason": "not_final_advertiser",
                            "ignored": True,
                        }
                    )
                continue
            sid = shop_id or subject_id
            try:
                rows = self.list_shop_advertisers(sid)
                for row in rows:
                    row = dict(row)
                    row["shop_id"] = sid
                    if len(rows) == 1 and not row.get("advertiser_name"):
                        row["advertiser_name"] = subject.get("advertiser_name") or ""
                    resolved[text_id(row.get("advertiser_id"))] = row
                evidence["subjects"].append(
                    {"subject_id": subject_id, "shop_id": sid, "role": role, "resolved": len(rows), "type": "shop"}
                )
            except Exception as exc:
                evidence["complete"] = False
                evidence["subjects"].append(
                    {"subject_id": subject_id, "shop_id": sid, "role": role, "resolved": 0, "type": "shop", "error": str(exc)}
                )
        missing_name_ids = [
            account_id
            for account_id, account in resolved.items()
            if not str(account.get("advertiser_name") or "").strip()
        ]
        evidence["account_names_complete"] = not missing_name_ids
        if missing_name_ids:
            try:
                public_rows = self.list_advertiser_public_info(missing_name_ids)
                public_by_id = {
                    text_id(row.get("advertiser_id")): row for row in public_rows
                }
                for account_id in missing_name_ids:
                    public = public_by_id.get(account_id) or {}
                    public_name = str(public.get("advertiser_name") or "").strip()
                    if public_name:
                        resolved[account_id]["advertiser_name"] = public_name
                evidence["account_names_complete"] = all(
                    str(account.get("advertiser_name") or "").strip()
                    for account in resolved.values()
                )
            except Exception as exc:
                # Account IDs are still valid and may be selected.  A name lookup
                # failure must not make the plan catalog itself incomplete.
                evidence["account_name_error"] = str(exc)
        return list(resolved.values()), evidence

    @staticmethod
    def _default_plan_window() -> tuple[str, str]:
        end = datetime.now()
        start = end - timedelta(days=179)
        return start.strftime("%Y-%m-%d 00:00:00"), end.strftime("%Y-%m-%d 23:59:59")

    def list_plans(
        self,
        advertiser_id: Any,
        *,
        marketing_goal: str,
        adlab_scene: str = "",
        start_time: str = "",
        end_time: str = "",
        fields: Optional[Iterable[str]] = None,
        filtering: Optional[Mapping[str, Any]] = None,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        aid = require_digit_id(advertiser_id, "advertiser_id")
        goal = str(marketing_goal or "").strip().upper()
        if goal not in self.MARKETING_GOALS:
            raise ValueError("marketing_goal 仅支持 LIVE_PROM_GOODS 或 VIDEO_PROM_GOODS")
        scene = str(adlab_scene or "").strip().upper()
        if scene and scene not in {"UNI_PROJECT", "OVERALL_PROJECT"}:
            raise ValueError("adlab_scene 仅支持 UNI_PROJECT 或 OVERALL_PROJECT")
        if not start_time or not end_time:
            start_time, end_time = self._default_plan_window()
        query: dict[str, Any] = {
            "advertiser_id": aid,
            "start_time": start_time,
            "end_time": end_time,
            "marketing_goal": goal,
            "fields": list(fields or []),
        }
        if scene:
            query["adlab_scene"] = scene
        if filtering:
            query["filtering"] = dict(filtering)
        rows, request_ids = self.client.get_all_pages(
            self.PLAN_LIST, query, advertiser_id=aid, page_size=100
        )
        return [normalize_plan(row, advertiser_id=aid) for row in rows], request_ids

    def list_all_plans(self, advertiser_id: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        aid = require_digit_id(advertiser_id, "advertiser_id")
        combined: list[dict[str, Any]] = []
        evidence: dict[str, Any] = {"complete": True, "classes": {}}
        # The official endpoint defaults to UNI_PROJECT when adlab_scene is
        # omitted.  Query all four classes explicitly or Chengfang plans are
        # silently absent even though the request itself succeeds.
        classes = (
            ("chengfang_live", "LIVE_PROM_GOODS", "OVERALL_PROJECT"),
            ("chengfang_product", "VIDEO_PROM_GOODS", "OVERALL_PROJECT"),
            ("global_live", "LIVE_PROM_GOODS", "UNI_PROJECT"),
            ("global_product", "VIDEO_PROM_GOODS", "UNI_PROJECT"),
        )
        for class_key, goal, scene in classes:
            try:
                plans, request_ids = self.list_plans(
                    aid,
                    marketing_goal=goal,
                    adlab_scene=scene,
                )
                combined.extend(plans)
                evidence["classes"][class_key] = {
                    "complete": True,
                    "count": len(plans),
                    "marketing_goal": goal,
                    "adlab_scene": scene,
                    "request_ids": request_ids,
                }
            except Exception as exc:
                evidence["complete"] = False
                evidence["classes"][class_key] = {
                    "complete": False,
                    "count": 0,
                    "marketing_goal": goal,
                    "adlab_scene": scene,
                    "error": str(exc),
                }
        deduped: dict[str, dict[str, Any]] = {}
        for plan in combined:
            if plan.get("ad_id"):
                deduped[str(plan["ad_id"])] = plan
        return list(deduped.values()), evidence

    def get_plan_detail(self, advertiser_id: Any, ad_id: Any) -> tuple[dict[str, Any], ApiResponse]:
        aid = require_digit_id(advertiser_id, "advertiser_id")
        pid = require_digit_id(ad_id, "ad_id")
        response = self.client.get(
            self.PLAN_DETAIL,
            {"advertiser_id": aid, "ad_id": pid},
            advertiser_id=aid,
        )
        data = response.data if isinstance(response.data, Mapping) else {}
        return normalize_plan(data, advertiser_id=aid), response

    def list_plan_materials(
        self,
        advertiser_id: Any,
        ad_id: Any,
        *,
        start_date: str,
        end_date: str,
        fields: Optional[Iterable[str]] = None,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        aid = require_digit_id(advertiser_id, "advertiser_id")
        pid = require_digit_id(ad_id, "ad_id")
        rows, request_ids = self.client.get_all_pages(
            self.PLAN_MATERIALS,
            {
                "advertiser_id": aid,
                "ad_id": pid,
                "filtering": {
                    "material_type": "VIDEO",
                    "start_date": start_date,
                    "end_date": end_date,
                    "material_select_type": "ALL",
                    "material_status": "ALL",
                },
                "fields": list(fields or []),
            },
            advertiser_id=aid,
            page_size=100,
        )
        materials = [normalize_material(row) for row in rows]
        materials = [item for item in materials if item["material_id"] not in {"", "-2"} and item["material_type"] == "VIDEO"]
        return materials, request_ids

    def list_plan_products(self, advertiser_id: Any, ad_id: Any) -> tuple[list[dict[str, Any]], list[str]]:
        aid = require_digit_id(advertiser_id, "advertiser_id")
        pid = require_digit_id(ad_id, "ad_id")
        rows, request_ids = self.client.get_all_pages(
            self.PLAN_PRODUCTS,
            {"advertiser_id": aid, "ad_id": pid},
            advertiser_id=aid,
            page_size=100,
        )
        return [normalize_product(row) for row in rows], request_ids

    def get_report_config(self, advertiser_id: Any, *, marketing_goal: str) -> tuple[dict[str, str], ApiResponse]:
        aid = require_digit_id(advertiser_id, "advertiser_id")
        response = self.client.get(
            self.REPORT_CONFIG,
            {"advertiser_id": aid, "marketing_goal": str(marketing_goal).upper()},
            advertiser_id=aid,
        )
        return build_metric_unit_map(response.data), response

    def query_report(self, advertiser_id: Any, query: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
        aid = require_digit_id(advertiser_id, "advertiser_id")
        payload = dict(query)
        payload["advertiser_id"] = aid
        page_size = int(payload.pop("page_size", 100) or 100)
        return self.client.get_all_pages(
            self.REPORT_DATA,
            payload,
            advertiser_id=aid,
            page_size=page_size,
        )

    def list_control_tasks(
        self,
        advertiser_id: Any,
        *,
        ad_id: Any,
        marketing_goal: str,
        start_time: str,
        end_time: str,
        scene: str = "MATERIAL_ADD_BUDGET",
    ) -> tuple[list[dict[str, Any]], list[str]]:
        aid = require_digit_id(advertiser_id, "advertiser_id")
        pid = require_digit_id(ad_id, "ad_id")
        rows, request_ids = self.client.get_all_pages(
            self.CONTROL_LIST,
            {
                "advertiser_id": aid,
                "ad_id": pid,
                "marketing_goal": str(marketing_goal).upper(),
                "start_time": start_time,
                "end_time": end_time,
                "scene": scene,
            },
            advertiser_id=aid,
            page_size=100,
        )
        return [normalize_control_task(row) for row in rows], request_ids

    def find_duplicate_control_task(
        self,
        advertiser_id: Any,
        *,
        ad_id: Any,
        marketing_goal: str,
        budget: Any,
        duration: Any,
        material_ids: Iterable[Any],
    ) -> Optional[dict[str, Any]]:
        now = datetime.now()
        tasks, _ = self.list_control_tasks(
            advertiser_id,
            ad_id=ad_id,
            marketing_goal=marketing_goal,
            start_time=(now - timedelta(days=7)).strftime("%Y-%m-%d 00:00:00"),
            end_time=(now + timedelta(days=1)).strftime("%Y-%m-%d 23:59:59"),
        )
        wanted = set(stable_material_set(material_ids))
        wanted_budget = Decimal(str(budget))
        wanted_duration = Decimal(str(duration))
        for task in tasks:
            existing = set(stable_material_set(task.get("material_ids") or []))
            try:
                same_numbers = Decimal(str(task.get("budget"))) == wanted_budget and Decimal(str(task.get("duration"))) == wanted_duration
            except Exception:
                same_numbers = False
            status = str(task.get("status") or "").upper()
            terminal = status in {"DISABLED", "FINISHED", "DELETED", "ENDED"}
            # Only an identical frozen group is a duplicate.  Overlapping
            # groups are an intentional v0.1.46 feature and must remain legal.
            if not terminal and existing == wanted and same_numbers:
                return task
        return None

    def create_material_control_task(
        self,
        advertiser_id: Any,
        *,
        ad_id: Any,
        marketing_goal: str,
        name: str,
        budget: Any,
        duration: Any,
        material_ids: Iterable[Any],
        extra: Optional[Mapping[str, Any]] = None,
    ) -> ApiResponse:
        self._require_writes()
        aid = require_digit_id(advertiser_id, "advertiser_id")
        pid = require_digit_id(ad_id, "ad_id")
        materials = stable_material_set(material_ids)
        if not 1 <= len(materials) <= 20:
            raise ValueError("每条追投调控任务必须包含 1 至 20 条视频素材")
        money = Decimal(str(budget))
        hours = Decimal(str(duration))
        if money < Decimal("100"):
            raise ValueError("追投预算不得低于 100 元")
        if hours < Decimal("0.5") or hours > Decimal("24"):
            raise ValueError("追投时长须为 0.5 至 24 小时")
        duplicate = self.find_duplicate_control_task(
            aid,
            ad_id=pid,
            marketing_goal=marketing_goal,
            budget=money,
            duration=hours,
            material_ids=materials,
        )
        if duplicate:
            raise ApiRequestError(f"发现可能重复的现有追投调控任务 {duplicate.get('task_id')}")
        body: dict[str, Any] = {
            "advertiser_id": id_number(aid, "advertiser_id"),
            "ad_id": id_number(pid, "ad_id"),
            "name": str(name or "素材追投")[:100],
            "scene": "MATERIAL_ADD_BUDGET",
            "budget": float(money),
            "duration": float(hours),
            "material_type": "VIDEO",
            "material_ids": [id_number(value, "material_id") for value in materials],
        }
        for key, value in dict(extra or {}).items():
            if key not in body and key not in {"task_ids", "opt_type", "scene"}:
                body[key] = value
        return self.client.post(self.CONTROL_CREATE, body, advertiser_id=aid)

    def update_control_status(self, advertiser_id: Any, task_ids: Iterable[Any], *, action: str) -> ApiResponse:
        self._require_writes()
        aid = require_digit_id(advertiser_id, "advertiser_id")
        ids = tuple(dict.fromkeys(require_digit_id(value, "task_id") for value in task_ids))
        if not 1 <= len(ids) <= 10:
            raise ValueError("一次只能操作 1 至 10 条已验证调控任务")
        opt_type = str(action or "").strip().upper()
        if opt_type not in {"PAUSE", "DISABLE", "ENABLE"}:
            raise ValueError("调控任务状态仅允许 PAUSE、DISABLE 或 ENABLE；禁止自动 DELETE")
        return self.client.post(
            self.CONTROL_STATUS,
            {
                "advertiser_id": id_number(aid, "advertiser_id"),
                "task_ids": [id_number(value, "task_id") for value in ids],
                "opt_type": opt_type,
            },
            advertiser_id=aid,
        )

    def update_control_budget(self, advertiser_id: Any, task_id: Any, budget: Any) -> ApiResponse:
        self._require_writes()
        aid = require_digit_id(advertiser_id, "advertiser_id")
        return self.client.post(
            self.CONTROL_BUDGET,
            {"advertiser_id": id_number(aid, "advertiser_id"), "task_id": id_number(task_id, "task_id"), "budget": float(Decimal(str(budget)))},
            advertiser_id=aid,
        )

    def update_control_duration(self, advertiser_id: Any, task_id: Any, duration: Any) -> ApiResponse:
        self._require_writes()
        aid = require_digit_id(advertiser_id, "advertiser_id")
        return self.client.post(
            self.CONTROL_DURATION,
            {"advertiser_id": id_number(aid, "advertiser_id"), "task_id": id_number(task_id, "task_id"), "duration": float(Decimal(str(duration)))},
            advertiser_id=aid,
        )

    def list_operation_logs(
        self,
        advertiser_id: Any,
        *,
        start_time: str,
        end_time: str,
        object_type: str = "ACCOUNT",
        object_id: Any = "",
    ) -> tuple[list[dict[str, Any]], list[str]]:
        aid = require_digit_id(advertiser_id, "advertiser_id")
        kind = str(object_type or "ACCOUNT").strip().upper()
        if kind not in {"ACCOUNT", "AD"}:
            raise ValueError("操作日志 object_type 仅支持 ACCOUNT 或 AD")
        query: dict[str, Any] = {
            "advertiser_id": aid,
            "object_type": kind,
            "start_time": start_time,
            "end_time": end_time,
        }
        if kind == "AD":
            query["object_id"] = require_digit_id(object_id, "object_id")
        rows, request_ids = self.client.get_all_pages(
            self.OPERATION_LOGS, query, advertiser_id=aid, page_size=20
        )
        return [normalize_operation_log(row) for row in rows], request_ids
