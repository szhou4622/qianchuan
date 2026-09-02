"""Official API execution adapters with the legacy service result contracts."""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import threading
import time
import traceback
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Mapping, Optional

from services.plan_system import normalize_plan_system
from services.qianchuan_open_api.errors import ApiRateLimitError, ApiWriteOutcomeUnknown
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


CONTROL_TASK_NAME_MAX_LENGTH = 50


class _MaterialRetargetEvidenceCheckError(RuntimeError):
    """The local fail-closed scene evidence could not be evaluated safely."""


def _existing_reconciliation(execution_uid: Optional[str]) -> Optional[dict[str, Any]]:
    """Return a previously submitted immutable execution, if any.

    This lookup happens before a POST.  It is deliberately fail-closed only
    when a durable execution id is present; old callers without one retain
    their existing behavior.
    """
    uid = str(execution_uid or "").strip()
    if not uid:
        return None
    try:
        from services.qianchuan_session import current_session_owner
        from utils.sqlite_store import SQLiteStore

        owner = str(current_session_owner() or "").strip().casefold()
        if not owner:
            return None
        return SQLiteStore().select_one(
            "execution_reconciliation",
            where={"account_username": owner, "idempotency_key": uid},
        )
    except Exception:
        return None


def _cached_material_retarget_evidence(
    *,
    aavid: Any,
    ad_id: Any,
    task_id: Any,
) -> bool:
    """Accept a missing API ``scene`` only with exact fresh local evidence."""
    try:
        from services.qianchuan_session import current_session_owner
        from utils.sqlite_store import SQLiteStore

        owner = str(current_session_owner() or "").strip().casefold()
        if not owner:
            raise _MaterialRetargetEvidenceCheckError("当前工具账号为空")
        rows = SQLiteStore().execute(
            "SELECT t.updated_at FROM pmc_roi2_assist_task t "
            "JOIN promotion_target p ON p.target_uid=t.target_uid "
            "JOIN qianchuan_account q ON q.account_uid=p.account_uid "
            "WHERE LOWER(q.owner_username)=? "
            "AND q.aavid=t.aadvid AND p.aadvid=t.aadvid AND p.ad_id=t.ad_id "
            "AND t.aadvid=? AND t.ad_id=? "
            "AND t.assist_task_id=? AND t.data_source='qianchuan_open_api' "
            "ORDER BY t.updated_at DESC LIMIT 1",
            (owner, text_id(aavid), text_id(ad_id), text_id(task_id)),
            fetch=True,
        ) or []
        if not rows:
            return False
        updated = datetime.strptime(str(rows[0].get("updated_at") or ""), "%Y-%m-%d %H:%M:%S")
        return datetime.now() - updated <= timedelta(minutes=10)
    except _MaterialRetargetEvidenceCheckError:
        raise
    except Exception as exc:
        raise _MaterialRetargetEvidenceCheckError("本地素材追投场景证据查询失败") from exc


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _unique_control_task_name(
    strategy_title: Optional[str],
    execution_uid: Optional[str],
    *,
    now: Optional[datetime] = None,
) -> str:
    """Build a stable, user-readable and per-execution unique task name.

    Qianchuan rejects a new control task when its name is identical to a
    historical task, including a task the user has already closed.  A Feishu
    task UID is stable across lease recovery, so it gives retries of the same
    local execution the same name while different confirmations cannot clash.
    """
    # ``now`` remains in the signature for backward compatibility with tests
    # and callers; uniqueness is represented by a compact 8-character token.
    _ = now or datetime.now()
    raw_uid = str(execution_uid or "").strip()
    if raw_uid:
        # Parent/group task UIDs share their prefix and differ at the end, so
        # truncating the prefix made separate groups collide.  Hash the full
        # UID to keep retries stable while making cards and groups distinct.
        marker = hashlib.sha256(raw_uid.encode("utf-8")).hexdigest()[:8]
        suffix = f"-{marker}"
    else:
        # Callers without a durable execution UID cannot prove recovery
        # idempotency, but still receive a per-submission unique name.
        suffix = f"-{secrets.token_hex(4)}"
    base = str(strategy_title or "素材追投").strip() or "素材追投"
    return f"{base[: max(1, CONTROL_TASK_NAME_MAX_LENGTH - len(suffix))]}{suffix}"


