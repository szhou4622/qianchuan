"""Official API execution adapters with the legacy service result contracts."""

from __future__ import annotations

import asyncio
import json
import traceback
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Mapping, Optional

from services.plan_system import normalize_plan_system
from services.qianchuan_open_api.errors import ApiWriteOutcomeUnknown
from services.qianchuan_open_api.audit import OfficialApiAuditStore
from services.qianchuan_open_api.normalizers import (
    first,
    normalize_plan_system as normalize_api_plan_system,
    normalize_promotion_scene,
    require_digit_id,
    stable_material_set,
    text_id,
)
from services.qianchuan_open_api.runtime import get_official_api_service


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _goal(scene: str) -> str:
    return "LIVE_PROM_GOODS" if str(scene or "").lower() == "live" else "VIDEO_PROM_GOODS"


def _task_id(response: Any) -> str:
    data = getattr(response, "data", {})
    return text_id(first(data, "task_id", "control_task_id", "id"))


def _control_window() -> tuple[str, str]:
    now = datetime.now()
    return (
        (now - timedelta(days=179)).strftime("%Y-%m-%d 00:00:00"),
        (now + timedelta(days=1)).strftime("%Y-%m-%d 23:59:59"),
    )


def _find_control_task(
    service: Any,
    *,
    aavid: Any,
    ad_id: Any,
    promotion_scene: str,
    task_id: Any,
) -> Optional[dict[str, Any]]:
    start_time, end_time = _control_window()
    tasks, _ = service.list_control_tasks(
        aavid,
        ad_id=ad_id,
        marketing_goal=_goal(promotion_scene),
        start_time=start_time,
        end_time=end_time,
    )
    wanted = text_id(task_id)
    return next((item for item in tasks if text_id(item.get("task_id")) == wanted), None)


def _verify_control_task(
    service: Any,
    *,
    aavid: Any,
    ad_id: Any,
    promotion_scene: str,
    task_id: Any,
    material_ids: Optional[list[str]] = None,
    budget: Any = None,
    duration: Any = None,
) -> dict[str, Any]:
    task = _find_control_task(
        service,
        aavid=aavid,
        ad_id=ad_id,
        promotion_scene=promotion_scene,
        task_id=task_id,
    )
    if not task:
        raise RuntimeError("官方 API 返回成功，但调控任务列表尚未查到该任务")
    if text_id(task.get("ad_id")) not in {"", text_id(ad_id)}:
        raise RuntimeError("新调控任务归属的主计划与请求不一致")
    if str(task.get("scene") or "").upper() != "MATERIAL_ADD_BUDGET":
        raise RuntimeError("新调控任务不是素材追投任务")
    if material_ids is not None:
        expected = stable_material_set(material_ids)
        actual = stable_material_set(task.get("material_ids") or [])
        if actual != expected:
            raise RuntimeError("新调控任务的冻结素材集合与请求不一致")
    for field, expected in (("budget", budget), ("duration", duration)):
        if expected is None:
            continue
        try:
            matched = Decimal(str(task.get(field))) == Decimal(str(expected))
        except Exception:
            matched = False
        if not matched:
            raise RuntimeError(f"新调控任务的{field}与请求不一致")
    return task


def _material_is_writable(item: Mapping[str, Any]) -> bool:
    status = str(item.get("material_status") or "").strip().upper()
    audit = str(item.get("audit_status") or "").strip().upper()
    positive_status = {
        "ENABLE", "ENABLED", "ACTIVE", "DELIVERY", "DELIVERING", "RUNNING",
        "AVAILABLE", "投放中", "可投放",
    }
    positive_audit = {
        "PASS", "PASSED", "APPROVED", "AUDIT_PASS", "审核通过",
    }
    # Missing or unrecognised evidence must fail closed before a real write.
    return status in positive_status and audit in positive_audit


