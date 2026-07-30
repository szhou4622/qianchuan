# -*- coding: utf-8 -*-
"""单个千川账户的统一操作流水。"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

from services.plan_system import normalize_plan_system
from utils.sqlite_store import SQLiteStore, init_sqlite_schema


TABLE = "account_operation_event"
ALLOWED_SOURCES = {"tool_direct", "browser_observed", "platform_log"}
ALLOWED_ACTIONS = {
    "retarget",
    "stop",
    "plan_create",
    "plan_copy",
    "plan_enable",
    "plan_disable",
    "plan_delete",
    "budget_update",
    "bid_update",
    "roi_update",
    "other",
}
ALLOWED_STATUSES = {"requested", "executing", "success", "failed", "unknown"}

ACTION_LABELS = {
    "retarget": "追投",
    "stop": "停投",
    "plan_create": "新建计划",
    "plan_copy": "复制计划",
    "plan_enable": "启用计划",
    "plan_disable": "暂停计划",
    "plan_delete": "删除计划",
    "budget_update": "修改预算",
    "bid_update": "修改出价",
    "roi_update": "修改ROI",
    "other": "其他",
}


def _json(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _json_field(data: Dict[str, Any], primary: str, fallback: str) -> str:
    value = data.get(primary)
    if value in (None, ""):
        value = data.get(fallback)
    return _json(value)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _first(row: Dict[str, Any], names: Iterable[str], default: Any = "") -> Any:
    for name in names:
        value = row.get(name)
        if value is not None and str(value).strip() != "":
            return value
    return default


def _nested_value(value: Any, names: Iterable[str]) -> str:
    wanted = {str(name).lower() for name in names}
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in wanted and item not in (None, ""):
                return str(item)
        for item in value.values():
            found = _nested_value(item, wanted)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _nested_value(item, wanted)
            if found:
                return found
    elif isinstance(value, str):
        try:
            decoded = json.loads(value)
        except Exception:
            return ""
        return _nested_value(decoded, wanted)
    return ""


def normalize_action_type(text: Any) -> str:
    s = str(text or "").strip().lower()
    if not s:
        return "other"
    rules = (
        (
            "stop",
            (
                "停投",
                "停止调控",
                "终止调控",
                "结束调控",
                "调控手动关闭",
                "手动关停",
                "手动关闭",
            ),
        ),
        ("retarget", ("追投", "追加投放", "assist task", "assist_task")),
        ("plan_copy", ("复制计划", "拷贝计划", "copy plan", "copy campaign")),
        ("plan_create", ("新建计划", "创建计划", "新增计划", "create plan", "create campaign")),
        ("plan_delete", ("删除计划", "delete plan", "delete campaign")),
        ("plan_disable", ("暂停计划", "关闭计划", "停用计划", "disable plan", "pause plan")),
        ("plan_enable", ("启用计划", "开启计划", "启动计划", "enable plan", "start plan")),
        ("budget_update", ("修改预算", "调整预算", "日预算", "update budget", "modify budget")),
        ("bid_update", ("修改出价", "调整出价", "我的出价", "update bid", "modify bid")),
        ("roi_update", ("修改roi", "调整roi", "roi目标", "roi 目标", "update roi", "modify roi")),
    )
    for action, needles in rules:
        if any(x in s for x in needles):
            return action
    return "other"


def make_event_uid(source: str, *parts: Any) -> str:
    raw = "|".join([source] + [str(x or "").strip() for x in parts])
    return f"{source}:{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:32]}"


def upsert_operation_event(data: Dict[str, Any], db: Optional[SQLiteStore] = None) -> str:
    store = db or SQLiteStore()
    init_sqlite_schema(database=store.config["database"])
    source = str(data.get("source") or "tool_direct").strip()
    if source not in ALLOWED_SOURCES:
        source = "tool_direct"
    action = str(data.get("action_type") or "other").strip()
    if action not in ALLOWED_ACTIONS:
        action = "other"
    status = str(data.get("status") or "unknown").strip()
    if status not in ALLOWED_STATUSES:
        status = "unknown"
    uid = str(data.get("event_uid") or "").strip() or f"local:{uuid.uuid4()}"
    occurred_at = str(data.get("occurred_at") or "").strip() or _now()
    object_type = str(data.get("object_type") or "").strip()
    object_id = str(data.get("object_id") or "").strip()
    object_name = str(data.get("object_name") or "").strip()
    plan_id = str(data.get("plan_id") or "").strip()
    plan_name = str(data.get("plan_name") or "").strip()
    material_id = str(data.get("material_id") or "").strip()
    material_name = str(data.get("material_name") or "").strip()
    regulate_task_id = str(data.get("regulate_task_id") or "").strip()
    regulate_task_name = str(data.get("regulate_task_name") or "").strip()
    if not plan_id and (object_type == "plan" or action.startswith("plan_") or action in {"budget_update", "bid_update", "roi_update"}):
        plan_id, plan_name = object_id, plan_name or object_name
    if not material_id and (object_type == "material" or action == "retarget"):
        material_id, material_name = object_id, material_name or object_name
    if not regulate_task_id and (object_type == "assist_task" or action == "stop"):
        regulate_task_id, regulate_task_name = object_id, regulate_task_name or object_name
    if not regulate_task_id:
        regulate_task_id = _nested_value(data.get("after") or data.get("after_json"), ("regulate_task_id", "assist_task_id"))
    target_uid = str(data.get("target_uid") or "legacy_unscoped").strip()
    try:
        plan_system = normalize_plan_system(data.get("plan_system") or "unknown")
    except ValueError:
        plan_system = "unknown"
    if plan_system == "unknown" and target_uid not in ("", "legacy_unscoped"):
        target = store.select_one(
            "promotion_target",
            fields="plan_system",
            where={"target_uid": target_uid},
        )
        try:
            plan_system = normalize_plan_system(
                (target or {}).get("plan_system") or "unknown"
            )
        except ValueError:
            plan_system = "unknown"
    aavid = str(data.get("aavid") or data.get("aadvid") or "").strip()
    account_uid = str(data.get("account_uid") or "").strip()
    if aavid and not account_uid:
        from services.qianchuan_accounts import ensure_qianchuan_account

        account_uid = ensure_qianchuan_account(
            aavid,
            directory_selected=False,
            db=store,
        )["account_uid"]
    elif aavid and account_uid:
        account = store.select_one(
            "qianchuan_account",
            fields="account_uid",
            where={"account_uid": account_uid, "aavid": aavid},
        )
        if not account:
            raise ValueError("操作流水的 account_uid 与 aavid 不匹配")
    existing_uid = store.select_one(
        TABLE,
        fields="account_uid",
        where={"event_uid": uid},
    )
    if (
        existing_uid
        and str(existing_uid.get("account_uid") or "")
        not in {"", account_uid}
    ):
        uid = make_event_uid(source, account_uid, uid)
    row = {
        "event_uid": uid,
        "aavid": aavid,
        "account_uid": account_uid,
        "ad_id": str(data.get("ad_id") or "").strip(),
        "target_uid": target_uid,
        "promotion_scene": str(data.get("promotion_scene") or "").strip(),
        "plan_system": plan_system,
        "source": source,
        "action_type": action,
        "object_type": object_type,
        "object_id": object_id,
        "object_name": object_name,
        "plan_id": plan_id,
        "plan_name": plan_name,
        "material_id": material_id,
        "material_name": material_name,
        "product_id": str(data.get("product_id") or "").strip(),
        "product_name": str(data.get("product_name") or "").strip(),
        "regulate_task_id": regulate_task_id,
        "regulate_task_name": regulate_task_name,
        "operator_id": str(data.get("operator_id") or "").strip(),
        "operator_name": str(data.get("operator_name") or "").strip(),
        "status": status,
        "summary": str(data.get("summary") or ACTION_LABELS[action]).strip(),
        "detail": str(data.get("detail") or "").strip(),
        "before_json": _json_field(data, "before_json", "before"),
        "after_json": _json_field(data, "after_json", "after"),
        "trigger_json": _json_field(data, "trigger_json", "trigger"),
        "request_json": _json_field(data, "request_json", "request"),
        "response_json": _json_field(data, "response_json", "response"),
        "raw_json": _json_field(data, "raw_json", "raw"),
        "cloud_task_id": str(data.get("cloud_task_id") or "").strip(),
        "platform_event_id": str(data.get("platform_event_id") or "").strip(),
        "related_event_uid": str(data.get("related_event_uid") or "").strip(),
        "possible_duplicate": 1 if data.get("possible_duplicate") else 0,
        "occurred_at": occurred_at,
    }
    if not row["aavid"]:
        raise ValueError("操作流水缺少 aavid")
    store.insert_or_update(TABLE, row, unique_fields=["event_uid"])
    return uid


def migrate_legacy_operation_runs(db: Optional[SQLiteStore] = None) -> int:
    """幂等导入旧追投/停投流水；旧表继续保留。"""
    store = db or SQLiteStore()
    init_sqlite_schema(database=store.config["database"])
    count = 0
    retarget_rows = store.execute(
        "SELECT r.* FROM pmc_retargeting_run r LEFT JOIN account_operation_event e "
        "ON e.event_uid=('retarget_run:' || r.id) WHERE e.id IS NULL "
        "OR (COALESCE(r.account_uid,'')<>'' AND "
        "COALESCE(e.account_uid,'')<>r.account_uid)",
        fetch=True,
    ) or []
    for row in retarget_rows:
        if not str(row.get("aavid") or "").strip():
            continue
        uid = f"retarget_run:{row['id']}"
        upsert_operation_event(
            {
                "event_uid": uid,
                "aavid": row.get("aavid"),
                "account_uid": row.get("account_uid"),
                "ad_id": row.get("ad_id"),
                "target_uid": row.get("target_uid") or "legacy_unscoped",
                "promotion_scene": row.get("promotion_scene") or "live",
                "plan_system": row.get("plan_system") or "unknown",
                "source": "tool_direct",
                "action_type": "retarget",
                "object_type": "material",
                "object_id": row.get("material_id"),
                "object_name": row.get("material_name"),
                "material_id": row.get("material_id"),
                "material_name": row.get("material_name"),
                "product_id": row.get("product_id"),
                "product_name": row.get("product_name"),
                "regulate_task_id": row.get("regulate_task_id"),
                "status": "success" if int(row.get("status") or -1) == 1 else "failed",
                "summary": row.get("message") or "追投",
                "detail": row.get("detail"),
                "after": {"regulate_task_id": row.get("regulate_task_id")},
                "trigger_json": row.get("trigger_snapshot_json"),
                "request_json": row.get("retargeting_json"),
                "response_json": {"step": row.get("step")},
                "occurred_at": row.get("ended_at") or row.get("started_at"),
            },
            store,
        )
        count += 1
    stop_rows = store.execute(
        "SELECT r.* FROM pmc_regulation_run r LEFT JOIN account_operation_event e "
        "ON e.event_uid=('regulation_run:' || r.id) WHERE e.id IS NULL "
        "OR (COALESCE(r.account_uid,'')<>'' AND "
        "COALESCE(e.account_uid,'')<>r.account_uid)",
        fetch=True,
    ) or []
    for row in stop_rows:
        if not str(row.get("aavid") or "").strip():
            continue
        uid = f"regulation_run:{row['id']}"
        st = int(row.get("status") or -1)
        upsert_operation_event(
            {
                "event_uid": uid,
                "aavid": row.get("aavid"),
                "account_uid": row.get("account_uid"),
                "ad_id": row.get("ad_id"),
                "target_uid": row.get("target_uid") or "legacy_unscoped",
                "promotion_scene": row.get("promotion_scene") or "live",
                "plan_system": row.get("plan_system") or "unknown",
                "source": "tool_direct",
                "action_type": "stop",
                "object_type": "assist_task",
                "object_id": row.get("assist_task_id"),
                "object_name": row.get("task_name"),
                "regulate_task_id": row.get("assist_task_id"),
                "regulate_task_name": row.get("task_name"),
                "status": "success" if st in (1, 2) else "failed",
                "summary": row.get("message") or "停投",
                "detail": row.get("detail"),
                "request": {"stop_action": row.get("stop_action")},
                "trigger_json": row.get("trigger_snapshot_json"),
                "response": {"step": row.get("step")},
                "occurred_at": row.get("ended_at") or row.get("started_at"),
            },
            store,
        )
        count += 1
    return count


def _date_bound(value: Any, end: bool = False) -> Optional[str]:
    s = str(value or "").strip()
    if not s:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return s + (" 23:59:59" if end else " 00:00:00")
    return s.replace("T", " ")[:19]


def query_operation_events_page(
    *,
    aavid: Any,
    date_from: Any = None,
    date_to: Any = None,
    action_type: Any = None,
    source: Any = None,
    status: Any = None,
    operator: Any = None,
    q: Any = None,
    target_uid: Any = None,
    page: Any = 1,
    page_size: Any = 50,
) -> Tuple[int, List[Dict[str, Any]]]:
    init_sqlite_schema()
    store = SQLiteStore()
    migrate_legacy_operation_runs(store)
    aid = str(aavid or "").strip()
    if not aid:
        return 0, []
    from services.qianchuan_accounts import get_qianchuan_account

    account = get_qianchuan_account(aid, db=store)
    if not account:
        return 0, []
    try:
        p = max(1, int(page))
    except Exception:
        p = 1
    try:
        ps = max(1, min(5000, int(page_size)))
    except Exception:
        ps = 50
    where = ["aavid = ?", "account_uid = ?"]
    params: List[Any] = [aid, str(account.get("account_uid") or "")]
    target = str(target_uid or "").strip()
    if target:
        where.append("target_uid = ?")
        params.append(target)
    f = _date_bound(date_from)
    t = _date_bound(date_to, end=True)
    if f:
        where.append("occurred_at >= ?")
        params.append(f)
    if t:
        where.append("occurred_at <= ?")
        params.append(t)
    if action_type and str(action_type) in ALLOWED_ACTIONS:
        where.append("action_type = ?")
        params.append(str(action_type))
    if source and str(source) in ALLOWED_SOURCES:
        where.append("source = ?")
        params.append(str(source))
    if status and str(status) in ALLOWED_STATUSES:
        where.append("status = ?")
        params.append(str(status))
    op = str(operator or "").strip()
    if op:
        where.append("(operator_name LIKE ? OR operator_id LIKE ?)")
        params.extend([f"%{op}%", f"%{op}%"])
    keyword = str(q or "").strip()
    if keyword:
        like = f"%{keyword}%"
        where.append(
            "(summary LIKE ? OR detail LIKE ? OR object_id LIKE ? OR object_name LIKE ? "
            "OR plan_id LIKE ? OR plan_name LIKE ? OR material_id LIKE ? OR material_name LIKE ? "
            "OR regulate_task_id LIKE ? OR regulate_task_name LIKE ? OR platform_event_id LIKE ? OR cloud_task_id LIKE ?)"
        )
        params.extend([like] * 12)
    clause = " AND ".join(where)
    total_rows = store.execute(
        f"SELECT COUNT(*) AS n FROM {TABLE} WHERE {clause}", tuple(params), fetch=True
    ) or []
    total = int(total_rows[0]["n"]) if total_rows else 0
    fields = (
        "id,event_uid,aavid,ad_id,target_uid,promotion_scene,plan_system,source,action_type,object_type,object_id,object_name,"
        "plan_id,plan_name,material_id,material_name,product_id,product_name,regulate_task_id,regulate_task_name,"
        "operator_id,operator_name,status,summary,cloud_task_id,platform_event_id,"
        "related_event_uid,possible_duplicate,occurred_at,created_at"
    )
    rows = store.execute(
        f"SELECT {fields} FROM {TABLE} WHERE {clause} "
        "ORDER BY occurred_at DESC, id DESC LIMIT ? OFFSET ?",
        tuple(params + [ps, (p - 1) * ps]),
        fetch=True,
    ) or []
    for row in rows:
        row["action_label"] = ACTION_LABELS.get(row.get("action_type"), "其他")
    return total, rows


def get_operation_event(event_id: Any, aavid: Any = None) -> Optional[Dict[str, Any]]:
    init_sqlite_schema()
    store = SQLiteStore()
    try:
        rid = int(event_id)
    except Exception:
        return None
    where = {"id": rid}
    aid = str(aavid or "").strip()
    if not aid:
        return None
    where["aavid"] = aid
    from services.qianchuan_accounts import get_qianchuan_account

    account = get_qianchuan_account(aid, db=store)
    if not account:
        return None
    where["account_uid"] = str(account.get("account_uid") or "")
    return store.select_one(TABLE, where=where)


def list_operation_accounts(
    db: Optional[SQLiteStore] = None,
) -> List[Dict[str, str]]:
    store = db or SQLiteStore()
    init_sqlite_schema(database=store.config["database"])
    migrate_legacy_operation_runs(store)
    from services.qianchuan_accounts import list_qianchuan_accounts

    accounts: List[Dict[str, str]] = []
    for item in list_qianchuan_accounts(db=store):
        aavid = str(item.get("aavid") or "").strip()
        if not aavid:
            continue
        accounts.append(
            {
                "aavid": aavid,
                "account_uid": str(item.get("account_uid") or "").strip(),
                "account_name": str(
                    item.get("account_name") or f"千川账户 {aavid}"
                ).strip(),
            }
        )
    return accounts


def operation_sync_state(aavid: Any) -> Dict[str, Any]:
    init_sqlite_schema()
    store = SQLiteStore()
    aid = str(aavid or "").strip()
    row = None
    if aid:
        from services.qianchuan_accounts import get_qianchuan_account

        account = get_qianchuan_account(aid, db=store)
        if account:
            row = store.select_one(
                "platform_log_sync_state",
                where={
                    "aavid": aid,
                    "account_uid": str(account.get("account_uid") or ""),
                },
            )
    return row or {
        "aavid": aid,
        "coverage_from": "",
        "coverage_to": "",
        "last_sync_at": "",
        "last_status": "not_configured",
        "last_error": "尚未发现千川后台操作日志接口；当前流水可能不完整",
    }


def update_platform_sync_state(
    aavid: Any,
    *,
    owner_username: Any = None,
    db: Optional[SQLiteStore] = None,
    **values: Any,
) -> None:
    store = db or SQLiteStore()
    init_sqlite_schema(database=store.config["database"])
    aid = str(aavid or "").strip()
    if not aid:
        return
    allowed = {
        "coverage_from", "coverage_to", "last_sync_at", "last_status", "last_error",
        "discovered_page_url", "discovered_api_url", "discovered_request_json",
    }
    from services.qianchuan_accounts import ensure_qianchuan_account

    data = {
        "aavid": aid,
        "account_uid": ensure_qianchuan_account(
            aid,
            owner_username=owner_username,
            directory_selected=False,
            db=store,
        )["account_uid"],
    }
    existing = store.select_one(
        "platform_log_sync_state",
        where={
            "account_uid": data["account_uid"],
            "aavid": aid,
        },
    ) or {}
    for key, value in values.items():
        if key in allowed:
            data[key] = _json(value) if key == "discovered_request_json" else str(value or "")
    if data.get("coverage_from"):
        data["coverage_from"] = min(
            value
            for value in (
                str(existing.get("coverage_from") or ""),
                str(data["coverage_from"]),
            )
            if value
        )
    if data.get("coverage_to"):
        data["coverage_to"] = max(
            value
            for value in (
                str(existing.get("coverage_to") or ""),
                str(data["coverage_to"]),
            )
            if value
        )
    store.insert_or_update(
        "platform_log_sync_state",
        data,
        unique_fields=["account_uid", "aavid"],
    )


def export_operation_events_csv(**filters: Any) -> str:
    filters = dict(filters)
    filters["page"] = 1
    filters["page_size"] = 5000
    total, rows = query_operation_events_page(**filters)
    page = 2
    while len(rows) < total:
        filters["page"] = page
        _, chunk = query_operation_events_page(**filters)
        if not chunk:
            break
        rows.extend(chunk)
        page += 1
    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(
        ["账户ID", "监控目标", "推广场景", "计划体系", "操作时间", "操作", "对象类型", "对象ID", "对象名称", "计划ID", "计划名称",
         "商品ID", "商品名称", "素材ID", "素材名称", "调控任务ID", "调控任务名称", "操作人", "来源", "结果", "摘要", "云端任务ID"]
    )
    for row in rows:
        writer.writerow(
            [
                row.get("aavid", ""),
                row.get("target_uid", ""),
                "推商品" if row.get("promotion_scene") == "product" else ("推直播" if row.get("promotion_scene") == "live" else row.get("promotion_scene", "")),
                {"global": "全域", "chengfang": "千川乘方", "unknown": "待确认"}.get(row.get("plan_system"), "待确认"),
                row.get("occurred_at", ""),
                row.get("action_label", ""),
                row.get("object_type", ""),
                row.get("object_id", ""),
                row.get("object_name", ""),
                row.get("plan_id", ""),
                row.get("plan_name", ""),
                row.get("product_id", ""),
                row.get("product_name", ""),
                row.get("material_id", ""),
                row.get("material_name", ""),
                row.get("regulate_task_id", ""),
                row.get("regulate_task_name", ""),
                row.get("operator_name") or row.get("operator_id") or "",
                row.get("source", ""),
                row.get("status", ""),
                row.get("summary", ""),
                row.get("cloud_task_id", ""),
            ]
        )
    return "\ufeff" + output.getvalue()


def prune_operation_events(retention_days: int = 180) -> int:
    init_sqlite_schema()
    days = max(1, int(retention_days))
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    return SQLiteStore().execute(
        "DELETE FROM account_operation_event WHERE occurred_at < ?", (cutoff,)
    ) or 0


def _normalize_occurred_at(value: Any) -> str:
    if isinstance(value, (int, float)) or re.fullmatch(r"\d{10,13}", str(value or "").strip()):
        try:
            stamp = float(value)
            if stamp > 10_000_000_000:
                stamp /= 1000
            return datetime.fromtimestamp(stamp).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return _now()
    text = str(value or "").strip().replace("T", " ")
    return text[:19] if text else _now()


def ingest_platform_log_rows(
    aavid: Any,
    rows: Iterable[Dict[str, Any]],
    *,
    owner_username: Any = None,
    db: Optional[SQLiteStore] = None,
) -> int:
    """接收记录浏览器发现的平台日志行，保留原文并做基础字段兼容。"""
    init_sqlite_schema()
    aid = str(aavid or "").strip()
    if not aid:
        raise ValueError("缺少 aavid")
    store = db or SQLiteStore()
    from services.qianchuan_accounts import ensure_qianchuan_account

    account_uid = str(
        ensure_qianchuan_account(
            aid,
            owner_username=owner_username,
            directory_selected=False,
            db=store,
        ).get("account_uid")
        or ""
    )
    inserted = 0
    seen_times: List[str] = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        row_account = str(_first(raw, ("aavid", "aadvid", "advertiser_id"))).strip()
        if row_account and row_account != aid:
            continue
        description = _first(raw, ("operation", "operation_name", "action_name", "description", "内容", "操作"))
        platform_id = str(_first(raw, ("id", "log_id", "record_id", "operation_id"))).strip()
        occurred_at = _normalize_occurred_at(
            _first(raw, ("operate_time", "operation_time", "created_at", "create_time", "time", "操作时间", "时间"), _now())
        )
        operator_id = _first(raw, ("operator_id", "user_id", "creator_id", "operate_user_id"))
        operator_name = _first(raw, ("operator_name", "user_name", "creator_name", "operate_user_name", "操作人"))
        object_id = _first(raw, ("object_id", "plan_id", "campaign_id", "ad_id", "material_id", "对象ID", "计划ID", "素材ID"))
        object_name = _first(raw, ("object_name", "plan_name", "campaign_name", "ad_name", "material_name", "对象名称", "计划名称", "素材名称"))
        raw_status = str(_first(raw, ("status", "result", "operation_result", "结果"))).strip().lower()
        event_status = "failed" if any(x in raw_status for x in ("fail", "失败", "错误")) else ("success" if raw_status == "" or any(x in raw_status for x in ("success", "成功")) else "unknown")
        action = normalize_action_type(description)
        legacy_uid = (
            f"platform_log:{aid}:{platform_id}"
            if platform_id
            else make_event_uid("platform_log", aid, occurred_at, operator_id, description, object_id)
        )
        legacy_event = store.select_one(
            TABLE,
            fields="account_uid",
            where={"event_uid": legacy_uid},
        )
        if (
            legacy_event
            and str(legacy_event.get("account_uid") or "")
            not in {"", account_uid}
        ):
            uid = (
                f"platform_log:{account_uid}:{aid}:{platform_id}"
                if platform_id
                else make_event_uid(
                    "platform_log",
                    account_uid,
                    aid,
                    occurred_at,
                    operator_id,
                    description,
                    object_id,
                )
            )
        else:
            # 保留既有单账号UID，避免升级后同一日志形成重复记录。
            uid = legacy_uid
        possible = False
        related = ""
        candidates = store.execute(
            "SELECT event_uid,operator_id,operator_name,platform_event_id,raw_json "
            "FROM account_operation_event WHERE account_uid=? AND aavid=? AND source<>? "
            "AND action_type=? AND object_id=? AND ABS(strftime('%s',occurred_at)-strftime('%s',?)) <= 120 LIMIT 2",
            (
                account_uid,
                aid,
                "platform_log",
                action,
                str(object_id or ""),
                occurred_at,
            ),
            fetch=True,
        ) or []
        if object_id not in (None, "") and action != "other" and len(candidates) == 1:
            matched = candidates[0]
            related = str(matched["event_uid"])
            previous_raw: Any = matched.get("raw_json") or ""
            if previous_raw:
                try:
                    previous_raw = json.loads(str(previous_raw))
                except Exception:
                    previous_raw = str(previous_raw)
            evidence: Dict[str, Any] = {"platform_log": raw}
            if previous_raw:
                evidence["captured_event"] = previous_raw
            store.update(
                TABLE,
                {
                    "platform_event_id": platform_id or matched.get("platform_event_id") or "",
                    "operator_id": matched.get("operator_id") or operator_id or "",
                    "operator_name": matched.get("operator_name") or operator_name or "",
                    "related_event_uid": uid,
                    "possible_duplicate": 0,
                    "raw_json": _json(evidence),
                },
                where={"event_uid": related},
            )
            seen_times.append(occurred_at)
            inserted += 1
            continue
        elif candidates:
            possible = True
            for candidate in candidates:
                store.update(
                    TABLE,
                    {"possible_duplicate": 1, "related_event_uid": uid},
                    where={"event_uid": str(candidate["event_uid"])},
                )
        object_type = str(_first(raw, ("object_type", "target_type", "resource_type")) or "").strip()
        if not object_type:
            object_type = (
                "material"
                if action == "retarget"
                else ("assist_task" if action == "stop" else ("plan" if action != "other" else ""))
            )
        plan_id = _first(raw, ("plan_id", "campaign_id"))
        plan_name = _first(raw, ("plan_name", "campaign_name"))
        material_id = _first(raw, ("material_id",))
        material_name = _first(raw, ("material_name",))
        regulate_task_id = _first(raw, ("regulate_task_id", "assist_task_id", "task_id"))
        regulate_task_name = _first(raw, ("regulate_task_name", "assist_task_name", "task_name"))
        upsert_operation_event(
            {
                "event_uid": uid,
                "aavid": aid,
                "account_uid": account_uid,
                "ad_id": _first(raw, ("ad_id", "advertiser_id")),
                "target_uid": _first(raw, ("target_uid",), "legacy_unscoped"),
                "promotion_scene": _first(raw, ("promotion_scene", "scene")),
                "plan_system": _first(raw, ("plan_system", "delivery_system")),
                "source": "platform_log",
                "action_type": action,
                "object_type": object_type,
                "object_id": object_id,
                "object_name": object_name,
                "plan_id": plan_id,
                "plan_name": plan_name,
                "material_id": material_id,
                "material_name": material_name,
                "product_id": _first(raw, ("product_id", "commodity_id")),
                "product_name": _first(raw, ("product_name", "commodity_name")),
                "regulate_task_id": regulate_task_id,
                "regulate_task_name": regulate_task_name,
                "operator_id": operator_id,
                "operator_name": operator_name,
                "status": event_status,
                "summary": description or ACTION_LABELS[action],
                "before": _first(raw, ("before", "before_value", "old_value", "origin_value")),
                "after": _first(raw, ("after", "after_value", "new_value", "target_value")),
                "raw": raw,
                "platform_event_id": platform_id,
                "related_event_uid": related,
                "possible_duplicate": possible,
                "occurred_at": occurred_at,
            },
            store,
        )
        seen_times.append(occurred_at)
        inserted += 1
    state = {
        "aavid": aid,
        "account_uid": account_uid,
        "last_sync_at": _now(),
        "last_status": "ok",
        "last_error": "",
    }
    if seen_times:
        existing = store.select_one(
            "platform_log_sync_state",
            where={"account_uid": account_uid, "aavid": aid},
        ) or {}
        old_from = str(existing.get("coverage_from") or "")
        old_to = str(existing.get("coverage_to") or "")
        state["coverage_from"] = min([x for x in [old_from, min(seen_times)] if x])
        state["coverage_to"] = max([x for x in [old_to, max(seen_times)] if x])
    store.insert_or_update(
        "platform_log_sync_state",
        state,
        unique_fields=["account_uid", "aavid"],
    )
    return inserted