def _configured_control_task_base_name(
    retargeting: Mapping[str, Any],
    strategy_title: Optional[str],
) -> str:
    """Return the user-configured task name, with a safe legacy fallback.

    ``task_name_suffix`` is the persisted field name kept for compatibility
    with existing rule files and the old browser form.  In the official API
    backend it is the user-visible base task name, not merely a cosmetic
    suffix.  A unique execution marker is appended separately by
    :func:`_unique_control_task_name`.
    """
    configured = str((retargeting or {}).get("task_name_suffix") or "").strip()
    if configured:
        return configured
    legacy = str(strategy_title or "").strip()
    return legacy or "素材追投"


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


def classify_stop_status(expected_status: Any, actual_status: Any) -> tuple[str, str]:
    """Classify one read-only control-task status after a stop submission.

    PAUSE is an update action, not a task-list state.  Verified platform
    history records a successful pause as DISABLE.  OFFLINE_TIME means the
    task naturally expired while verification was pending; the stop objective
    is complete but must not be described as a confirmed pause action.
    """

    expected = str(expected_status or "PAUSE").strip().upper()
    actual = str(actual_status or "").strip().upper()
    if actual == "OFFLINE_TIME":
        return "terminal_natural", "调控任务已自然到期"
    if actual in {"DISABLE", "DISABLED", "FINISHED", "ENDED"}:
        if expected == "PAUSE":
            return "confirmed", "调控任务已暂停，平台状态为 DISABLE"
        return "confirmed", "调控任务已结束"
    if actual == "PROCESSING":
        return "pending", "平台任务仍在调控中"
    return "pending", f"平台任务状态尚未完成停投核验，当前为 {actual or 'unknown'}"


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
    # The list endpoint is already filtered by MATERIAL_ADD_BUDGET.  Current
    # production responses may omit ``scene`` entirely, so only reject an
    # explicit conflicting value.  All remaining identity, material, budget
    # and duration checks below still have to pass.
    returned_scene = str(task.get("scene") or "").strip().upper()
    if returned_scene and returned_scene != "MATERIAL_ADD_BUDGET":
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


def _verify_control_task_eventually(
    service: Any,
    *,
    retry_delays: tuple[float, ...] = (1, 2, 4, 6, 8),
    sleep: Any = time.sleep,
    **kwargs: Any,
) -> dict[str, Any]:
    """Reconcile a successful create response against an eventually consistent list.

    The control-task create endpoint can return before the list endpoint exposes
    the task's complete material list.  This function performs read-only
    reconciliation only; it never resubmits the POST request.
    """
    last_error: Optional[Exception] = None
    for attempt in range(len(retry_delays) + 1):
        try:
            return _verify_control_task(service, **kwargs)
        except Exception as exc:
            last_error = exc
            if attempt >= len(retry_delays):
                break
            sleep(retry_delays[attempt])
    assert last_error is not None
    raise last_error


def _reconcile_created_control_task(
    service: Any,
    *,
    request_uid: str,
    task_id: str,
    verify_kwargs: Mapping[str, Any],
) -> None:
    """Finish create reconciliation off the user-facing execution path."""
    try:
        verified_task = _verify_control_task_eventually(service, **dict(verify_kwargs))
    except Exception as exc:
        if request_uid:
            OfficialApiAuditStore().mark_reconciled(
                request_uid,
                status="unresolved",
                task_id=task_id,
                response={"verification_error": str(exc)},
            )
        return
    if request_uid:
        OfficialApiAuditStore().mark_reconciled(
            request_uid,
            status="confirmed",
            task_id=task_id,
            response=verified_task,
        )


def _start_control_task_reconciliation(
    service: Any,
    *,
    request_uid: str,
    request_id: str,
    task_id: str,
    task_uid: str,
    idempotency_key: str,
    verify_kwargs: Mapping[str, Any],
) -> None:
    """Persist reconciliation before returning to the caller."""
    if request_uid:
        OfficialApiAuditStore().mark_reconciled(
            request_uid,
            status="pending",
            task_id=task_id,
            response={"message": "调控任务已创建，等待列表同步核对"},
        )
    from services.official_api_reconciliation import (
        enqueue_execution_reconciliation,
        start_official_api_reconciliation_background_thread,
    )

    enqueue_execution_reconciliation(
        task_uid=task_uid,
        action_type="retarget",
        aavid=verify_kwargs.get("aavid"),
        ad_id=verify_kwargs.get("ad_id"),
        control_task_id=task_id,
        request_id=request_id,
        request_uid=request_uid,
        idempotency_key=idempotency_key,
        verify_payload=verify_kwargs,
    )
    start_official_api_reconciliation_background_thread()