def _check_plan(
    service: Any,
    *,
    aavid: Any,
    ad_id: Any,
    promotion_scene: str,
    plan_system: str,
) -> dict[str, Any]:
    detail, _ = service.get_plan_detail(aavid, ad_id)
    if text_id(detail.get("aavid")) != text_id(aavid) or text_id(detail.get("ad_id")) != text_id(ad_id):
        raise RuntimeError("官方 API 计划详情与待执行账户或计划不一致")
    if normalize_promotion_scene(detail.get("marketing_goal")) != str(promotion_scene or "").lower():
        raise RuntimeError("官方 API 计划推广方式与任务快照不一致")
    if normalize_api_plan_system(detail.get("adlab_scene")) != normalize_plan_system(plan_system):
        raise RuntimeError("官方 API 计划体系与任务快照不一致")
    if str(detail.get("platform_status") or "") not in {"active", "learning"}:
        raise RuntimeError("官方 API 返回的主计划当前不可投放")
    return detail


def _retarget_params(retargeting: Mapping[str, Any]) -> tuple[Decimal, Decimal, dict[str, Any]]:
    method = str(retargeting.get("method") or "volume").lower()
    extra: dict[str, Any] = {}
    if method == "volume":
        volume = retargeting.get("volume") if isinstance(retargeting.get("volume"), Mapping) else {}
        return (
            Decimal(str(volume.get("total_budget_yuan"))),
            Decimal(str(volume.get("duration_hours"))),
            extra,
        )
    control = retargeting.get("cost_control") if isinstance(retargeting.get("cost_control"), Mapping) else {}
    goal = str(control.get("optimization_goal") or "net_roi").lower()
    if goal == "net_roi":
        block = control.get("net_roi") if isinstance(control.get("net_roi"), Mapping) else {}
        extra["roi2_goal"] = float(Decimal(str(block.get("net_roi_target"))))
        budget = Decimal(str(block.get("daily_budget_yuan")))
    else:
        block = control.get("live_room") if isinstance(control.get("live_room"), Mapping) else {}
        extra["bid"] = float(Decimal(str(block.get("bid_per_conversion_yuan"))))
        budget = Decimal(str(block.get("daily_budget_yuan")))
    # The official control task endpoint still requires a duration.  Existing
    # cost-control UI has no duration field, so its documented maximum window
    # is used and frozen in the execution audit.
    return budget, Decimal("24"), extra


