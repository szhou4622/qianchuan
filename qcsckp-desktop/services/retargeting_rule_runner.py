# -*- coding: utf-8 -*-
"""
规则化追投调度：按固定间隔拉取大屏同期素材，按每条「追投策略」的 trigger 分别筛选后执行 Playwright 追投，并写入 pmc_retargeting_run。
多策略之间 asyncio 并行，默认最多 5 路（环境变量 RETARGET_STRATEGY_PARALLEL）。
根级 interval 对应全局限频表 pmc_retargeting_rate_limit（每素材一行）。
未开启分策略限频：仅按该表做「是否跳过」判断，成功后只更新该表。
开启分策略限频：「是否跳过」只按各策略的 pmc_retargeting_rate_limit_strategy 判断；成功后策略表 +1，且对全局表按根级 interval 调用 rate_limit_record_success（窗口未过期则 use_count+1，过期则重置 limit_started_at 并记为 1）。
触发限频时仅跳过，不写流水。
同一素材在多策略并行时，用「每素材一把 asyncio 锁」串行化：检查限频 → 执行追投 → 成功后记次，避免两策略同时通过检查导致超次数。
同一策略本轮内多条命中素材：可按策略选择逐条分别追投，或最多20条合并成一个追投任务。

运行（项目根目录）:
    python -m services.retargeting_rule_runner
GUI：由 gui_app 调用 start_retargeting_rule_runner_background_thread() 启动同逻辑后台线程。
可选环境变量 / 常量见下方 DEFAULT_INTERVAL_SEC。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import queue
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

RATE_LIMIT_TABLE = "pmc_retargeting_rate_limit"
RATE_LIMIT_STRATEGY_TABLE = "pmc_retargeting_rate_limit_strategy"

from api.dashboard import DashboardApi
from api.rule_retargeting_config import (
    build_trigger_evaluation_snapshot,
    evaluate_trigger,
    load_rule_retargeting_config,
    target_report_metric_units,
    unsupported_strategy_monitor_metrics,
)
from config import CURRENT_VERSION, TEST_MODE
from services.cloud_retarget_client import create_retarget_task
from services.local_test_guard import row_is_in_test_scope
from services.product_rule_engine import evaluate_product_strategy, material_net_roi_sort_key
from services.plan_system import normalize_plan_system
from services.promotion_capability import check_target_capability
from services.promotion_browser_lock import exclusive_browser_operation
from services.qianchuan_accounts import schedulable_promotion_targets
from services.qianchuan_session import automation_session_ready, current_session_owner
from services.retarget_budget_increase import (
    assist_task_sync_ready,
    budget_increase_fingerprint,
    calculate_budget_increase,
)
from services.retargeting_service import (
    QianChuanRetargetingService,
    retarget_capability_matches,
    retarget_log_tag,
    retargeting_block_from_full_config,
)
from utils.log import logger
from utils.sqlite_store import SQLiteStore, init_sqlite_schema
from services.rule_wakeup import TargetWakeBatch


DEFAULT_INTERVAL_SEC = 300
MAX_REVALIDATION_AGE_SECONDS = 10 * 60

# 多策略并行上限（同一轮内各策略各起一个浏览器任务）
MAX_STRATEGY_PARALLEL = 5

_DASHBOARD_PAGE_SIZE = 20_000

# 多策略并行时，同一 material_id 的限频检查与记次必须互斥，避免竞态超次数
_material_retouch_locks: Dict[str, asyncio.Lock] = {}
_material_retouch_locks_guard = asyncio.Lock()

# 规则保存、监控计划首次采集完成后，不应让用户再等待完整的 5 分钟周期。
# Queue 用于合并连续唤醒，同时避免 Event 在 wait/clear 交界处丢失通知。
_RUNNER_WAKE_QUEUE: "queue.Queue[str]" = queue.Queue(maxsize=1)
_TARGET_WAKE = TargetWakeBatch()
_RUNNER_THREAD_LOCK = threading.Lock()
_RUNNER_THREAD: Optional[threading.Thread] = None
_RUNNER_STOP = threading.Event()

# 官方 API 采集开始/排队时会暂时更新 last_status，但不会清空上一轮已
# 完成的 last_sync_at。只要该快照仍在 10 分钟新鲜期内，就可以进入规则
# 复核；真正提交前仍会读取最新指标并再次校验计划和素材。
_TRANSIENT_COLLECTION_STATUSES = frozenset({"collecting", "queued"})


def _auto_execution_uid(
    *,
    target_uid: str,
    strategy_id: str,
    material_ids: List[str],
    now: Optional[float] = None,
) -> str:
    """Stable identity for one 5-minute automatic evaluation window.

    It prevents a scheduler wake-up, process recovery, or concurrent strategy
    path from submitting the same candidate twice before rate-limit state is
    committed.  A later 5-minute window remains eligible subject to the user's
    configured frequency guard.
    """
    bucket = int((time.time() if now is None else float(now)) // DEFAULT_INTERVAL_SEC)
    owner = str(current_session_owner() or "").strip().casefold()
    source = "|".join(
        [
            owner,
            str(target_uid or ""),
            str(strategy_id or ""),
            ",".join(sorted({str(mid) for mid in material_ids if str(mid)})),
            str(bucket),
        ]
    )
    return "auto-" + hashlib.sha256(source.encode("utf-8")).hexdigest()[:32]


def auto_execute_allowed_in_current_environment() -> bool:
    """正式环境保留自动追投；本地测试环境只允许走飞书确认任务。"""
    return not TEST_MODE


def retarget_method_is_supported_for_scene(
    promotion_scene: str,
    retargeting: Dict[str, Any],
) -> bool:
    """商品表单当前仅支持放量；直播保留放量和控成本两种方式。"""
    scene = str(promotion_scene or "live").strip().lower()
    method = str((retargeting or {}).get("method") or "volume").strip().lower()
    if scene == "product":
        return method == "volume"
    return method in ("volume", "cost_control")


async def _lock_for_material_retouch(
    material_id: str,
    target_uid: Optional[str] = None,
) -> asyncio.Lock:
    mid = str(material_id).strip()
    if not mid:
        mid = "__empty_material__"
    lock_key = f"{_rate_target_uid(target_uid)}:{mid}"
    async with _material_retouch_locks_guard:
        if lock_key not in _material_retouch_locks:
            _material_retouch_locks[lock_key] = asyncio.Lock()
        return _material_retouch_locks[lock_key]


async def _locks_for_material_retouch(
    material_ids: List[str],
    target_uid: Optional[str] = None,
) -> List[asyncio.Lock]:
    """按稳定顺序取得一组素材锁，避免合并任务与单素材任务并发穿透限频。"""
    locks: List[asyncio.Lock] = []
    for material_id in sorted({str(item or "").strip() for item in material_ids if str(item or "").strip()}):
        locks.append(await _lock_for_material_retouch(material_id, target_uid))
    return locks


class _MaterialLockGroup:
    def __init__(self, locks: List[asyncio.Lock]) -> None:
        self._locks = locks

    async def __aenter__(self) -> "_MaterialLockGroup":
        for lock in self._locks:
            await lock.acquire()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        for lock in reversed(self._locks):
            lock.release()


def _beijing_now_str() -> str:
    return (datetime.utcnow() + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")


def _timestamp_is_fresh(
    value: Any,
    *,
    max_age_seconds: int = MAX_REVALIDATION_AGE_SECONDS,
) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    try:
        observed_at = datetime.strptime(
            text.replace("T", " ")[:19],
            "%Y-%m-%d %H:%M:%S",
        )
    except (TypeError, ValueError):
        return False
    age_seconds = (datetime.now() - observed_at).total_seconds()
    return -300 <= age_seconds <= max(1, int(max_age_seconds))


def target_has_usable_collection_snapshot(target: Dict[str, Any]) -> bool:
    """判断目标是否有可用于本轮规则复核的已完成采集快照。"""
    status = str(target.get("last_status") or "").strip().lower()
    if status == "ok":
        return True
    if status in _TRANSIENT_COLLECTION_STATUSES:
        return _timestamp_is_fresh(target.get("last_sync_at"))
    return False


def request_retargeting_rule_evaluation(reason: str = "", *, target_uids=None) -> bool:
    """请求规则线程立即执行一轮；连续请求合并为一次。"""
    if not _TARGET_WAKE.request(target_uids):
        return False
    try:
        _RUNNER_WAKE_QUEUE.put_nowait(str(reason or "manual"))
        return True
    except queue.Full:
        return False


def _wait_for_next_rule_cycle(timeout_seconds: int) -> bool:
    """阻塞等待周期或即时唤醒；返回 True 表示收到唤醒。"""
    try:
        _RUNNER_WAKE_QUEUE.get(timeout=max(0.01, float(timeout_seconds)))
        return True
    except queue.Empty:
        return False


def ensure_official_api_auto_execution_runtime(
    cfg: Dict[str, Any],
) -> bool:
    """从已保存的自动策略恢复官方 API 写权限，避免重启后只采集不执行。"""
    import config as runtime_config

    if runtime_config.QIANCHUAN_BACKEND != "official_api":
        return False
    if TEST_MODE:
        return False
    if not bool(cfg.get("enabled")):
        return False
    has_auto_strategy = any(
        isinstance(item, dict)
        and str(item.get("action_mode") or "card_confirm").strip().lower()
        == "auto_execute"
        for item in (cfg.get("strategies") or [])
    )
    if not has_auto_strategy:
        return False

    from services.qianchuan_open_api.runtime import apply_live_write_permission
    from services.qianchuan_open_api.runtime_settings import (
        enable_execution_for_saved_rules,
        load_runtime_settings,
    )

    settings = load_runtime_settings()
    if not bool(settings.get("allow_live_api_writes")):
        enable_execution_for_saved_rules(cfg)
    else:
        # 单例可能早于运行设置加载而创建；启动时把持久选择重新应用一次。
        runtime_config.ALLOW_LIVE_OFFICIAL_API_WRITES = True
        apply_live_write_permission(True)
    return True


def _json_dumps(obj: Any) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return "{}"


def block_target_after_rate_record_failure(
    db: SQLiteStore,
    target_uid: str,
    exc: BaseException,
) -> None:
    """千川已写成功但限频落库失败时封锁目标，避免下一轮重复执行。"""
    try:
        message = (
            "追投成功但限频记录失败，已暂停自动写操作：" + str(exc)
        )[:1000]
        db.execute(
            "UPDATE promotion_target SET last_status='error',last_error=?,"
            "automation_write_blocked=1,write_block_reason=?,"
            "write_block_origin='rate_record_failure',"
            "write_blocked_at=datetime('now','+8 hours'),"
            "updated_at=datetime('now','+8 hours') WHERE target_uid=?",
            (
                message,
                (
                    "追投已成功，但限频记录失败；必须核对千川任务和限频流水后人工解除："
                    + str(exc)
                )[:2000],
                str(target_uid or ""),
            ),
        )
    except Exception:
        logger.exception(
            "限频记录失败后封锁监控目标也失败 target=%s",
            target_uid,
        )


def record_retarget_rate_success_safely(
    db: SQLiteStore,
    *,
    material_ids: List[str],
    target_uid: str,
    cfg: Dict[str, Any],
    strategy: Dict[str, Any],
    retargeting: Dict[str, Any],
) -> None:
    """在浏览器写锁内记录成功次数；失败则封锁目标但不伪报千川写失败。"""
    try:
        for material_id in material_ids:
            if bool(cfg.get("per_strategy_rate_limit")):
                window_seconds, max_count = _interval_window_and_max(retargeting)
                rate_limit_strategy_record_success(
                    db,
                    material_id,
                    str(strategy.get("id") or "__legacy__"),
                    window_seconds,
                    max_count,
                    target_uid,
                )
            root_window, root_max = _interval_from_root_cfg(cfg)
            rate_limit_record_success(
                db,
                material_id,
                root_window,
                root_max,
                target_uid,
            )
    except Exception as exc:
        logger.exception(
            "追投成功后记录限频失败，已暂停目标 target=%s materials=%s",
            target_uid,
            ",".join(material_ids),
        )
        block_target_after_rate_record_failure(db, target_uid, exc)


def _strategy_snapshot(strategy: Dict[str, Any]) -> Dict[str, Any]:
    """生成稳定的策略快照，供排队前后比较，防止等待浏览器期间策略被修改。"""
    return {
        "id": str(strategy.get("id") or ""),
        "title": str(strategy.get("title") or strategy.get("id") or "?")[:64],
        "account_uid": str(strategy.get("account_uid") or ""),
        "target_uid": str(strategy.get("target_uid") or ""),
        "trigger_level": str(strategy.get("trigger_level") or "material"),
        "product_filter": (
            strategy.get("product_filter")
            if isinstance(strategy.get("product_filter"), list)
            else []
        ),
        "candidate_trigger": (
            strategy.get("candidate_trigger")
            if isinstance(strategy.get("candidate_trigger"), dict)
            else {}
        ),
        "candidate_sort": str(strategy.get("candidate_sort") or "net_roi_desc"),
        "candidate_limit": int(strategy.get("candidate_limit") or 1),
        "material_grouping_mode": (
            "merged"
            if str(strategy.get("material_grouping_mode") or "separate").strip().lower()
            == "merged"
            else "separate"
        ),
        "action_mode": str(strategy.get("action_mode") or "card_confirm"),
        "task_action": str(strategy.get("task_action") or "create_retarget"),
        "trigger": (
            strategy.get("trigger")
            if isinstance(strategy.get("trigger"), dict)
            else {}
        ),
        "retargeting": (
            strategy.get("retargeting")
            if isinstance(strategy.get("retargeting"), dict)
            else {}
        ),
    }


def _strategy_fingerprint(strategy: Dict[str, Any]) -> str:
    raw = json.dumps(
        _strategy_snapshot(strategy),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _find_strategy(cfg: Dict[str, Any], strategy_id: str) -> Optional[Dict[str, Any]]:
    for strategy in cfg.get("strategies") or []:
        if (
            isinstance(strategy, dict)
            and str(strategy.get("id") or "") == strategy_id
        ):
            return strategy
    return None


def _revalidate_auto_retarget_under_lock(
    db: SQLiteStore,
    *,
    original_strategy: Dict[str, Any],
    target_uid: str,
    aavid: str,
    ad_id: str,
    promotion_scene: str,
    plan_system: str,
    material_id: str,
    product_id: str,
) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    """在取得全局浏览器写锁后重新验证所有会影响自动追投的可变状态。"""
    owner = current_session_owner()
    if not owner:
        raise RuntimeError("当前工具账号未登录，已阻止自动追投")
    session_gate = automation_session_ready(owner)
    if not session_gate.get("ready"):
        raise RuntimeError(
            str(session_gate.get("message") or "千川登录状态不存在或已失效")
        )

    current_cfg = load_rule_retargeting_config()
    if not current_cfg.get("enabled"):
        raise RuntimeError("规则化追投已关闭")
    strategy_id = str(original_strategy.get("id") or "")
    current_strategy = _find_strategy(current_cfg, strategy_id)
    if not current_strategy:
        raise RuntimeError("追投策略已删除")
    if str(current_strategy.get("action_mode") or "card_confirm") != "auto_execute":
        raise RuntimeError("追投策略执行方式已经变更")
    if _strategy_fingerprint(current_strategy) != _strategy_fingerprint(
        original_strategy
    ):
        raise RuntimeError("追投策略参数已经变更")

    target = next(
        (
            row
            for row in schedulable_promotion_targets(
                owner_username=owner,
                db=db,
            )
            if str(row.get("target_uid") or "") == target_uid
        ),
        None,
    )
    if not target:
        raise RuntimeError("监控计划已停用或正在等待监控容量")
    if not bool(target.get("retarget_eligible")):
        raise RuntimeError(
            str(target.get("ineligible_reason") or "监控计划尚未取得可追投资格")
        )
    current_scene = str(target.get("promotion_scene") or "live").strip().lower()
    current_system = normalize_plan_system(target.get("plan_system") or "unknown")
    if (
        str(target.get("aadvid") or "") != str(aavid)
        or str(target.get("ad_id") or "") != str(ad_id)
        or current_scene != str(promotion_scene)
        or current_system != normalize_plan_system(plan_system)
    ):
        raise RuntimeError("当前账户、计划、推广场景或计划体系已经变化")
    if not target_has_usable_collection_snapshot(target):
        raise RuntimeError("监控计划当前状态异常")
    if not _timestamp_is_fresh(target.get("last_sync_at")):
        raise RuntimeError(
            "监控计划最近一次采集已超过10分钟或时间无效，"
            "必须等待新一轮实时数据"
        )
    if bool(target.get("automation_write_blocked")):
        raise RuntimeError(
            "该计划已触发自动写入安全封锁："
            + str(target.get("write_block_reason") or "请人工核对后解除")
        )
    if current_system == "unknown":
        raise RuntimeError("计划体系尚未确认")
    capability_ok, capability_reason = check_target_capability(
        target,
        action="retarget",
        promotion_scene=current_scene,
        plan_system=current_system,
    )
    if not capability_ok:
        raise RuntimeError(f"当前计划追投能力无效：{capability_reason}")
    retargeting = (
        current_strategy.get("retargeting")
        if isinstance(current_strategy.get("retargeting"), dict)
        else {}
    )
    if not retarget_method_is_supported_for_scene(current_scene, retargeting):
        raise RuntimeError("当前追投方式不适用于该推广场景")

    period = str(current_cfg.get("trigger_query_period") or "1h")
    response = DashboardApi().get_table_data(
        period=period,
        sort_by="costDiff",
        sort_order="desc",
        page=1,
        page_size=_DASHBOARD_PAGE_SIZE,
        target_uid=target_uid,
    )
    if not response.get("success"):
        raise RuntimeError(response.get("message") or "读取素材最新数据失败")
    target_rows = [
        row
        for row in (response.get("data") or [])
        if isinstance(row, dict)
        and str(row.get("targetUid") or target_uid) == target_uid
    ]
    raw_material_row = next(
        (
            row
            for row in target_rows
            if str(row.get("id") or "") == str(material_id)
            and str(row.get("aadvid") or aavid) == str(aavid)
        ),
        None,
    )
    if not raw_material_row:
        raise RuntimeError("素材已不在当前计划的最新数据中")
    if not _timestamp_is_fresh(
        raw_material_row.get("periodEndTime")
        or raw_material_row.get("period_end_time")
        or raw_material_row.get("createdAt")
        or raw_material_row.get("created_at")
    ):
        raise RuntimeError("素材实时数据已超过10分钟或时间无效")
    target_rows = [
        row
        for row in target_rows
        if _timestamp_is_fresh(
            row.get("periodEndTime")
            or row.get("period_end_time")
            or row.get("createdAt")
            or row.get("created_at")
        )
    ]
    material_row = raw_material_row

    trigger_level = str(
        current_strategy.get("trigger_level") or "material"
    ).strip().lower()
    if trigger_level == "product":
        if current_scene != "product":
            raise RuntimeError("商品级规则不能用于推直播计划")
        if not product_id:
            raise RuntimeError("商品级自动追投缺少商品归属")
        relation_rows = db.select(
            "promotion_material_product",
            fields="material_id, product_id",
            where={"target_uid": target_uid},
        )
        relation_map: Dict[str, List[str]] = {}
        for relation in relation_rows:
            relation_map.setdefault(
                str(relation.get("material_id") or ""),
                [],
            ).append(str(relation.get("product_id") or ""))
        product_rows = db.select(
            "promotion_product",
            fields="product_id, product_name",
            where={"target_uid": target_uid},
        )
        product_names = {
            str(item.get("product_id") or ""): str(item.get("product_name") or "")
            for item in product_rows
        }
        hits = evaluate_product_strategy(
            target_rows,
            current_strategy,
            relation_map=relation_map,
            product_names=product_names,
            allowed_product_ids=[product_id],
        )
        candidates = {
            str(candidate.get("id") or "")
            for hit in hits
            if str(hit.get("productId") or "") == product_id
            for candidate in (hit.get("candidates") or [])
        }
        if material_id not in candidates:
            raise RuntimeError("商品汇总或候选素材条件已不再命中")
    elif not evaluate_trigger(current_strategy.get("trigger") or {}, material_row):
        raise RuntimeError("素材最新数据已不再命中追投规则")

    if bool(current_cfg.get("per_strategy_rate_limit")):
        window_seconds, max_count = _interval_window_and_max(retargeting)
        if rate_limit_strategy_should_skip(
            db,
            material_id,
            str(current_strategy.get("id") or "__legacy__"),
            window_seconds,
            max_count,
            target_uid,
        ):
            raise RuntimeError("素材已达到本策略追投次数上限")
    else:
        window_seconds, max_count = _interval_from_root_cfg(current_cfg)
        if rate_limit_should_skip(
            db,
            material_id,
            window_seconds,
            max_count,
            target_uid,
        ):
            raise RuntimeError("素材已达到全局追投次数上限")
    return current_cfg, current_strategy, target


def _interval_from_root_cfg(cfg: Dict[str, Any]) -> Tuple[int, int]:
    """全策略共用限频：优先根级 interval，与 rule_retargeting.json 一致。"""
    inv = cfg.get("interval")
    if isinstance(inv, dict):
        return _interval_window_and_max({"interval": inv})
    strats = cfg.get("strategies")
    if isinstance(strats, list) and strats:
        r0 = strats[0].get("retargeting")
        if isinstance(r0, dict):
            return _interval_window_and_max(r0)
    rb = retargeting_block_from_full_config(cfg)
    return _interval_window_and_max(rb)


def _interval_window_and_max(retargeting: Dict[str, Any]) -> Tuple[int, int]:
    inv = retargeting.get("interval") or {}
    if not isinstance(inv, dict):
        return 86400, 1
    try:
        ws = int(float(inv.get("window_seconds", 86400)))
    except (TypeError, ValueError):
        ws = 86400
    try:
        mc = int(inv.get("max_count", 1))
    except (TypeError, ValueError):
        mc = 1
    return max(0, ws), max(0, mc)


def _parse_beijing_dt(s: str) -> Optional[datetime]:
    try:
        return datetime.strptime(str(s).strip(), "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def resolve_ad_id_for_aavid(db: SQLiteStore, aavid: str) -> Optional[str]:
    rows = db.select(
        table="pmc_ad_detail_basic",
        fields="ad_id",
        where="aadvid = ?",
        params=(str(aavid).strip(),),
        limit=1,
    )
    if not rows:
        return None
    v = rows[0].get("ad_id")
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None


def resolve_target(
    db: SQLiteStore,
    target_uid: str,
) -> Optional[Dict[str, Any]]:
    uid = str(target_uid or "").strip()
    if not uid:
        return None
    return db.select_one("promotion_target", where={"target_uid": uid})


def _rate_target_uid(target_uid: Optional[str]) -> str:
    return str(target_uid or "legacy_unscoped").strip() or "legacy_unscoped"


def _optimization_goal_str(retargeting: Dict[str, Any]) -> Optional[str]:
    m = str(retargeting.get("method") or "").strip().lower()
    if m != "cost_control":
        return None
    cc = retargeting.get("cost_control") or {}
    if not isinstance(cc, dict):
        return None
    og = str(cc.get("optimization_goal") or "net_roi").strip().lower()
    return og or None


def _material_name_from_dashboard_row(row: Dict[str, Any]) -> str:
    """大屏 get_table_data 行：title 来自 video_name；兼容原始键名。"""
    if not isinstance(row, dict):
        return ""
    for k in ("title", "video_name", "videoName"):
        v = row.get(k)
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s[:512]
    return ""


def _account_name_for_target(
    db: SQLiteStore,
    target: Dict[str, Any],
    fallback: str = "",
) -> str:
    aavid = str(target.get("aadvid") or "").strip()
    ad_id = str(target.get("ad_id") or "").strip()
    account_uid = str(target.get("account_uid") or "").strip()
    # The account directory is the authoritative source in official-API mode.
    # pmc_ad_detail_basic is material-detail data and may legitimately be empty
    # for a newly enabled plan, which previously produced “未命名账户” cards.
    account = None
    if account_uid:
        account = db.select_one(
            "qianchuan_account",
            fields="account_name",
            where={"account_uid": account_uid},
            order_by="updated_at DESC",
        )
    if not account and aavid:
        account = db.select_one(
            "qianchuan_account",
            fields="account_name",
            where={"aavid": aavid},
            order_by="updated_at DESC",
        )
    name = str((account or {}).get("account_name") or "").strip()
    if name:
        return name[:200]
    if aavid:
        account = db.select_one(
            "pmc_ad_detail_basic",
            fields="user_info_name",
            where=(
                {"aadvid": aavid, "ad_id": ad_id}
                if ad_id
                else {"aadvid": aavid}
            ),
            order_by="created_at DESC",
        )
        name = str((account or {}).get("user_info_name") or "").strip()
        if name:
            return name[:200]
    return str(fallback or "").strip()[:200]


def rate_limit_should_skip(
    db: SQLiteStore,
    material_id: str,
    window_seconds: int,
    max_count: int,
    target_uid: Optional[str] = None,
) -> bool:
    """
    只读判断：当前窗口内「已成功次数」是否已达上限；达上限则跳过本次追投。
    不写入限频表；计数仅在追投成功后由 rate_limit_record_success 更新。

    window_seconds / max_count 任一为 0 或负则视为不限频。
    """
    if window_seconds <= 0 or max_count <= 0:
        return False
    if not str(material_id).strip():
        return False

    now_str = _beijing_now_str()
    now_dt = _parse_beijing_dt(now_str)
    if now_dt is None:
        return False

    rows = db.select(
        table=RATE_LIMIT_TABLE,
        where="target_uid = ? AND material_id = ?",
        params=(_rate_target_uid(target_uid), material_id),
        limit=1,
    )

    if not rows:
        return False

    row = rows[0]
    start_s = row.get("limit_started_at") or ""
    start_dt = _parse_beijing_dt(str(start_s))
    try:
        use_count = int(row.get("use_count") or 0)
    except (TypeError, ValueError):
        use_count = 0

    if start_dt is None:
        return False

    window_end = start_dt + timedelta(seconds=window_seconds)
    if window_end > now_dt:
        return use_count >= max_count
    return False


def rate_limit_remaining_capacity(
    db: SQLiteStore,
    material_id: str,
    window_seconds: int,
    max_count: int,
    target_uid: Optional[str] = None,
) -> Optional[int]:
    """返回当前窗口剩余次数；None 表示不限频。"""
    if window_seconds <= 0 or max_count <= 0:
        return None
    mid = str(material_id).strip()
    if not mid:
        return 0
    rows = db.select(
        table=RATE_LIMIT_TABLE,
        where="target_uid = ? AND material_id = ?",
        params=(_rate_target_uid(target_uid), mid),
        limit=1,
    )
    if not rows:
        return max_count
    row = rows[0]
    start_dt = _parse_beijing_dt(str(row.get("limit_started_at") or ""))
    now_dt = _parse_beijing_dt(_beijing_now_str())
    if start_dt is None or now_dt is None:
        return max_count
    if start_dt + timedelta(seconds=window_seconds) <= now_dt:
        return max_count
    try:
        use_count = int(row.get("use_count") or 0)
    except (TypeError, ValueError):
        use_count = 0
    return max(0, max_count - use_count)


def rate_limit_record_success(
    db: SQLiteStore,
    material_id: str,
    window_seconds: int,
    max_count: int,
    target_uid: Optional[str] = None,
) -> None:
    """
    追投成功（Playwright 返回 success）后调用：按窗口累加成功次数，或过期后新开窗口记为 1。
    不限频时不写表。
    """
    if window_seconds <= 0 or max_count <= 0:
        return
    if not str(material_id).strip():
        return

    now_str = _beijing_now_str()
    now_dt = _parse_beijing_dt(now_str)
    if now_dt is None:
        return

    rows = db.select(
        table=RATE_LIMIT_TABLE,
        where="target_uid = ? AND material_id = ?",
        params=(_rate_target_uid(target_uid), material_id),
        limit=1,
    )

    if not rows:
        db.insert(
            table=RATE_LIMIT_TABLE,
            data={
                "target_uid": _rate_target_uid(target_uid),
                "material_id": material_id,
                "limit_started_at": now_str,
                "use_count": 1,
            },
        )
        return

    row = rows[0]
    start_dt = _parse_beijing_dt(str(row.get("limit_started_at") or ""))
    try:
        use_count = int(row.get("use_count") or 0)
    except (TypeError, ValueError):
        use_count = 0

    if start_dt is None:
        db.update(
            table=RATE_LIMIT_TABLE,
            data={
                "limit_started_at": now_str,
                "use_count": 1,
                "updated_at": now_str,
            },
            where="target_uid = ? AND material_id = ?",
            params=(_rate_target_uid(target_uid), material_id),
        )
        return

    window_end = start_dt + timedelta(seconds=window_seconds)
    if window_end > now_dt:
        db.update(
            table=RATE_LIMIT_TABLE,
            data={"use_count": use_count + 1, "updated_at": now_str},
            where="target_uid = ? AND material_id = ?",
            params=(_rate_target_uid(target_uid), material_id),
        )
        return

    db.update(
        table=RATE_LIMIT_TABLE,
        data={
            "limit_started_at": now_str,
            "use_count": 1,
            "updated_at": now_str,
        },
        where="target_uid = ? AND material_id = ?",
        params=(_rate_target_uid(target_uid), material_id),
    )


def rate_limit_strategy_should_skip(
    db: SQLiteStore,
    material_id: str,
    strategy_id: str,
    window_seconds: int,
    max_count: int,
    target_uid: Optional[str] = None,
) -> bool:
    """分策略限频：同一素材在不同策略下独立计数。"""
    if window_seconds <= 0 or max_count <= 0:
        return False
    mid = str(material_id).strip()
    sid = str(strategy_id).strip()
    if not mid or not sid:
        return False

    now_str = _beijing_now_str()
    now_dt = _parse_beijing_dt(now_str)
    if now_dt is None:
        return False

    rows = db.select(
        table=RATE_LIMIT_STRATEGY_TABLE,
        where="target_uid = ? AND material_id = ? AND strategy_id = ?",
        params=(_rate_target_uid(target_uid), mid, sid),
        limit=1,
    )

    if not rows:
        return False

    row = rows[0]
    start_s = row.get("limit_started_at") or ""
    start_dt = _parse_beijing_dt(str(start_s))
    try:
        use_count = int(row.get("use_count") or 0)
    except (TypeError, ValueError):
        use_count = 0

    if start_dt is None:
        return False

    window_end = start_dt + timedelta(seconds=window_seconds)
    if window_end > now_dt:
        return use_count >= max_count
    return False


def rate_limit_strategy_remaining_capacity(
    db: SQLiteStore,
    material_id: str,
    strategy_id: str,
    window_seconds: int,
    max_count: int,
    target_uid: Optional[str] = None,
) -> Optional[int]:
    """返回指定策略当前窗口剩余次数；None 表示不限频。"""
    if window_seconds <= 0 or max_count <= 0:
        return None
    mid = str(material_id).strip()
    sid = str(strategy_id).strip()
    if not mid or not sid:
        return 0
    rows = db.select(
        table=RATE_LIMIT_STRATEGY_TABLE,
        where="target_uid = ? AND material_id = ? AND strategy_id = ?",
        params=(_rate_target_uid(target_uid), mid, sid),
        limit=1,
    )
    if not rows:
        return max_count
    row = rows[0]
    start_dt = _parse_beijing_dt(str(row.get("limit_started_at") or ""))
    now_dt = _parse_beijing_dt(_beijing_now_str())
    if start_dt is None or now_dt is None:
        return max_count
    if start_dt + timedelta(seconds=window_seconds) <= now_dt:
        return max_count
    try:
        use_count = int(row.get("use_count") or 0)
    except (TypeError, ValueError):
        use_count = 0
    return max(0, max_count - use_count)


def rate_limit_strategy_record_success(
    db: SQLiteStore,
    material_id: str,
    strategy_id: str,
    window_seconds: int,
    max_count: int,
    target_uid: Optional[str] = None,
) -> None:
    if window_seconds <= 0 or max_count <= 0:
        return
    mid = str(material_id).strip()
    sid = str(strategy_id).strip()
    if not mid or not sid:
        return

    now_str = _beijing_now_str()
    now_dt = _parse_beijing_dt(now_str)
    if now_dt is None:
        return

    rows = db.select(
        table=RATE_LIMIT_STRATEGY_TABLE,
        where="target_uid = ? AND material_id = ? AND strategy_id = ?",
        params=(_rate_target_uid(target_uid), mid, sid),
        limit=1,
    )

    if not rows:
        db.insert(
            table=RATE_LIMIT_STRATEGY_TABLE,
            data={
                "target_uid": _rate_target_uid(target_uid),
                "material_id": mid,
                "strategy_id": sid,
                "limit_started_at": now_str,
                "use_count": 1,
            },
        )
        return

    row = rows[0]
    start_dt = _parse_beijing_dt(str(row.get("limit_started_at") or ""))
    try:
        use_count = int(row.get("use_count") or 0)
    except (TypeError, ValueError):
        use_count = 0

    if start_dt is None:
        db.update(
            table=RATE_LIMIT_STRATEGY_TABLE,
            data={
                "limit_started_at": now_str,
                "use_count": 1,
                "updated_at": now_str,
            },
            where="target_uid = ? AND material_id = ? AND strategy_id = ?",
            params=(_rate_target_uid(target_uid), mid, sid),
        )
        return

    window_end = start_dt + timedelta(seconds=window_seconds)
    if window_end > now_dt:
        db.update(
            table=RATE_LIMIT_STRATEGY_TABLE,
            data={"use_count": use_count + 1, "updated_at": now_str},
            where="target_uid = ? AND material_id = ? AND strategy_id = ?",
            params=(_rate_target_uid(target_uid), mid, sid),
        )
        return

    db.update(
        table=RATE_LIMIT_STRATEGY_TABLE,
        data={
            "limit_started_at": now_str,
            "use_count": 1,
            "updated_at": now_str,
        },
        where="target_uid = ? AND material_id = ? AND strategy_id = ?",
        params=(_rate_target_uid(target_uid), mid, sid),
    )


def rate_limit_increment_manual_only(
    db: SQLiteStore,
    material_id: str,
    window_seconds: int,
    max_count: int,
    target_uid: Optional[str] = None,
) -> None:
    """
    即刻追投「表单已就绪」成功时：仅对 use_count +1，不修改 limit_started_at（不重置窗口起点）。
    与自动追投的 rate_limit_record_success 区分，避免与调度器的窗口轮转逻辑打架。
    不限频时不写表。
    """
    if window_seconds <= 0 or max_count <= 0:
        return
    if not str(material_id).strip():
        return

    now_str = _beijing_now_str()
    rows = db.select(
        table=RATE_LIMIT_TABLE,
        where="target_uid = ? AND material_id = ?",
        params=(_rate_target_uid(target_uid), material_id),
        limit=1,
    )

    if not rows:
        db.insert(
            table=RATE_LIMIT_TABLE,
            data={
                "target_uid": _rate_target_uid(target_uid),
                "material_id": material_id,
                "limit_started_at": now_str,
                "use_count": 1,
            },
        )
        return

    row = rows[0]
    try:
        use_count = int(row.get("use_count") or 0)
    except (TypeError, ValueError):
        use_count = 0

    db.update(
        table=RATE_LIMIT_TABLE,
        data={"use_count": use_count + 1, "updated_at": now_str},
        where="target_uid = ? AND material_id = ?",
        params=(_rate_target_uid(target_uid), material_id),
    )


def rate_limit_increment_manual_only_strategy(
    db: SQLiteStore,
    material_id: str,
    strategy_id: str,
    window_seconds: int,
    max_count: int,
    target_uid: Optional[str] = None,
) -> None:
    """即刻追投成功：分策略限频表仅 use_count+1，不重置窗口起点。"""
    if window_seconds <= 0 or max_count <= 0:
        return
    mid = str(material_id).strip()
    sid = str(strategy_id).strip()
    if not mid or not sid:
        return

    now_str = _beijing_now_str()
    rows = db.select(
        table=RATE_LIMIT_STRATEGY_TABLE,
        where="target_uid = ? AND material_id = ? AND strategy_id = ?",
        params=(_rate_target_uid(target_uid), mid, sid),
        limit=1,
    )

    if not rows:
        db.insert(
            table=RATE_LIMIT_STRATEGY_TABLE,
            data={
                "target_uid": _rate_target_uid(target_uid),
                "material_id": mid,
                "strategy_id": sid,
                "limit_started_at": now_str,
                "use_count": 1,
            },
        )
        return

    row = rows[0]
    try:
        use_count = int(row.get("use_count") or 0)
    except (TypeError, ValueError):
        use_count = 0

    db.update(
        table=RATE_LIMIT_STRATEGY_TABLE,
        data={"use_count": use_count + 1, "updated_at": now_str},
        where="target_uid = ? AND material_id = ? AND strategy_id = ?",
        params=(_rate_target_uid(target_uid), mid, sid),
    )


def _insert_run(
    db: SQLiteStore,
    *,
    aavid: str,
    ad_id: str,
    material_id: str,
    target_uid: str = "legacy_unscoped",
    promotion_scene: str = "live",
    plan_system: str = "unknown",
    trigger_level: str = "material",
    product_id: str = "",
    product_name: str = "",
    material_name: str = "",
    strategy_name: str = "",
    regulate_task_id: str = "",
    started_at: str,
    ended_at: str,
    duration_ms: int,
    status: int,
    step: str,
    message: str,
    detail: str,
    retargeting: Dict[str, Any],
    rule_full_json: str,
    trigger_snapshot_json: str,
    query_snapshot_json: str,
    headless: bool,
    browser_headless_rule: bool,
    trigger_source: str = "scheduler",
    cloud_task_id: str = "",
    operator_id: str = "",
    materials: Optional[List[Dict[str, Any]]] = None,
) -> None:
    _rid = str(regulate_task_id or "").strip()
    _mn = str(material_name or "").strip()
    _sn = str(strategy_name or "").strip()[:128]
    if not _sn or _sn == "?":
        _sn = None
    try:
        target = db.select_one(
            "promotion_target",
            fields="account_uid",
            where={"target_uid": _rate_target_uid(target_uid)},
        ) or {}
        account_uid = str(target.get("account_uid") or "")
    except Exception:
        account_uid = ""
    data: Dict[str, Any] = {
        "aavid": aavid,
        "account_uid": account_uid,
        "ad_id": ad_id,
        "target_uid": _rate_target_uid(target_uid),
        "promotion_scene": str(promotion_scene or "live"),
        "plan_system": normalize_plan_system(plan_system or "unknown"),
        "trigger_level": (
            "product" if str(trigger_level or "material") == "product" else "material"
        ),
        "product_id": str(product_id or "").strip() or None,
        "product_name": str(product_name or "").strip() or None,
        "material_id": material_id,
        "material_name": _mn if _mn else None,
        "materials_json": _json_dumps(materials or []),
        "strategy_name": _sn,
        "regulate_task_id": _rid if _rid else None,
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_ms": duration_ms,
        "status": status,
        "step": step,
        "message": message[:2000] if message else "",
        "detail": detail[:8000] if detail else "",
        "retargeting_method": str(retargeting.get("method") or "")[:64],
        "optimization_goal": (_optimization_goal_str(retargeting) or "")[:64] or None,
        "retargeting_json": _json_dumps(retargeting),
        "rule_full_json": rule_full_json,
        "trigger_snapshot_json": trigger_snapshot_json,
        "query_snapshot_json": query_snapshot_json,
        "headless": 1 if headless else 0,
        "browser_headless_rule": 1 if browser_headless_rule else 0,
        "trigger_source": (str(trigger_source or "scheduler").strip()[:64] or "scheduler"),
        "execution_uid": str(cloud_task_id or "").strip() or None,
        "execution_state": (
            "submitted_verifying"
            if str(step or "") == "submitted_verifying"
            else ("confirmed_succeeded" if status == 1 else "confirmed_failed")
        ),
        "app_version": CURRENT_VERSION,
    }
    run_id = db.insert(table="pmc_retargeting_run", data=data)
    try:
        from api.operation_events import upsert_operation_event

        upsert_operation_event(
            {
                "event_uid": f"retarget_run:{run_id}",
                "aavid": aavid,
                "account_uid": account_uid,
                "ad_id": ad_id,
                "target_uid": _rate_target_uid(target_uid),
                "promotion_scene": str(promotion_scene or "live"),
                "plan_system": normalize_plan_system(plan_system or "unknown"),
                "source": "tool_direct",
                "action_type": "retarget",
                "object_type": "assist_task" if len(materials or []) > 1 else "material",
                "object_id": _rid or material_id,
                "object_name": (
                    f"{len(materials or [])}条素材追投"
                    if len(materials or []) > 1
                    else _mn
                ),
                "plan_id": ad_id,
                "material_id": material_id,
                "material_name": _mn,
                "product_id": str(product_id or "").strip(),
                "product_name": str(product_name or "").strip(),
                "regulate_task_id": _rid,
                "status": (
                    "pending"
                    if str(step or "") == "submitted_verifying"
                    else ("success" if status == 1 else "failed")
                ),
                "summary": message or "追投",
                "detail": detail,
                "after": {
                    "regulate_task_id": _rid,
                    "materials": materials or [],
                },
                "trigger_json": trigger_snapshot_json,
                "request_json": data["retargeting_json"],
                "response": {"step": step},
                "raw": {"materials": materials or []},
                "cloud_task_id": cloud_task_id,
                "operator_id": operator_id,
                "operator_name": "飞书确认用户" if operator_id else "工具",
                "occurred_at": ended_at,
            },
            db,
        )
    except Exception:
        logger.exception("%s 统一操作流水写入失败 run_id=%s", retarget_log_tag(scheduler=True), run_id)
    if cloud_task_id and step == "submitted_verifying":
        try:
            from services.official_api_reconciliation import replay_terminal_reconciliations
            account = db.select_one("qianchuan_account", where={"account_uid": account_uid}) or {}
            replay_terminal_reconciliations(
                str(account.get("owner_username") or ""), db=db, execution_uid=cloud_task_id,
            )
        except Exception:
            logger.exception("追投流水已保存，补齐已完成核验结果失败 run_id=%s", run_id)


async def run_one_cycle(db: SQLiteStore, *, target_uids=None) -> None:
    _log_sched = retarget_log_tag(scheduler=True)
    cfg = load_rule_retargeting_config()
    if not cfg.get("enabled"):
        logger.info("%s 未启用 enabled，跳过本轮", _log_sched)
        return

    period = str(cfg.get("trigger_query_period") or "1h").strip() or "1h"
    strategies = cfg.get("strategies")
    if not isinstance(strategies, list) or not strategies:
        trig = cfg.get("trigger")
        ret = cfg.get("retargeting") if isinstance(cfg.get("retargeting"), dict) else {}
        if not isinstance(trig, dict):
            logger.warning("%s trigger 非法，跳过", _log_sched)
            return
        strategies = [
            {"id": "", "title": "策略 1", "trigger": trig, "retargeting": ret},
        ]

    if target_uids is not None:
        scope = {str(uid) for uid in target_uids}
        strategies = [st for st in strategies if isinstance(st, dict) and str(st.get("target_uid") or "") in scope]
        if not strategies:
            return

    rule_full_json = _json_dumps(cfg)
    ws, mc = _interval_from_root_cfg(cfg)
    per_strategy_rl = bool(cfg.get("per_strategy_rate_limit"))

    enabled_targets = (
        schedulable_promotion_targets(db=db)
        if hasattr(db, "config")
        else db.select(
            "promotion_target",
            where="enabled=1 AND capacity_state='active'",
            order_by="updated_at DESC, id DESC",
        )
    )
    has_material_strategies = any(
        str((item or {}).get("task_action") or "create_retarget")
        != "increase_budget"
        for item in strategies
        if isinstance(item, dict)
    )
    dash = DashboardApi()
    rows: List[Dict[str, Any]] = []
    period_label = period
    if has_material_strategies:
        strategy_target_uids = {
            str(item.get("target_uid") or "").strip()
            for item in strategies
            if isinstance(item, dict)
            and str(item.get("task_action") or "create_retarget")
            != "increase_budget"
            and str(item.get("target_uid") or "").strip()
        }
        if not strategy_target_uids and len(enabled_targets) == 1:
            strategy_target_uids = {
                str(enabled_targets[0].get("target_uid") or "").strip()
            }
        # Filter in SQLite by target before material rows reach Python. This
        # bounds memory by monitored targets instead of the lifetime history of
        # every account in the local database.
        for strategy_target_uid in sorted(strategy_target_uids):
            resp = dash.get_table_data(
                period=period,
                sort_by="costDiff",
                sort_order="desc",
                page=1,
                page_size=_DASHBOARD_PAGE_SIZE,
                target_uid=strategy_target_uid,
            )
            if not resp.get("success"):
                logger.warning(
                    "%s get_table_data 失败 target=%s: %s",
                    _log_sched,
                    strategy_target_uid,
                    resp.get("message"),
                )
                return
            rows.extend(
                item for item in (resp.get("data") or []) if isinstance(item, dict)
            )
            period_label = str(resp.get("period") or period_label)
    assist_rows: List[Dict[str, Any]] = []
    if any(
        str((item or {}).get("task_action") or "create_retarget")
        == "increase_budget"
        for item in strategies
        if isinstance(item, dict)
    ):
        assist_resp = dash.get_roi2_assist_table_data(
            sort_by="stat_cost_for_roi2_assist",
            sort_order="desc",
            page=1,
            page_size=_DASHBOARD_PAGE_SIZE,
            ad_delivery_type=0,
            regulation_full_scan=True,
            assist_updated_within_minutes=10,
            **({"target_uids": sorted({str(st.get("target_uid") or "") for st in strategies})}
               if target_uids is not None else {}),
        )
        if not assist_resp.get("success"):
            logger.warning(
                "%s get_roi2_assist_table_data failed: %s",
                _log_sched,
                assist_resp.get("message"),
            )
            return
        assist_rows = [
            row for row in (assist_resp.get("data") or [])
            if isinstance(row, dict)
        ]
    account_name = str((dash.get_dashboard_account_label() or {}).get("label") or "").strip()
    query_at = _beijing_now_str()
    period_label = period_label or ""

    logger.info(
        "%s 周期=%s 素材总数=%s 策略数=%s 并行上限=%s",
        _log_sched,
        period,
        len(rows),
        len(strategies),
        min(MAX_STRATEGY_PARALLEL, len(strategies)),
    )

    sem = asyncio.Semaphore(MAX_STRATEGY_PARALLEL)
    browser_rule = bool(cfg.get("browser_headless", True))
    async def process_strategy(st: Dict[str, Any]) -> None:
        async with sem:
            trigger = st.get("trigger") or {}
            if not isinstance(trigger, dict):
                logger.warning("%s 策略 %s trigger 非法，跳过", _log_sched, st.get("id"))
                return
            retargeting = st.get("retargeting") or {}
            if not isinstance(retargeting, dict):
                retargeting = {}
            target_uid = str(st.get("target_uid") or "").strip()
            if not target_uid:
                # 旧版规则只在恰好一条启用计划时自动绑定；多计划时拒绝猜测。
                if len(enabled_targets) == 1:
                    target_uid = str(enabled_targets[0].get("target_uid") or "").strip()
                else:
                    logger.warning(
                        "%s 策略 %s 未选择监控计划，当前启用计划数=%s，已安全跳过",
                        _log_sched,
                        st.get("id"),
                        len(enabled_targets),
                    )
                    return
            target = next(
                (
                    item
                    for item in enabled_targets
                    if str(item.get("target_uid") or "") == target_uid
                ),
                None,
            )
            if not target:
                logger.warning(
                    "%s 策略 %s 对应监控计划不存在或已停用 target=%s",
                    _log_sched,
                    st.get("id"),
                    target_uid,
                )
                return
            strategy_account_uid = str(st.get("account_uid") or "").strip()
            target_account_uid = str(target.get("account_uid") or "").strip()
            if strategy_account_uid and strategy_account_uid != target_account_uid:
                logger.error(
                    "%s 策略 %s 的监控账户与计划归属不一致，已安全跳过：strategy_account=%s target_account=%s",
                    _log_sched,
                    st.get("id"),
                    strategy_account_uid,
                    target_account_uid,
                )
                return
            if not bool(target.get("retarget_eligible")):
                logger.warning(
                    "%s 策略 %s 对应计划尚未取得可追投资格，已跳过：%s",
                    _log_sched,
                    st.get("id"),
                    target.get("ineligible_reason") or "unknown",
                )
                return
            if bool(target.get("automation_write_blocked")):
                logger.warning(
                    "%s 策略 %s 对应计划已被持久写入保护封锁，"
                    "本轮不发送追投卡片：target=%s reason=%s",
                    _log_sched,
                    st.get("id"),
                    target_uid,
                    str(target.get("write_block_reason") or "unknown"),
                )
                return
            target_status = str(target.get("last_status") or "").strip().lower()
            if not target_has_usable_collection_snapshot(target):
                logger.warning(
                    "%s 策略 %s 对应计划当前不可追投 "
                    "target=%s status=%s，已跳过历史素材",
                    _log_sched,
                    st.get("id"),
                    target_uid,
                    target_status or "unknown",
                )
                return
            promotion_scene = str(target.get("promotion_scene") or "live").strip()
            plan_system = normalize_plan_system(
                target.get("plan_system") or "unknown"
            )
            target_account_name = _account_name_for_target(
                db,
                target,
                account_name,
            )
            if plan_system == "unknown":
                logger.warning(
                    "%s 策略 %s 对应计划尚未确认是全域还是千川乘方，"
                    "本轮不发送卡片、不执行追投",
                    _log_sched,
                    st.get("id"),
                )
                return
            task_action = str(
                st.get("task_action") or "create_retarget"
            ).strip().lower()
            if task_action != "increase_budget":
                report_units = target_report_metric_units(target)
                unsupported_metrics = unsupported_strategy_monitor_metrics(st, target)
                if not report_units or unsupported_metrics:
                    logger.warning(
                        "%s 策略 %s 的官方监控指标尚未就绪或已失效，"
                        "本轮不生成候选、不执行追投：target=%s unsupported=%s",
                        _log_sched,
                        st.get("id"),
                        target_uid,
                        ",".join(unsupported_metrics) or "report_metric_units_missing",
                    )
                    return
            if task_action == "increase_budget":
                sync_ready, sync_error = assist_task_sync_ready(
                    target,
                    max_age_minutes=10,
                )
                if not sync_ready:
                    logger.warning(
                        "%s strategy %s skipped: %s",
                        _log_sched,
                        st.get("id"),
                        sync_error,
                    )
                    return
                action_mode = str(
                    st.get("action_mode") or "card_confirm"
                ).strip().lower()
                if action_mode not in ("card_confirm", "auto_execute"):
                    action_mode = "card_confirm"
                increase_config = (
                    retargeting.get("budget_increase")
                    if isinstance(retargeting.get("budget_increase"), dict)
                    else {}
                )
                matching_tasks: List[Dict[str, Any]] = []
                for assist_row in assist_rows:
                    if (
                        str(assist_row.get("target_uid") or "") != target_uid
                        or str(assist_row.get("aadvid") or "")
                        != str(target.get("aadvid") or "")
                        or str(assist_row.get("ad_id") or "")
                        != str(target.get("ad_id") or "")
                    ):
                        continue
                    if not _timestamp_is_fresh(assist_row.get("metrics_observed_at")):
                        continue
                    delivery_type = assist_row.get("ad_delivery_type")
                    if str(
                        delivery_type if delivery_type is not None else "0"
                    ).strip() not in {"", "0"}:
                        continue
                    if evaluate_trigger(trigger, assist_row):
                        matching_tasks.append(assist_row)
                logger.info(
                    "%s target=%s task_action=increase_budget hits=%s",
                    retarget_log_tag(
                        strategy_title=str(st.get("title") or st.get("id") or "?")[:64]
                    ),
                    target_uid,
                    len(matching_tasks),
                )
                if not matching_tasks:
                    return
                if action_mode == "auto_execute":
                    logger.warning(
                        "%s automatic budget increase is blocked until the live platform adjustment contract is verified",
                        retarget_log_tag(
                            strategy_title=str(st.get("title") or st.get("id") or "?")[:64]
                        ),
                    )
                    return

                strategy_snapshot = _strategy_snapshot(st)
                strategy_json = json.dumps(
                    strategy_snapshot,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                strategy_hash = hashlib.sha256(
                    strategy_json.encode("utf-8")
                ).hexdigest()
                for assist_row in matching_tasks:
                    try:
                        calculation = calculate_budget_increase(
                            assist_row,
                            increase_config,
                        )
                    except ValueError as exc:
                        logger.warning(
                            "%s budget calculation blocked task=%s: %s",
                            _log_sched,
                            assist_row.get("assist_task_id"),
                            exc,
                        )
                        continue
                    assist_task_id = str(
                        assist_row.get("assist_task_id") or ""
                    ).strip()
                    if not assist_task_id:
                        continue
                    calculation_fingerprint = budget_increase_fingerprint(
                        target_uid=target_uid,
                        strategy_id=str(st.get("id") or ""),
                        calculation=calculation,
                    )
                    card_payload = {
                        "task_operation": "increase_budget",
                        "aavid": str(target.get("aadvid") or ""),
                        "account_name": target_account_name,
                        "ad_id": str(target.get("ad_id") or ""),
                        "target_uid": target_uid,
                        "plan_name": str(target.get("plan_name") or ""),
                        "promotion_scene": promotion_scene,
                        "plan_system": plan_system,
                        "assist_task_id": assist_task_id,
                        "assist_task_name": str(
                            assist_row.get("task_name") or assist_task_id
                        ),
                        "strategy_id": str(st.get("id") or "__legacy__"),
                        "strategy_name": str(
                            st.get("title") or st.get("id") or "追加预算策略"
                        )[:64],
                        "strategy_hash": strategy_hash,
                        "rule_snapshot": strategy_snapshot,
                        "trigger_snapshot": {
                            "strategy_id": st.get("id"),
                            "strategy_title": st.get("title"),
                            "trigger_config": trigger,
                            "evaluation": build_trigger_evaluation_snapshot(
                                trigger,
                                assist_row,
                            ),
                        },
                        "query_snapshot": {
                            "data_source": "pmc_roi2_assist_task",
                            "query_at": query_at,
                            "assist_row": assist_row,
                            "target": {
                                "target_uid": target_uid,
                                "aavid": target.get("aadvid"),
                                "ad_id": target.get("ad_id"),
                                "plan_name": target.get("plan_name"),
                            },
                        },
                        "metrics_snapshot": assist_row,
                        "budget_increase": increase_config,
                        "calculation_snapshot": calculation,
                        "calculation_fingerprint": calculation_fingerprint,
                    }
                    card_result = await asyncio.to_thread(
                        create_retarget_task,
                        card_payload,
                    )
                    if not card_result.get("success"):
                        logger.warning(
                            "%s budget increase card failed task=%s: %s",
                            _log_sched,
                            assist_task_id,
                            card_result.get("message"),
                        )
                return
            capability_ok, capability_reason = check_target_capability(
                target,
                action="retarget",
                promotion_scene=promotion_scene,
                plan_system=plan_system,
            )
            if not capability_ok:
                logger.warning(
                    "%s 策略 %s 对应计划缺少与场景/体系匹配的追投能力证据：%s；"
                    "本轮不发送卡片、不执行追投",
                    _log_sched,
                    st.get("id"),
                    capability_reason,
                )
                return
            if promotion_scene == "product":
                method = str(
                    retargeting.get("method") or "volume"
                ).strip().lower()
                if not retarget_method_is_supported_for_scene(
                    promotion_scene,
                    retargeting,
                ):
                    logger.warning(
                        "%s 策略 %s 对应推商品计划，但追投方式为 %s；"
                        "推商品当前仅支持放量追投，本轮不发送卡片、不执行追投",
                        _log_sched,
                        st.get("id"),
                        method or "unknown",
                    )
                    return
                try:
                    capability = json.loads(target.get("capability_json") or "{}")
                except (TypeError, ValueError, json.JSONDecodeError):
                    capability = {}
                if not retarget_capability_matches(
                    capability,
                    promotion_scene=promotion_scene,
                    plan_system=plan_system,
                ):
                    logger.warning(
                        "%s 策略 %s 对应商品计划的追投能力证据缺失、过期或与场景/体系不一致，"
                        "本轮不发送追投卡片",
                        _log_sched,
                        st.get("id"),
                    )
                    return
            target_rows = [
                row
                for row in rows
                if isinstance(row, dict)
                and str(row.get("targetUid") or "") == target_uid
            ]

            st_label = str(st.get("title") or st.get("id") or "?")[:64]
            action_mode = str(st.get("action_mode") or "card_confirm").strip().lower()
            if action_mode not in ("card_confirm", "auto_execute"):
                action_mode = "card_confirm"
            _tag = retarget_log_tag(strategy_title=st_label)
            strategy_id_for_rl = str(st.get("id") or "").strip() or "__legacy__"
            ws_s, mc_s = _interval_window_and_max(retargeting)

            scoped_rows = [
                row
                for row in target_rows
                if (
                    action_mode == "card_confirm"
                    or row_is_in_test_scope(row)
                )
                and str(row.get("id") or "").strip() not in ("", "-2")
            ]
            hit_rows: List[Dict[str, Any]] = []
            trigger_level = str(st.get("trigger_level") or "material").strip().lower()
            if trigger_level == "product":
                if promotion_scene != "product":
                    logger.warning("%s 商品级策略不能用于直播计划，已跳过", _tag)
                    return
                relation_rows = db.select(
                    "promotion_material_product",
                    fields="material_id, product_id",
                    where={"target_uid": target_uid},
                )
                relation_map: Dict[str, List[str]] = {}
                for relation in relation_rows:
                    relation_map.setdefault(
                        str(relation.get("material_id") or ""),
                        [],
                    ).append(str(relation.get("product_id") or ""))
                product_rows = db.select(
                    "promotion_product",
                    fields="product_id, product_name",
                    where={"target_uid": target_uid},
                )
                product_names = {
                    str(item.get("product_id") or ""): str(item.get("product_name") or "")
                    for item in product_rows
                }
                allowed_products = st.get("product_filter")
                if not allowed_products and str(target.get("product_filter_mode") or "all") == "selected":
                    try:
                        allowed_products = json.loads(target.get("product_ids_json") or "[]")
                    except (TypeError, ValueError, json.JSONDecodeError):
                        allowed_products = []
                product_hits = evaluate_product_strategy(
                    scoped_rows,
                    st,
                    relation_map=relation_map,
                    product_names=product_names,
                    allowed_product_ids=allowed_products,
                )
                for product_hit in product_hits:
                    for candidate in product_hit.get("candidates") or []:
                        candidate_row = dict(candidate)
                        candidate_row["_trigger_level"] = "product"
                        candidate_row["_product_id"] = str(product_hit.get("productId") or "")
                        candidate_row["_product_name"] = str(product_hit.get("productName") or "")
                        candidate_row["_product_metrics"] = {
                            key: value
                            for key, value in product_hit.items()
                            if key not in ("materials", "candidates")
                        }
                        hit_rows.append(candidate_row)
            else:
                for row in scoped_rows:
                    if evaluate_trigger(trigger, row):
                        candidate_row = dict(row)
                        candidate_row["_trigger_level"] = "material"
                        hit_rows.append(candidate_row)

            # 先对候选去重并稳定排序。飞书确认模式仍保留现有的卡内人工选择
            # 能力；自动模式会在进入执行器前应用 1～20 条候选上限。
            unique_hit_rows: List[Dict[str, Any]] = []
            seen_hit_material_ids = set()
            for row in hit_rows:
                material_id = str(row.get("id") or "").strip()
                if not material_id or material_id in seen_hit_material_ids:
                    continue
                seen_hit_material_ids.add(material_id)
                unique_hit_rows.append(row)
            unique_hit_rows.sort(key=material_net_roi_sort_key)
            try:
                candidate_limit = int(st.get("candidate_limit") or 1)
            except (TypeError, ValueError):
                candidate_limit = 1
            candidate_limit = max(1, min(20, candidate_limit))
            hit_rows = unique_hit_rows

            logger.info(
                "%s target=%s scene=%s level=%s 命中候选素材数=%s",
                _tag,
                target_uid,
                promotion_scene,
                trigger_level,
                len(hit_rows),
            )
            if not hit_rows:
                return

            if action_mode == "card_confirm":
                aavid_raw = target.get("aadvid")
                aavid = str(aavid_raw).strip() if aavid_raw is not None else ""
                ad_id = str(target.get("ad_id") or "").strip()
                try:
                    aavid_int = int(aavid)
                    ad_id_int = int(ad_id)
                except (TypeError, ValueError):
                    logger.warning(
                        "%s 监控计划缺少有效账户或计划ID，无法创建批量追投卡片",
                        _tag,
                    )
                    return

                batch_materials: List[Dict[str, Any]] = []
                material_index: Dict[str, Dict[str, Any]] = {}
                evaluation_snapshots: List[Dict[str, Any]] = []
                query_material_rows: List[Dict[str, Any]] = []
                for row in hit_rows:
                    material_id = str(row.get("id") or "").strip()
                    if not material_id:
                        continue
                    if per_strategy_rl:
                        limited = rate_limit_strategy_should_skip(
                            db,
                            material_id,
                            strategy_id_for_rl,
                            ws_s,
                            mc_s,
                            target_uid,
                        )
                    else:
                        limited = rate_limit_should_skip(
                            db,
                            material_id,
                            ws,
                            mc,
                            target_uid,
                        )
                    if limited:
                        logger.info(
                            "%s 批量卡片跳过已达限频素材 material_id=%s",
                            _tag,
                            material_id,
                        )
                        continue

                    product_id = str(row.get("_product_id") or "").strip()
                    product_name = str(row.get("_product_name") or "").strip()
                    evaluation_row = (
                        row.get("_product_metrics")
                        if trigger_level == "product"
                        and isinstance(row.get("_product_metrics"), dict)
                        else row
                    )
                    if material_id in material_index:
                        existing = material_index[material_id]
                        if product_id and product_id not in existing["product_ids"]:
                            existing["product_ids"].append(product_id)
                        continue
                    if len(batch_materials) >= 20:
                        logger.warning(
                            "%s 单个追投计划最多20条素材，其余命中素材留待本任务结束后再次提醒",
                            _tag,
                        )
                        break

                    material = {
                        "material_id": material_id,
                        "material_name": _material_name_from_dashboard_row(row),
                        "product_id": product_id,
                        "product_name": product_name,
                        "product_ids": [product_id] if product_id else [],
                    }
                    batch_materials.append(material)
                    material_index[material_id] = material
                    evaluation_snapshots.append(
                        {
                            "material_id": material_id,
                            "product_id": product_id,
                            "product_name": product_name,
                            "evaluation": build_trigger_evaluation_snapshot(
                                trigger,
                                evaluation_row,
                            ),
                        }
                    )
                    query_material_rows.append(
                        {
                            "material_id": material_id,
                            "material_name": material["material_name"],
                            "product_id": product_id,
                            "product_name": product_name,
                            "material_row": row,
                        }
                    )

                if not batch_materials:
                    return
                # Keep card creation and confirmation on the exact same
                # canonical snapshot contract.  A hand-built snapshot here
                # previously omitted task_action, so every confirmation was
                # incorrectly rejected as “策略参数已经变更”.
                strategy_snapshot = _strategy_snapshot(st)
                strategy_json = json.dumps(
                    strategy_snapshot,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                strategy_hash = hashlib.sha256(
                    strategy_json.encode("utf-8")
                ).hexdigest()
                product_ids = {
                    pid
                    for material in batch_materials
                    for pid in material.get("product_ids") or []
                    if pid
                }
                first_material = batch_materials[0]
                trigger_snapshot = {
                    "strategy_id": st.get("id"),
                    "strategy_title": st.get("title"),
                    "target_uid": target_uid,
                    "promotion_scene": promotion_scene,
                    "plan_system": plan_system,
                    "trigger_level": trigger_level,
                    "trigger_config": trigger,
                    "material_count": len(batch_materials),
                    "materials": evaluation_snapshots,
                }
                query_snapshot = {
                    "query_period": period,
                    "period_label": period_label,
                    "query_at": query_at,
                    "dashboard_total": len(rows),
                    "materials": query_material_rows,
                    "target": {
                        "target_uid": target_uid,
                        "aavid": aavid,
                        "ad_id": ad_id,
                        "plan_name": target.get("plan_name"),
                        "promotion_scene": promotion_scene,
                        "plan_system": plan_system,
                    },
                }
                card_payload = {
                    "aavid": str(aavid_int),
                    "account_name": target_account_name,
                    "ad_id": str(ad_id_int),
                    "target_uid": target_uid,
                    "plan_name": str(target.get("plan_name") or ""),
                    "promotion_scene": promotion_scene,
                    "plan_system": plan_system,
                    "trigger_level": trigger_level,
                    "product_id": (
                        str(first_material.get("product_id") or "")
                        if len(product_ids) == 1
                        else ""
                    ),
                    "product_name": (
                        str(first_material.get("product_name") or "")
                        if len(product_ids) == 1
                        else ""
                    ),
                    "material_id": str(first_material["material_id"]),
                    "material_name": str(first_material["material_name"]),
                    "materials": batch_materials,
                    "strategy_id": str(st.get("id") or "__legacy__"),
                    "strategy_name": st_label,
                    "strategy_hash": strategy_hash,
                    "trigger_snapshot": trigger_snapshot,
                    "query_snapshot": query_snapshot,
                    "retargeting": retargeting,
                    "rule_snapshot": strategy_snapshot,
                    "evaluation_interval_seconds": DEFAULT_INTERVAL_SEC,
                    "effective_rate_limit": {
                        "window_seconds": ws_s if per_strategy_rl else ws,
                        "max_count": mc_s if per_strategy_rl else mc,
                        "scope": "strategy" if per_strategy_rl else "global",
                    },
                }
                card_result = await asyncio.to_thread(
                    create_retarget_task,
                    card_payload,
                )
                if card_result.get("success"):
                    logger.info(
                        "%s 已登记批量飞书确认卡片 materials=%s task_uid=%s duplicate=%s delivery_state=%s",
                        _tag,
                        len(batch_materials),
                        (card_result.get("data") or {}).get("task_uid"),
                        bool(card_result.get("duplicate")),
                        (card_result.get("data") or {}).get("delivery_state", "unrecorded"),
                    )
                else:
                    logger.warning(
                        "%s 批量飞书确认任务创建失败: %s",
                        _tag,
                        card_result.get("message") or "未知错误",
                    )
                return

            svc = QianChuanRetargetingService.from_rule_file_dict(cfg)
            headless_cfg = browser_rule
            if action_mode == "auto_execute":
                hit_rows = hit_rows[:candidate_limit]
            grouping_mode = (
                "merged"
                if str(st.get("material_grouping_mode") or "separate").strip().lower()
                == "merged"
                else "separate"
            )
            if action_mode == "auto_execute" and grouping_mode == "merged":
                batch_rows: List[Dict[str, Any]] = []
                for row in hit_rows:
                    material_id = str(row.get("id") or "").strip()
                    if not material_id:
                        continue
                    if per_strategy_rl:
                        limited = rate_limit_strategy_should_skip(
                            db,
                            material_id,
                            strategy_id_for_rl,
                            ws_s,
                            mc_s,
                            target_uid,
                        )
                    else:
                        limited = rate_limit_should_skip(
                            db,
                            material_id,
                            ws,
                            mc,
                            target_uid,
                        )
                    if not limited:
                        batch_rows.append(row)
                if not batch_rows:
                    await svc.close()
                    return
                if not auto_execute_allowed_in_current_environment():
                    logger.warning("%s 本地测试模式禁止自动合并追投", _tag)
                    await svc.close()
                    return

                material_ids = [str(row.get("id") or "").strip() for row in batch_rows]
                first_row = batch_rows[0]
                aavid = str(target.get("aadvid") or "").strip()
                ad_id = str(target.get("ad_id") or "").strip()
                started_at = _beijing_now_str()
                t0 = time.time()
                result = None
                locked_retargeting = retargeting
                execution_uid = _auto_execution_uid(
                    target_uid=target_uid,
                    strategy_id=strategy_id_for_rl,
                    material_ids=material_ids,
                )
                try:
                    aavid_int = int(aavid)
                    ad_id_int = int(ad_id)
                    material_locks = await _locks_for_material_retouch(material_ids, target_uid)
                    async with _MaterialLockGroup(material_locks), exclusive_browser_operation(
                            f"合并追投:{target_uid}:{','.join(material_ids)}",
                            priority=10,
                        ):
                        locked_cfg = None
                        locked_strategy = None
                        locked_target = None
                        for row in batch_rows:
                            current_cfg, current_strategy, current_target = await asyncio.to_thread(
                                _revalidate_auto_retarget_under_lock,
                                db,
                                original_strategy=st,
                                target_uid=target_uid,
                                aavid=aavid,
                                ad_id=ad_id,
                                promotion_scene=promotion_scene,
                                plan_system=plan_system,
                                material_id=str(row.get("id") or ""),
                                product_id=str(row.get("_product_id") or ""),
                            )
                            locked_cfg = current_cfg
                            locked_strategy = current_strategy
                            locked_target = current_target
                        locked_retargeting = (
                            locked_strategy.get("retargeting")
                            if isinstance((locked_strategy or {}).get("retargeting"), dict)
                            else {}
                        )
                        result = await svc.run(
                            aavid=aavid_int,
                            ad_id=ad_id_int,
                            material_id=material_ids[0],
                            material_ids=material_ids,
                            retargeting=locked_retargeting,
                            strategy_title=st_label,
                            execution_uid=execution_uid,
                            reconciliation_task_uid=execution_uid,
                            target_uid=target_uid,
                            promotion_scene=promotion_scene,
                            plan_system=plan_system,
                            source_url=((locked_target or {}).get("sanitized_page_url") or None),
                            reuse_session=False,
                            close_session=False,
                        )
                        if result.success:
                            record_retarget_rate_success_safely(
                                db,
                                material_ids=material_ids,
                                target_uid=target_uid,
                                cfg=locked_cfg or cfg,
                                strategy=locked_strategy or st,
                                retargeting=locked_retargeting,
                            )
                except Exception as exc:
                    ended_at = _beijing_now_str()
                    _insert_run(
                        db,
                        aavid=aavid,
                        ad_id=ad_id,
                        material_id=material_ids[0],
                        target_uid=target_uid,
                        promotion_scene=promotion_scene,
                        plan_system=plan_system,
                        trigger_level=trigger_level,
                        product_id=str(first_row.get("_product_id") or ""),
                        product_name=str(first_row.get("_product_name") or ""),
                        material_name=_material_name_from_dashboard_row(first_row),
                        strategy_name=st_label,
                        started_at=started_at,
                        ended_at=ended_at,
                        duration_ms=int((time.time() - t0) * 1000),
                        status=-1,
                        step="batch_exception",
                        message=str(exc),
                        detail=traceback.format_exc()[:8000],
                        retargeting=locked_retargeting,
                        rule_full_json=rule_full_json,
                        trigger_snapshot_json=_json_dumps({"material_ids": material_ids}),
                        query_snapshot_json=_json_dumps({"materials": batch_rows}),
                        headless=headless_cfg,
                        browser_headless_rule=browser_rule,
                        materials=[
                            {
                                "material_id": str(row.get("id") or ""),
                                "material_name": _material_name_from_dashboard_row(row),
                            }
                            for row in batch_rows
                        ],
                    )
                    logger.exception("%s 自动合并追投异常 materials=%s", _tag, material_ids)
                    await svc.close()
                    return

                ended_at = _beijing_now_str()
                _insert_run(
                    db,
                    aavid=str(result.aavid or aavid),
                    ad_id=str(result.ad_id or ad_id),
                    material_id=material_ids[0],
                    target_uid=target_uid,
                    promotion_scene=promotion_scene,
                    plan_system=plan_system,
                    trigger_level=trigger_level,
                    product_id=str(first_row.get("_product_id") or ""),
                    product_name=str(first_row.get("_product_name") or ""),
                    material_name=_material_name_from_dashboard_row(first_row),
                    strategy_name=st_label,
                    regulate_task_id=str(result.regulate_task_id or ""),
                    started_at=started_at,
                    ended_at=ended_at,
                    duration_ms=int((time.time() - t0) * 1000),
                    status=1 if result.success else -1,
                    step=result.step,
                    message=result.message,
                    detail="" if result.success else (result.detail or ""),
                    retargeting=locked_retargeting,
                    rule_full_json=rule_full_json,
                    trigger_snapshot_json=_json_dumps({"material_ids": material_ids}),
                    query_snapshot_json=_json_dumps({"materials": batch_rows}),
                    headless=bool(result.headless),
                    browser_headless_rule=browser_rule,
                    cloud_task_id=execution_uid,
                    materials=[
                        {
                            "material_id": str(row.get("id") or ""),
                            "material_name": _material_name_from_dashboard_row(row),
                        }
                        for row in batch_rows
                    ],
                )
                logger.info(
                    "%s 自动合并追投 materials=%s success=%s task_id=%s",
                    _tag,
                    len(material_ids),
                    result.success,
                    result.regulate_task_id,
                )
                await svc.close()
                return
            try:
                for row in hit_rows:
                    material_id = str(row.get("id") or "").strip()
                    material_name = _material_name_from_dashboard_row(row)
                    product_id = str(row.get("_product_id") or "").strip()
                    product_name = str(row.get("_product_name") or "").strip()
                    aavid_raw = target.get("aadvid")
                    aavid = str(aavid_raw).strip() if aavid_raw is not None else ""
                    ad_id = str(target.get("ad_id") or "").strip()

                    evaluation_row = (
                        row.get("_product_metrics")
                        if trigger_level == "product"
                        and isinstance(row.get("_product_metrics"), dict)
                        else row
                    )
                    eval_snap = build_trigger_evaluation_snapshot(trigger, evaluation_row)
                    trigger_snap = _json_dumps(
                        {
                            "strategy_id": st.get("id"),
                            "strategy_title": st.get("title"),
                            "target_uid": target_uid,
                            "promotion_scene": promotion_scene,
                            "plan_system": plan_system,
                            "trigger_level": trigger_level,
                            "product_id": product_id,
                            "product_name": product_name,
                            "trigger_config": trigger,
                            "evaluation": eval_snap,
                        }
                    )
                    query_snap = _json_dumps(
                        {
                            "query_period": period,
                            "period_label": period_label,
                            "query_at": query_at,
                            "dashboard_total": len(rows),
                            "material_row": row,
                            "target": {
                                "target_uid": target_uid,
                                "aavid": aavid,
                                "ad_id": ad_id,
                                "plan_name": target.get("plan_name"),
                                "promotion_scene": promotion_scene,
                                "plan_system": plan_system,
                            },
                        }
                    )

                    # 同一素材多策略并行时，必须串行化「限频判断 → 执行 → 记成功次数」，避免竞态导致超次数
                    mat_lock = await _lock_for_material_retouch(material_id, target_uid)
                    async with mat_lock:
                        if per_strategy_rl:
                            if rate_limit_strategy_should_skip(
                                db,
                                material_id,
                                strategy_id_for_rl,
                                ws_s,
                                mc_s,
                                target_uid,
                            ):
                                logger.info(
                                    "%s 分策略限频跳过 material_id=%s strategy_id=%s（%ss 内已达 %s 次）",
                                    _tag,
                                    material_id,
                                    strategy_id_for_rl,
                                    ws_s,
                                    mc_s,
                                )
                                continue
                        else:
                            if rate_limit_should_skip(
                                db,
                                material_id,
                                ws,
                                mc,
                                target_uid,
                            ):
                                logger.info(
                                    "%s 全局限频跳过 material_id=%s（%ss 窗口内已达上限 %s 次）",
                                    _tag,
                                    material_id,
                                    ws,
                                    mc,
                                )
                                continue

                        if not ad_id:
                            now = _beijing_now_str()
                            _insert_run(
                                db,
                                aavid=aavid or "",
                                ad_id="",
                                material_id=material_id,
                                target_uid=target_uid,
                                promotion_scene=promotion_scene,
                                plan_system=plan_system,
                                trigger_level=trigger_level,
                                product_id=product_id,
                                product_name=product_name,
                                material_name=material_name,
                                strategy_name=st_label,
                                regulate_task_id="",
                                started_at=now,
                                ended_at=now,
                                duration_ms=0,
                                status=-1,
                                step="resolve_ad_id",
                                message="监控计划中缺少 ad_id",
                                detail="",
                                retargeting=retargeting,
                                rule_full_json=rule_full_json,
                                trigger_snapshot_json=trigger_snap,
                                query_snapshot_json=query_snap,
                                headless=headless_cfg,
                                browser_headless_rule=browser_rule,
                            )
                            logger.warning(
                                "%s 无 ad_id aavid=%s material_id=%s",
                                _tag,
                                aavid,
                                material_id,
                            )
                            continue

                        try:
                            aavid_int = int(str(aavid_raw).strip())
                            ad_id_int = int(str(ad_id).strip())
                        except (TypeError, ValueError):
                            now = _beijing_now_str()
                            _insert_run(
                                db,
                                aavid=aavid or "",
                                ad_id=str(ad_id),
                                material_id=material_id,
                                target_uid=target_uid,
                                promotion_scene=promotion_scene,
                                plan_system=plan_system,
                                trigger_level=trigger_level,
                                product_id=product_id,
                                product_name=product_name,
                                material_name=material_name,
                                strategy_name=st_label,
                                regulate_task_id="",
                                started_at=now,
                                ended_at=now,
                                duration_ms=0,
                                status=-1,
                                step="validate",
                                message="aavid 或 ad_id 无法转为整数",
                                detail="",
                                retargeting=retargeting,
                                rule_full_json=rule_full_json,
                                trigger_snapshot_json=trigger_snap,
                                query_snapshot_json=query_snap,
                                headless=headless_cfg,
                                browser_headless_rule=browser_rule,
                            )
                            continue

                        if not auto_execute_allowed_in_current_environment():
                            logger.warning(
                                "%s 本地测试模式禁止自动追投；请改为“飞书确认后追投”完成受控验收 material_id=%s",
                                _tag,
                                material_id,
                            )
                            continue

                        started_at = _beijing_now_str()
                        t0 = time.time()
                        rate_recorded_under_lock = False
                        execution_uid = _auto_execution_uid(
                            target_uid=target_uid,
                            strategy_id=strategy_id_for_rl,
                            material_ids=[material_id],
                        )
                        try:
                            async with exclusive_browser_operation(
                                f"追投:{target_uid}:{material_id}",
                                priority=10,
                            ):
                                (
                                    locked_cfg,
                                    locked_strategy,
                                    locked_target,
                                ) = await asyncio.to_thread(
                                    _revalidate_auto_retarget_under_lock,
                                    db,
                                    original_strategy=st,
                                    target_uid=target_uid,
                                    aavid=aavid,
                                    ad_id=ad_id,
                                    promotion_scene=promotion_scene,
                                    plan_system=plan_system,
                                    material_id=material_id,
                                    product_id=product_id,
                                )
                                locked_retargeting = (
                                    locked_strategy.get("retargeting")
                                    if isinstance(
                                        locked_strategy.get("retargeting"),
                                        dict,
                                    )
                                    else {}
                                )
                                result = await svc.run(
                                    aavid=aavid_int,
                                    ad_id=ad_id_int,
                                    material_id=material_id,
                                    retargeting=locked_retargeting,
                                    strategy_title=st_label,
                                    execution_uid=execution_uid,
                                    reconciliation_task_uid=execution_uid,
                                    target_uid=target_uid,
                                    promotion_scene=promotion_scene,
                                    plan_system=plan_system,
                                    source_url=(
                                        locked_target.get("sanitized_page_url")
                                        or None
                                    ),
                                    reuse_session=False,
                                    close_session=False,
                                )
                                if result.success:
                                    record_retarget_rate_success_safely(
                                        db,
                                        material_ids=[material_id],
                                        target_uid=target_uid,
                                        cfg=locked_cfg,
                                        strategy=locked_strategy,
                                        retargeting=locked_retargeting,
                                    )
                                    rate_recorded_under_lock = True
                        except Exception:
                            ended_at = _beijing_now_str()
                            dur = int((time.time() - t0) * 1000)
                            _insert_run(
                                db,
                                aavid=str(aavid_int),
                                ad_id=str(ad_id_int),
                                material_id=material_id,
                                target_uid=target_uid,
                                promotion_scene=promotion_scene,
                                plan_system=plan_system,
                                trigger_level=trigger_level,
                                product_id=product_id,
                                product_name=product_name,
                                material_name=material_name,
                                strategy_name=st_label,
                                regulate_task_id="",
                                started_at=started_at,
                                ended_at=ended_at,
                                duration_ms=dur,
                                status=-1,
                                step="exception",
                                message="run 异常",
                                detail=traceback.format_exc()[:8000],
                                retargeting=retargeting,
                                rule_full_json=rule_full_json,
                                trigger_snapshot_json=trigger_snap,
                                query_snapshot_json=query_snap,
                                headless=headless_cfg,
                                browser_headless_rule=browser_rule,
                            )
                            logger.exception("%s run 异常 material_id=%s", _tag, material_id)
                            continue

                        ended_at = _beijing_now_str()
                        dur = int((time.time() - t0) * 1000)
                        st_ok = 1 if result.success else -1
                        detail = "" if result.success else (result.detail or "")
                        _insert_run(
                            db,
                            aavid=str(result.aavid or aavid_int),
                            ad_id=str(result.ad_id or ad_id_int),
                            material_id=str(result.material_id or material_id),
                            target_uid=target_uid,
                            promotion_scene=promotion_scene,
                            plan_system=plan_system,
                            trigger_level=trigger_level,
                            product_id=product_id,
                            product_name=product_name,
                            material_name=material_name,
                            strategy_name=st_label,
                            regulate_task_id=str(result.regulate_task_id or ""),
                            started_at=started_at,
                            ended_at=ended_at,
                            duration_ms=dur,
                            status=st_ok,
                            step=result.step,
                            message=result.message,
                            detail=detail,
                            retargeting=retargeting,
                            rule_full_json=rule_full_json,
                            trigger_snapshot_json=trigger_snap,
                            query_snapshot_json=query_snap,
                            headless=bool(result.headless),
                            browser_headless_rule=browser_rule,
                            cloud_task_id=execution_uid,
                        )
                        if result.success and not rate_recorded_under_lock:
                            if per_strategy_rl:
                                rate_limit_strategy_record_success(
                                    db,
                                    material_id,
                                    strategy_id_for_rl,
                                    ws_s,
                                    mc_s,
                                    target_uid,
                                )
                            # 全局表始终用根级 interval；record_success 内：若 limit_started_at+全局窗口已过期则重置窗口并记 1，否则 use_count+1
                            rate_limit_record_success(
                                db,
                                material_id,
                                ws,
                                mc,
                                target_uid,
                            )
                        logger.info(
                            "%s material_id=%s success=%s step=%s",
                            _tag,
                            material_id,
                            result.success,
                            result.step,
                        )
            finally:
                await svc.close()

    await asyncio.gather(*(process_strategy(st) for st in strategies))


async def main_loop(interval_sec: int = DEFAULT_INTERVAL_SEC) -> None:
    init_sqlite_schema()
    db = SQLiteStore()
    try:
        ensure_official_api_auto_execution_runtime(
            load_rule_retargeting_config()
        )
    except Exception:
        # 运行权限恢复失败时后续真实提交仍会由官方 API 写守卫拒绝；保留
        # 调度线程继续运行，方便用户修复配置后重新保存策略自愈。
        logger.exception(
            "%s 启动时恢复自动追投运行权限失败",
            retarget_log_tag(scheduler=True),
        )
    logger.info(
        "%s 启动，间隔 %ss，版本 %s",
        retarget_log_tag(scheduler=True),
        interval_sec,
        CURRENT_VERSION,
    )
    scope = None
    next_full_scan = time.monotonic()
    while not _RUNNER_STOP.is_set():
        if time.monotonic() >= next_full_scan:
            _TARGET_WAKE.take(full_scan=True)
            scope = None
            next_full_scan = time.monotonic() + max(1, int(interval_sec))
        try:
            await run_one_cycle(db, target_uids=scope)
        except Exception:
            logger.exception("%s 本轮未捕获异常", retarget_log_tag(scheduler=True))
        while not _RUNNER_STOP.is_set() and time.monotonic() < next_full_scan:
            woke = await asyncio.to_thread(
                _wait_for_next_rule_cycle, max(0.01, next_full_scan - time.monotonic()),
            )
            if woke:
                await asyncio.to_thread(_RUNNER_STOP.wait, _TARGET_WAKE.remaining())
                ready, scope = _TARGET_WAKE.take()
                if ready:
                    logger.info("%s 收到即时复核请求 scope=%s", retarget_log_tag(scheduler=True),
                                "all" if scope is None else len(scope))
                    break


def _gui_background_target() -> None:
    """GUI 内嵌：在独立线程中跑 asyncio 事件循环（与命令行 `main()` 等价）。"""
    try:
        asyncio.run(main_loop())
    except Exception:
        logger.exception("%s 后台线程异常退出", retarget_log_tag(scheduler=True))


def start_retargeting_rule_runner_background_thread() -> threading.Thread:
    """
    供 gui_app 等调用：启动守护线程，逻辑与 `python -m services.retargeting_rule_runner` 一致。
    `rule_retargeting.json` 未启用 enabled 时每轮仅快速跳过。
    """
    global _RUNNER_THREAD
    with _RUNNER_THREAD_LOCK:
        if _RUNNER_THREAD is not None and _RUNNER_THREAD.is_alive():
            return _RUNNER_THREAD
        _RUNNER_STOP.clear()
        _RUNNER_THREAD = threading.Thread(
            target=_gui_background_target,
            name="retargeting-rule-runner",
            daemon=True,
        )
        _RUNNER_THREAD.start()
    logger.info(
        "%s 后台线程已启动（GUI，间隔 %ss，可用 RETARGET_RULE_INTERVAL_SEC 覆盖）",
        retarget_log_tag(scheduler=True),
        DEFAULT_INTERVAL_SEC,
    )
    return _RUNNER_THREAD


def stop_retargeting_rule_runner_background_thread(timeout: float = 8.0) -> None:
    """Stop taking new rule work and wait for the current short cycle to finish."""
    global _RUNNER_THREAD
    _RUNNER_STOP.set()
    request_retargeting_rule_evaluation("runtime_shutdown")
    with _RUNNER_THREAD_LOCK:
        thread = _RUNNER_THREAD
    if thread and thread.is_alive() and thread is not threading.current_thread():
        thread.join(timeout=max(0.1, float(timeout)))
    if thread is None or not thread.is_alive():
        with _RUNNER_THREAD_LOCK:
            if _RUNNER_THREAD is thread:
                _RUNNER_THREAD = None