def _material_is_writable(item: Mapping[str, Any]) -> bool:
    status = str(item.get("material_status") or "").strip().upper()
    audit = str(item.get("audit_status") or "").strip().upper()
    positive_status = {
        "ENABLE", "ENABLED", "ACTIVE", "DELIVERY", "DELIVERING", "RUNNING",
        "DELIVERY_OK",
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


def _retarget_params(
    retargeting: Mapping[str, Any],
    promotion_scene: str = "product",
) -> tuple[Decimal, Optional[Decimal], dict[str, Any]]:
    method = str(retargeting.get("method") or "volume").lower()
    is_live = str(promotion_scene or "").strip().lower() == "live"
    extra: dict[str, Any] = {}
    if method == "volume":
        volume = retargeting.get("volume") if isinstance(retargeting.get("volume"), Mapping) else {}
        if volume.get("total_budget_yuan") in (None, ""):
            raise ValueError("放量追投必须填写调控预算")
        if volume.get("duration_hours") in (None, ""):
            raise ValueError("放量追投必须填写调控时长")
        if is_live:
            extra["smart_bid_type"] = "SMART_BID_CONSERVATIVE"
        return (
            Decimal(str(volume.get("total_budget_yuan"))),
            Decimal(str(volume.get("duration_hours"))),
            extra,
        )
    if not is_live:
        raise ValueError("推商品计划当前仅支持放量追投")
    control = retargeting.get("cost_control") if isinstance(retargeting.get("cost_control"), Mapping) else {}
    goal = str(control.get("optimization_goal") or "net_roi").lower()
    extra.update(
        {
            "smart_bid_type": "SMART_BID_CUSTOM",
            "external_action": "AD_CONVERT_TYPE_LIVE_SUCCESSORDER_PAY",
        }
    )
    if goal == "net_roi":
        block = control.get("net_roi") if isinstance(control.get("net_roi"), Mapping) else {}
        if block.get("daily_budget_yuan") in (None, ""):
            raise ValueError("控成本追投必须填写调控日预算")
        if block.get("net_roi_target") in (None, ""):
            raise ValueError("控成本追投必须填写综合营销ROI目标")
        extra["deep_external_action"] = "AD_CONVERT_TYPE_LIVE_PURE_PAY_ROI"
        extra["roi2_goal"] = float(Decimal(str(block.get("net_roi_target"))))
        budget = Decimal(str(block.get("daily_budget_yuan")))
    else:
        block = control.get("live_room") if isinstance(control.get("live_room"), Mapping) else {}
        if block.get("daily_budget_yuan") in (None, ""):
            raise ValueError("直播间成交追投必须填写调控日预算")
        if block.get("bid_per_conversion_yuan") in (None, ""):
            raise ValueError("直播间成交追投必须填写转化出价")
        extra["bid"] = float(Decimal(str(block.get("bid_per_conversion_yuan"))))
        budget = Decimal(str(block.get("daily_budget_yuan")))
    # 直播控成本素材追投不支持 duration，平台按长期有效处理。
    return budget, None, extra


def _plan_budget_limit(detail: Mapping[str, Any]) -> Optional[Decimal]:
    raw = detail.get("raw") if isinstance(detail.get("raw"), Mapping) else {}
    plan_row = raw.get("ad_info") if isinstance(raw.get("ad_info"), Mapping) else raw
    for key in (
        "budget",
        "daily_budget",
        "dailyBudget",
        "total_budget",
        "totalBudget",
    ):
        value = plan_row.get(key) if isinstance(plan_row, Mapping) else None
        if value in (None, "", "不限", "UNLIMITED"):
            continue
        try:
            number = Decimal(str(value))
        except Exception:
            continue
        if number.is_finite() and number > 0:
            return number
    return None


def _validate_budget_against_plan(detail: Mapping[str, Any], budget: Decimal) -> None:
    limit = _plan_budget_limit(detail)
    if limit is not None and budget > limit:
        raise ValueError(
            f"追投预算 {budget} 元不能高于主计划当前预算 {limit} 元"
        )


def _public_api_error(exc: BaseException) -> str:
    """Keep the platform message and append identifiers needed for support."""

    message = str(exc) or "千川官方 API 请求失败"
    details: list[str] = []
    code = str(getattr(exc, "code", "") or "").strip()
    request_id = str(getattr(exc, "request_id", "") or "").strip()
    if code:
        details.append(f"错误码 {code}")
    if request_id:
        details.append(f"request_id {request_id}")
    return message + (f"（{'，'.join(details)}）" if details else "")


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
        execution_uid: Optional[str] = None,
        reconciliation_task_uid: Optional[str] = None,
        **_: Any,
    ) -> Any:
        from services.retargeting_service import RetargetingRunResult

        service = get_official_api_service()
        mids = [text_id(value) for value in (material_ids or [material_id]) if text_id(value)]
        rdict = dict(retargeting or {})
        intent_reserved = False
        intent_key = str(execution_uid or "").strip()
        control_task_name = _unique_control_task_name(
            _configured_control_task_base_name(rdict, strategy_title),
            execution_uid,
        )
        existing = _existing_reconciliation(execution_uid)
        if existing:
            existing_status = str(existing.get("status") or "submitted")
            task_id = text_id(existing.get("control_task_id"))
            if existing_status == "confirmed_succeeded":
                return RetargetingRunResult(
                    True,
                    "该追投已由平台核验成功，本次未重复提交",
                    "confirmed_succeeded",
                    "",
                    text_id(aavid),
                    text_id(ad_id),
                    text_id(material_id),
                    task_id,
                    str(rdict.get("method") or "volume"),
                    json.dumps(rdict, ensure_ascii=False, separators=(",", ":")),
                    _now(),
                    True,
                )
            if existing_status in {"submitted", "verifying"}:
                return RetargetingRunResult(
                    True,
                    "该追投已提交，正在核验平台最终状态，本次未重复提交",
                    "submitted_verifying",
                    "",
                    text_id(aavid),
                    text_id(ad_id),
                    text_id(material_id),
                    task_id,
                    str(rdict.get("method") or "volume"),
                    json.dumps(rdict, ensure_ascii=False, separators=(",", ":")),
                    _now(),
                    True,
                )
            return RetargetingRunResult(
                False,
                "该追投已有未确认结果，已禁止自动重复提交，请人工核对",
                existing_status or "unknown_requires_review",
                str(existing.get("last_error") or ""),
                text_id(aavid),
                text_id(ad_id),
                text_id(material_id),
                task_id,
                str(rdict.get("method") or "volume"),
                json.dumps(rdict, ensure_ascii=False, separators=(",", ":")),
                _now(),
                True,
            )
        try:
            plan_detail = await asyncio.to_thread(
                _check_plan,
                service,
                aavid=aavid,
                ad_id=ad_id,
                promotion_scene=promotion_scene,
                plan_system=plan_system,
            )
            budget, duration, extra = _retarget_params(rdict, promotion_scene)
            _validate_budget_against_plan(plan_detail, budget)
            # This is a current-state safety check, not a history backfill.
            # Scanning 30 days of every material can traverse hundreds of
            # irrelevant historical rows and lets a late-page platform error
            # block an otherwise valid task.  The rule candidate was produced
            # from today's fresh DELIVERY_OK snapshot, so re-read that same
            # official scope immediately before POST.
            end_date = datetime.now().strftime("%Y-%m-%d")
            start_date = end_date
            materials, _ = await asyncio.to_thread(
                service.list_plan_materials,
                aavid,
                ad_id,
                start_date=start_date,
                end_date=end_date,
                fields=[],
                delivery_only=True,
            )
            current = {text_id(item.get("material_id")): item for item in materials}
            missing = [mid for mid in mids if mid not in current]
            if missing:
                raise RuntimeError("待追投素材已不属于该计划：" + ",".join(missing))
            for mid in mids:
                if not _material_is_writable(current.get(mid) or {}):
                    raise RuntimeError(f"素材 {mid} 的投放或审核状态未明确可用，已禁止追投")

            if intent_key:
                from services.official_api_reconciliation import reserve_execution_intent

                intent, intent_reserved = reserve_execution_intent(
                    task_uid=str(reconciliation_task_uid or execution_uid or ""),
                    action_type="retarget",
                    aavid=aavid,
                    ad_id=ad_id,
                    idempotency_key=intent_key,
                    verify_payload={
                        "aavid": aavid,
                        "ad_id": ad_id,
                        "promotion_scene": promotion_scene,
                        "material_ids": mids,
                        "task_name": control_task_name,
                        "budget": str(budget),
                        "duration": str(duration) if duration is not None else "",
                        "execution_uid": intent_key,
                    },
                )
                if not intent_reserved:
                    return RetargetingRunResult(
                        False,
                        "该追投已有提交记录，已禁止重复提交，请等待核验或人工检查",
                        str(intent.get("status") or "unknown_requires_review"),
                        str(intent.get("last_error") or ""),
                        text_id(aavid),
                        text_id(ad_id),
                        text_id(material_id),
                        text_id(intent.get("control_task_id")),
                        str(rdict.get("method") or "volume"),
                        json.dumps(rdict, ensure_ascii=False, separators=(",", ":")),
                        _now(),
                        True,
                    )
            response = await asyncio.to_thread(
                service.create_material_control_task,
                aavid,
                ad_id=ad_id,
                marketing_goal=_goal(promotion_scene),
                name=control_task_name,
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
                    task_name=control_task_name,
                    budget=budget,
                    duration=duration,
                    material_ids=mids,
                )
                task_id = text_id((duplicate or {}).get("task_id"))
            if not task_id:
                raise RuntimeError("官方 API 已返回成功但未返回可核验的调控任务 ID")
            # A code=0 create response with a concrete task ID is the write
            # result.  The list endpoint is eventually consistent, so its
            # read-only verification runs in the background and must not hold
            # the card in "executing" for another 20+ seconds.
            _start_control_task_reconciliation(
                service,
                request_uid=str(response.request_uid or ""),
                request_id=str(response.request_id or ""),
                task_id=task_id,
                task_uid=str(reconciliation_task_uid or execution_uid or ""),
                idempotency_key=str(execution_uid or control_task_name),
                verify_kwargs={
                    "aavid": aavid,
                    "ad_id": ad_id,
                    "promotion_scene": promotion_scene,
                    "task_id": task_id,
                    "material_ids": mids,
                    "task_name": control_task_name,
                    "budget": budget,
                    "duration": duration,
                    "execution_uid": str(execution_uid or ""),
                },
            )
            return RetargetingRunResult(
                success=True,
                message="官方 API 已提交追投，正在核验平台最终状态",
                step="submitted_verifying",
                detail=json.dumps(
                    {
                        "source": "qianchuan_open_api",
                        "request_id": response.request_id,
                        "reconciliation_status": "pending",
                    },
                    ensure_ascii=False,
                ),
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
                budget, duration, _ = _retarget_params(rdict, promotion_scene)
                duplicate = await asyncio.to_thread(
                    service.find_duplicate_control_task,
                    aavid,
                    ad_id=ad_id,
                    marketing_goal=_goal(promotion_scene),
                    task_name=control_task_name,
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
            if intent_reserved and intent_key:
                _start_control_task_reconciliation(
                    service,
                    request_uid=str(exc.request_uid or ""),
                    request_id=str(exc.request_id or ""),
                    task_id=task_id,
                    task_uid=str(reconciliation_task_uid or execution_uid or ""),
                    idempotency_key=intent_key,
                    verify_kwargs={
                        "aavid": aavid,
                        "ad_id": ad_id,
                        "promotion_scene": promotion_scene,
                        "task_id": task_id,
                        "material_ids": mids,
                        "task_name": control_task_name,
                        "budget": budget,
                        "duration": duration,
                        "execution_uid": intent_key,
                    },
                )
            pending_verification = bool(task_id or (intent_reserved and intent_key))
            return RetargetingRunResult(
                success=pending_verification,
                message=(
                    "官方 API 已找到对应调控任务，正在继续核验"
                    if task_id
                    else "千川内部处理超时，已禁止重复提交，正在查询是否已创建"
                ),
                step=(
                    "submitted_verifying"
                    if pending_verification
                    else "unknown_requires_review"
                ),
                detail=_public_api_error(exc),
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
            retryable = isinstance(exc, ApiRateLimitError)
            retry_after_seconds = (
                max(60, int(getattr(exc, "retry_after", 0) or 0))
                if retryable
                else 0
            )
            if intent_reserved and intent_key:
                from services.official_api_reconciliation import finish_execution_intent

                finish_execution_intent(
                    intent_key,
                    status="confirmed_failed",
                    error=_public_api_error(exc),
                )
            return RetargetingRunResult(
                success=False,
                message=_public_api_error(exc),
                step="official_api",
                detail=traceback.format_exc()[:8000],
                aavid=text_id(aavid),
                ad_id=text_id(ad_id),
                material_id=text_id(material_id),
                retargeting_method=str(rdict.get("method") or "volume"),
                retargeting_json=json.dumps(rdict, ensure_ascii=False, separators=(",", ":")),
                finished_at=_now(),
                headless=True,
                retryable=retryable,
                retry_after_seconds=retry_after_seconds,
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
        execution_uid: Optional[str] = None,
        reconciliation_task_uid: Optional[str] = None,
        control_cycle_key: str = "",
        **_: Any,
    ) -> Any:
        from services.regulation_service import RegulationRunResult

        service = get_official_api_service()
        action = str(stop_action or "").lower()
        opt_type = "PAUSE" if action == "pause" else "DISABLE"
        intent_key = str(execution_uid or "").strip() or hashlib.sha256(
            f"stop|{text_id(aavid)}|{text_id(ad_id)}|{text_id(assist_task_id)}|{opt_type}".encode("utf-8")
        ).hexdigest()
        intent_reserved = False
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
            exact_scene = str(exact.get("scene") or "").upper()
            if exact_scene and exact_scene != "MATERIAL_ADD_BUDGET":
                raise RuntimeError("目标不是素材追投调控任务，已禁止操作")
            if not exact_scene:
                try:
                    has_fresh_material_evidence = _cached_material_retarget_evidence(
                        aavid=aavid,
                        ad_id=ad_id,
                        task_id=assist_task_id,
                    )
                except _MaterialRetargetEvidenceCheckError as exc:
                    raise RuntimeError(
                        "调控任务场景证据校验异常，未向千川提交停投"
                    ) from exc
                if not has_fresh_material_evidence:
                    raise RuntimeError("调控任务缺少场景证据，且本地没有10分钟内的素材追投记录，已禁止操作")
            current_status = str(exact.get("status") or "").upper()
            if current_status in {"DISABLE", "DISABLED", "FINISHED", "ENDED"}:
                message = "调控任务已经暂停" if opt_type == "PAUSE" else "调控任务已经结束"
                return RegulationRunResult(True, message, "done_already_paused", "", text_id(aavid), text_id(ad_id), text_id(assist_task_id), action, _now(), True)
            if current_status == "OFFLINE_TIME":
                return RegulationRunResult(True, "调控任务已自然到期", "done_naturally_expired", "", text_id(aavid), text_id(ad_id), text_id(assist_task_id), action, _now(), True)
            from services.official_api_reconciliation import reserve_execution_intent

            intent, intent_reserved = reserve_execution_intent(
                task_uid=str(reconciliation_task_uid or execution_uid or intent_key),
                action_type="stop",
                aavid=aavid,
                ad_id=ad_id,
                control_task_id=assist_task_id,
                idempotency_key=intent_key,
                verify_payload={
                    "aavid": aavid,
                    "ad_id": ad_id,
                    "promotion_scene": promotion_scene,
                    "task_id": text_id(assist_task_id),
                    "expected_status": opt_type,
                    "execution_uid": intent_key,
                    "control_cycle_key": str(control_cycle_key or ""),
                },
            )
            if not intent_reserved:
                existing_status = str(intent.get("status") or "unknown_requires_review")
                return RegulationRunResult(
                    existing_status == "confirmed_succeeded",
                    (
                        "该停投已由平台核验成功"
                        if existing_status == "confirmed_succeeded"
                        else "该停投已有提交记录，已禁止重复提交，请等待核验或人工检查"
                    ),
                    existing_status,
                    json.dumps(
                        {
                            "reconciliation_uid": str(
                                intent.get("reconciliation_uid") or ""
                            ),
                            "idempotency_key": str(
                                intent.get("idempotency_key") or intent_key
                            ),
                            "status": existing_status,
                            "control_cycle_key": str(control_cycle_key or ""),
                            "error": str(intent.get("last_error") or ""),
                        },
                        ensure_ascii=False,
                    ),
                    text_id(aavid),
                    text_id(ad_id),
                    text_id(assist_task_id),
                    action,
                    _now(),
                    True,
                )
            # A rate-limit response is an explicit rejection before the write
            # is accepted, so retrying is safe. Other parameter, permission or
            # state errors are deterministic and must never be retried.
            response = None
            for attempt in range(1, 4):
                try:
                    response = await asyncio.to_thread(
                        service.update_control_status,
                        aavid,
                        [assist_task_id],
                        action=opt_type,
                    )
                    break
                except ApiRateLimitError as exc:
                    if attempt >= 3:
                        raise
                    retry_after = max(
                        1.0,
                        float(getattr(exc, "retry_after", 0) or 60),
                    )
                    await asyncio.sleep(retry_after)
            if response is None:
                raise RuntimeError("官方 API 停投提交未返回结果")
            if response.request_uid:
                OfficialApiAuditStore().mark_reconciled(
                    response.request_uid,
                    status="pending",
                    task_id=assist_task_id,
                    response={"message": "停投已提交，等待平台状态核验"},
                )
            from services.official_api_reconciliation import (
                enqueue_execution_reconciliation,
                start_official_api_reconciliation_background_thread,
            )

            enqueue_execution_reconciliation(
                task_uid=str(reconciliation_task_uid or execution_uid or intent_key),
                action_type="stop",
                aavid=aavid,
                ad_id=ad_id,
                control_task_id=assist_task_id,
                request_id=str(response.request_id or ""),
                request_uid=str(response.request_uid or ""),
                idempotency_key=intent_key,
                verify_payload={
                    "aavid": aavid,
                    "ad_id": ad_id,
                    "promotion_scene": promotion_scene,
                    "task_id": text_id(assist_task_id),
                    "expected_status": opt_type,
                    "execution_uid": intent_key,
                    "control_cycle_key": str(control_cycle_key or ""),
                },
            )
            start_official_api_reconciliation_background_thread()
            return RegulationRunResult(
                True,
                "官方 API 停投已提交，正在核验平台最终状态",
                "submitted_verifying",
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
            stop_outcome, stop_message = classify_stop_status(opt_type, status)
            reconciled = stop_outcome in {"confirmed", "terminal_natural"}
            if exc.request_uid:
                OfficialApiAuditStore().mark_reconciled(
                    exc.request_uid,
                    status="confirmed" if reconciled else "unresolved",
                    task_id=assist_task_id,
                    response=exact or {},
                )
            if intent_reserved:
                if reconciled:
                    from services.official_api_reconciliation import (
                        enqueue_execution_reconciliation,
                        start_official_api_reconciliation_background_thread,
                    )

                    enqueue_execution_reconciliation(
                        task_uid=str(reconciliation_task_uid or execution_uid or intent_key),
                        action_type="stop",
                        aavid=aavid,
                        ad_id=ad_id,
                        control_task_id=assist_task_id,
                        request_id="",
                        request_uid=str(exc.request_uid or ""),
                        idempotency_key=intent_key,
                        verify_payload={
                            "aavid": aavid,
                            "ad_id": ad_id,
                            "promotion_scene": promotion_scene,
                            "task_id": text_id(assist_task_id),
                            "expected_status": opt_type,
                            "execution_uid": intent_key,
                            "control_cycle_key": str(control_cycle_key or ""),
                        },
                    )
                    start_official_api_reconciliation_background_thread()
                else:
                    from services.official_api_reconciliation import finish_execution_intent

                    finish_execution_intent(intent_key, status="unknown_requires_review", error=str(exc))
            return RegulationRunResult(
                reconciled,
                stop_message if reconciled else "官方 API 停投结果未知，已禁止自动重试",
                "submitted_verifying" if reconciled else "unknown_requires_review",
                str(exc),
                text_id(aavid),
                text_id(ad_id),
                text_id(assist_task_id),
                action,
                _now(),
                True,
            )
        except Exception as exc:
            if intent_reserved:
                from services.official_api_reconciliation import finish_execution_intent

                finish_execution_intent(
                    intent_key,
                    status="confirmed_failed",
                    error=_public_api_error(exc),
                )
            return RegulationRunResult(
                False,
                _public_api_error(exc),
                "official_api",
                traceback.format_exc()[:8000],
                text_id(aavid),
                text_id(ad_id),
                text_id(assist_task_id),
                action,
                _now(),
                True,
            )