class OfficialApiRetargetingService:
    def __init__(self, full_config: Optional[Mapping[str, Any]] = None) -> None:
        self.full_config = dict(full_config or {})

    async def close(self) -> None:
        return None

    async def run(
        self,
        *,
        aavid: int,
        ad_id: int,
        material_id: str,
        material_ids: Optional[list[str]] = None,
        retargeting: Optional[Mapping[str, Any]] = None,
        strategy_title: Optional[str] = None,
        target_uid: Optional[str] = None,
        promotion_scene: str = "live",
        plan_system: str = "unknown",
        **_: Any,
    ) -> Any:
        from services.retargeting_service import RetargetingRunResult

        service = get_official_api_service()
        mids = [text_id(value) for value in (material_ids or [material_id]) if text_id(value)]
        rdict = dict(retargeting or {})
        try:
            await asyncio.to_thread(
                _check_plan,
                service,
                aavid=aavid,
                ad_id=ad_id,
                promotion_scene=promotion_scene,
                plan_system=plan_system,
            )
            start_date = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
            end_date = datetime.now().strftime("%Y-%m-%d")
            materials, _ = await asyncio.to_thread(
                service.list_plan_materials,
                aavid,
                ad_id,
                start_date=start_date,
                end_date=end_date,
                fields=[],
            )
            current = {text_id(item.get("material_id")): item for item in materials}
            missing = [mid for mid in mids if mid not in current]
            if missing:
                raise RuntimeError("待追投素材已不属于该计划：" + ",".join(missing))
            for mid in mids:
                if not _material_is_writable(current.get(mid) or {}):
                    raise RuntimeError(f"素材 {mid} 的投放或审核状态未明确可用，已禁止追投")

            budget, duration, extra = _retarget_params(rdict)
            response = await asyncio.to_thread(
                service.create_material_control_task,
                aavid,
                ad_id=ad_id,
                marketing_goal=_goal(promotion_scene),
                name=str(strategy_title or "素材追投")[:100],
                budget=budget,
                duration=duration,
                material_ids=mids,
                extra=extra,
            )
            task_id = _task_id(response)
            if task_id:
                require_digit_id(task_id, "control_task_id")
            if not task_id:
                duplicate = await asyncio.to_thread(
                    service.find_duplicate_control_task,
                    aavid,
                    ad_id=ad_id,
                    marketing_goal=_goal(promotion_scene),
                    budget=budget,
                    duration=duration,
                    material_ids=mids,
                )
                task_id = text_id((duplicate or {}).get("task_id"))
            if not task_id:
                raise RuntimeError("官方 API 已返回成功但未返回可核验的调控任务 ID")
            try:
                verified_task = await asyncio.to_thread(
                    _verify_control_task,
                    service,
                    aavid=aavid,
                    ad_id=ad_id,
                    promotion_scene=promotion_scene,
                    task_id=task_id,
                    material_ids=mids,
                    budget=budget,
                    duration=duration,
                )
            except Exception as verify_exc:
                if response.request_uid:
                    OfficialApiAuditStore().mark_reconciled(
                        response.request_uid,
                        status="unresolved",
                        task_id=task_id,
                        response={"verification_error": str(verify_exc)},
                    )
                raise RuntimeError(
                    "官方 API 创建响应已返回，但调控任务反查未通过；禁止自动重试，请人工核对"
                ) from verify_exc
            if response.request_uid:
                OfficialApiAuditStore().mark_reconciled(
                    response.request_uid,
                    status="confirmed",
                    task_id=task_id,
                    response=verified_task,
                )
            return RetargetingRunResult(
                success=True,
                message="官方 API 追投成功",
                step="done",
                detail=json.dumps({"source": "qianchuan_open_api", "request_id": response.request_id}, ensure_ascii=False),
                aavid=text_id(aavid),
                ad_id=text_id(ad_id),
                material_id=text_id(material_id),
                regulate_task_id=task_id,
                retargeting_method=str(rdict.get("method") or "volume"),
                retargeting_json=json.dumps(rdict, ensure_ascii=False, separators=(",", ":")),
                finished_at=_now(),
                headless=True,
            )
        except ApiWriteOutcomeUnknown as exc:
            try:
                budget, duration, _ = _retarget_params(rdict)
                duplicate = await asyncio.to_thread(
                    service.find_duplicate_control_task,
                    aavid,
                    ad_id=ad_id,
                    marketing_goal=_goal(promotion_scene),
                    budget=budget,
                    duration=duration,
                    material_ids=mids,
                )
            except Exception:
                duplicate = None
            task_id = text_id((duplicate or {}).get("task_id"))
            if exc.request_uid:
                OfficialApiAuditStore().mark_reconciled(
                    exc.request_uid,
                    status="confirmed" if task_id else "unresolved",
                    task_id=task_id,
                    response=duplicate or {},
                )
            return RetargetingRunResult(
                success=bool(task_id),
                message="官方 API 追投已通过调控任务对账确认" if task_id else "官方 API 创建结果未知，已禁止自动重试",
                step="done_reconciled" if task_id else "api_outcome_unknown",
                detail=str(exc),
                aavid=text_id(aavid),
                ad_id=text_id(ad_id),
                material_id=text_id(material_id),
                regulate_task_id=task_id,
                retargeting_method=str(rdict.get("method") or "volume"),
                retargeting_json=json.dumps(rdict, ensure_ascii=False, separators=(",", ":")),
                finished_at=_now(),
                headless=True,
            )
        except Exception as exc:
            return RetargetingRunResult(
                success=False,
                message=str(exc),
                step="official_api",
                detail=traceback.format_exc()[:8000],
                aavid=text_id(aavid),
                ad_id=text_id(ad_id),
                material_id=text_id(material_id),
                retargeting_method=str(rdict.get("method") or "volume"),
                retargeting_json=json.dumps(rdict, ensure_ascii=False, separators=(",", ":")),
                finished_at=_now(),
                headless=True,
            )


class OfficialApiRegulationStopService:
    def __init__(self, full_config: Optional[Mapping[str, Any]] = None) -> None:
        self.full_config = dict(full_config or {})

    async def close(self) -> None:
        return None

    async def run(
        self,
        *,
        aavid: int,
        ad_id: int,
        assist_task_id: str,
        stop_action: str,
        promotion_scene: str = "live",
        plan_system: str = "unknown",
        **_: Any,
    ) -> Any:
        from services.regulation_service import RegulationRunResult

        service = get_official_api_service()
        action = str(stop_action or "").lower()
        opt_type = "PAUSE" if action == "pause" else "DISABLE"
        try:
            await asyncio.to_thread(
                _check_plan,
                service,
                aavid=aavid,
                ad_id=ad_id,
                promotion_scene=promotion_scene,
                plan_system=plan_system,
            )
            exact = await asyncio.to_thread(
                _find_control_task,
                service,
                aavid=aavid,
                ad_id=ad_id,
                promotion_scene=promotion_scene,
                task_id=assist_task_id,
            )
            if not exact:
                raise RuntimeError("官方 API 未找到待停投的素材追投调控任务")
            if str(exact.get("scene") or "").upper() != "MATERIAL_ADD_BUDGET":
                raise RuntimeError("目标不是素材追投调控任务，已禁止操作")
            current_status = str(exact.get("status") or "").upper()
            if current_status in {"PAUSE", "PAUSED"} and opt_type == "PAUSE":
                return RegulationRunResult(True, "调控任务已经暂停", "done_already_paused", "", text_id(aavid), text_id(ad_id), text_id(assist_task_id), action, _now(), True)
            if current_status in {"DISABLE", "DISABLED", "FINISHED", "ENDED"}:
                return RegulationRunResult(True, "调控任务已经结束", "done_already_paused", "", text_id(aavid), text_id(ad_id), text_id(assist_task_id), action, _now(), True)
            response = await asyncio.to_thread(
                service.update_control_status,
                aavid,
                [assist_task_id],
                action=opt_type,
            )
            verified = await asyncio.to_thread(
                _find_control_task,
                service,
                aavid=aavid,
                ad_id=ad_id,
                promotion_scene=promotion_scene,
                task_id=assist_task_id,
            )
            verified_status = str((verified or {}).get("status") or "").upper()
            status_ok = (
                (opt_type == "PAUSE" and verified_status in {"PAUSE", "PAUSED"})
                or (
                    opt_type == "DISABLE"
                    and verified_status in {"DISABLE", "DISABLED", "FINISHED", "ENDED"}
                )
            )
            if response.request_uid:
                OfficialApiAuditStore().mark_reconciled(
                    response.request_uid,
                    status="confirmed" if status_ok else "unresolved",
                    task_id=assist_task_id,
                    response=verified or {},
                )
            if not status_ok:
                raise RuntimeError(
                    "官方 API 停投响应已返回，但任务状态反查未通过；禁止自动重试，请人工核对"
                )
            return RegulationRunResult(
                True,
                "官方 API 停投成功",
                "done",
                json.dumps({"source": "qianchuan_open_api", "request_id": response.request_id, "opt_type": opt_type}, ensure_ascii=False),
                text_id(aavid),
                text_id(ad_id),
                text_id(assist_task_id),
                action,
                _now(),
                True,
            )
        except ApiWriteOutcomeUnknown as exc:
            try:
                exact = await asyncio.to_thread(
                    _find_control_task,
                    service,
                    aavid=aavid,
                    ad_id=ad_id,
                    promotion_scene=promotion_scene,
                    task_id=assist_task_id,
                )
            except Exception:
                exact = None
            status = str((exact or {}).get("status") or "").upper()
            reconciled = (
                (opt_type == "PAUSE" and status in {"PAUSE", "PAUSED"})
                or (opt_type == "DISABLE" and status in {"DISABLE", "DISABLED", "FINISHED", "ENDED"})
            )
            if exc.request_uid:
                OfficialApiAuditStore().mark_reconciled(
                    exc.request_uid,
                    status="confirmed" if reconciled else "unresolved",
                    task_id=assist_task_id,
                    response=exact or {},
                )
            return RegulationRunResult(
                reconciled,
                "官方 API 停投已通过调控任务反查确认" if reconciled else "官方 API 停投结果未知，已禁止自动重试",
                "done_reconciled" if reconciled else "api_outcome_unknown",
                str(exc),
                text_id(aavid),
                text_id(ad_id),
                text_id(assist_task_id),
                action,
                _now(),
                True,
            )
        except Exception as exc:
            return RegulationRunResult(False, str(exc), "official_api", traceback.format_exc()[:8000], text_id(aavid), text_id(ad_id), text_id(assist_task_id), action, _now(), True)
