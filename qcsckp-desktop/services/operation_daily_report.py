# -*- coding: utf-8 -*-
"""按千川账户生成并通过本地飞书长连接发送前一日操作日报。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import uuid
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from config import DATA_DIR, DB_FILE
from services.local_feishu_bridge import (
    current_local_feishu_account,
    get_local_feishu_status,
    list_local_feishu_bound_targets,
    send_local_feishu_bound_card,
)
from utils.log import logger
from utils.sqlite_store import SQLiteStore, init_sqlite_schema


CONFIG_FILE = os.path.join(DATA_DIR, "operation_daily_report.json")
CONFIG_LOCK = threading.RLock()
SCHEDULER_LOCK = threading.Lock()
SCHEDULER_STARTED = False
DETAIL_LIMIT = 20

DEFAULT_CONFIG = {
    "enabled": False,
    "send_time": "09:00",
    "aavids": [],
    "send_empty": False,
}

SOURCE_LABELS = {
    "tool_direct": "工具操作",
    "browser_observed": "浏览器记录",
    "platform_log": "千川后台日志",
}
STATUS_LABELS = {
    "requested": "已请求",
    "executing": "执行中",
    "success": "成功",
    "failed": "失败",
    "unknown": "结果未知",
}
ACTION_ORDER = [
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
]
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


def _account_key(value: Any) -> str:
    return str(value or "").strip().casefold()


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _normalize_send_time(value: Any) -> str:
    text = str(value or "").strip()
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", text):
        raise ValueError("发送时间必须是00:00到23:59之间的时间")
    return text


def _normalize_aavids(values: Any) -> List[str]:
    result: List[str] = []
    if not isinstance(values, list):
        return result
    for value in values:
        item = str(value or "").strip()
        if item and item not in result:
            result.append(item)
    return result[:100]


def _load_config_file() -> Dict[str, Any]:
    with CONFIG_LOCK:
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict) and isinstance(data.get("profiles"), dict):
                return data
        except Exception:
            pass
        return {"version": 1, "profiles": {}}


def _save_config_file(data: Dict[str, Any]) -> None:
    with CONFIG_LOCK:
        os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
        temp_path = CONFIG_FILE + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
        os.replace(temp_path, CONFIG_FILE)


def _profile_config(account_username: str) -> Dict[str, Any]:
    account = _account_key(account_username)
    raw = (_load_config_file().get("profiles") or {}).get(account)
    config = dict(DEFAULT_CONFIG)
    if isinstance(raw, dict):
        config.update(raw)
    try:
        config["send_time"] = _normalize_send_time(config.get("send_time"))
    except ValueError:
        config["send_time"] = DEFAULT_CONFIG["send_time"]
    config["enabled"] = bool(config.get("enabled"))
    config["send_empty"] = bool(config.get("send_empty", False))
    config["aavids"] = _normalize_aavids(config.get("aavids"))
    return config


def _save_profile_config(account_username: str, config: Dict[str, Any]) -> Dict[str, Any]:
    account = _account_key(account_username)
    if not account:
        raise RuntimeError("请先登录工具账号")
    selected_aavids = _normalize_aavids(config.get("aavids"))
    if bool(config.get("enabled")) and not selected_aavids:
        raise ValueError("开启自动日报前，请至少选择一个千川账户")
    normalized = {
        "enabled": bool(config.get("enabled")),
        "send_time": _normalize_send_time(config.get("send_time") or "09:00"),
        "aavids": selected_aavids,
        "send_empty": bool(config.get("send_empty", False)),
        "updated_at": _now_text(),
    }
    with CONFIG_LOCK:
        data = _load_config_file()
        data.setdefault("profiles", {})[account] = normalized
        _save_config_file(data)
    return dict(normalized)


def _store(database: Optional[str] = None) -> SQLiteStore:
    path = database or DB_FILE
    init_sqlite_schema(database=path)
    return SQLiteStore(database=path)


def list_operation_account_options(database: Optional[str] = None) -> List[Dict[str, str]]:
    store = _store(database)
    from services.qianchuan_accounts import (
        list_qianchuan_accounts,
        migrate_existing_qianchuan_accounts,
    )

    owner = current_local_feishu_account() or None
    migrate_existing_qianchuan_accounts(owner_username=owner, db=store)
    accounts = list_qianchuan_accounts(owner_username=owner, db=store)
    if accounts:
        return [
            {
                "account_uid": str(item.get("account_uid") or ""),
                "aavid": str(item.get("aavid") or ""),
                "account_name": str(item.get("account_name") or ""),
                "report_enabled": bool(item.get("report_enabled")),
            }
            for item in accounts
        ]
    rows = store.execute(
        "SELECT aavid FROM account_operation_event WHERE aavid<>'' "
        "UNION SELECT aadvid AS aavid FROM pmc_ad_detail_basic WHERE aadvid<>'' "
        "ORDER BY aavid",
        fetch=True,
    ) or []
    result: List[Dict[str, str]] = []
    for row in rows:
        aavid = str(row.get("aavid") or "").strip()
        latest = store.execute(
            "SELECT user_info_name FROM pmc_ad_detail_basic "
            "WHERE aadvid=? AND COALESCE(user_info_name,'')<>'' "
            "ORDER BY updated_at DESC,id DESC LIMIT 1",
            (aavid,),
            fetch=True,
        ) or []
        name = str(latest[0].get("user_info_name") or "") if latest else ""
        result.append({"aavid": aavid, "account_name": name or f"千川账户 {aavid}"})
    return result


def get_operation_daily_report_config(database: Optional[str] = None) -> Dict[str, Any]:
    account = current_local_feishu_account()
    if not account:
        return {
            "success": False,
            "message": "请先登录工具账号",
            "config": dict(DEFAULT_CONFIG),
            "accounts": [],
        }
    config = _profile_config(account)
    options = list_operation_account_options(database)
    selected = [
        str(item.get("aavid") or "")
        for item in options
        if bool(item.get("report_enabled"))
    ]
    if options:
        config["aavids"] = selected
    return {
        "success": True,
        "account_username": account,
        "config": config,
        "accounts": options,
    }


def save_operation_daily_report_config(
    config: Dict[str, Any],
    database: Optional[str] = None,
) -> Dict[str, Any]:
    account = current_local_feishu_account()
    if not account:
        return {"success": False, "message": "请先登录工具账号"}
    try:
        saved = _save_profile_config(account, config if isinstance(config, dict) else {})
        from services.qianchuan_accounts import (
            list_qianchuan_accounts,
            migrate_existing_qianchuan_accounts,
            save_qianchuan_account_settings,
        )

        selected = set(saved.get("aavids") or [])
        store = _store(database)
        migrate_existing_qianchuan_accounts(
            owner_username=account,
            db=store,
        )
        for item in list_qianchuan_accounts(owner_username=account, db=store):
            save_qianchuan_account_settings(
                item["account_uid"],
                {
                    "report_enabled": str(item.get("aavid") or "") in selected,
                },
                owner_username=account,
                db=store,
            )
    except (RuntimeError, ValueError) as exc:
        return {"success": False, "message": str(exc)}
    logger.info(
        "[操作日报] 配置已保存 account=%s enabled=%s time=%s aavids=%s",
        account,
        saved.get("enabled"),
        saved.get("send_time"),
        ",".join(saved.get("aavids") or []),
    )
    return {"success": True, "message": "昨日操作日报设置已保存", "config": saved}


def _report_date(value: Any = None) -> date:
    if value in (None, ""):
        return datetime.now().date() - timedelta(days=1)
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value), "%Y-%m-%d").date()


def _one_line(value: Any, limit: int = 180) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


def _group_counts(
    store: SQLiteStore,
    aavid: str,
    start: str,
    end: str,
    field: str,
    account_uid: str = "",
) -> Dict[str, int]:
    if field not in {"action_type", "source", "status"}:
        raise ValueError("不支持的统计字段")
    scope = "account_uid=?" if account_uid else "aavid=?"
    scope_value = account_uid or aavid
    rows = store.execute(
        f"SELECT {field} AS name,COUNT(*) AS n FROM account_operation_event "
        f"WHERE {scope} AND occurred_at>=? AND occurred_at<=? GROUP BY " + field,
        (scope_value, start, end),
        fetch=True,
    ) or []
    return {str(row.get("name") or ""): int(row.get("n") or 0) for row in rows}


def build_operation_daily_report(
    aavid: Any,
    report_date: Any = None,
    *,
    database: Optional[str] = None,
) -> Dict[str, Any]:
    aid = str(aavid or "").strip()
    if not aid:
        raise ValueError("缺少千川账户ID")
    day = _report_date(report_date)
    start = day.strftime("%Y-%m-%d 00:00:00")
    end = day.strftime("%Y-%m-%d 23:59:59")
    store = _store(database)
    from services.qianchuan_accounts import migrate_existing_qianchuan_accounts

    owner = current_local_feishu_account() or None
    migrate_existing_qianchuan_accounts(owner_username=owner, db=store)
    directory = store.select_one(
        "qianchuan_account",
        where={
            "owner_username": _account_key(owner) if owner else "local_default",
            "aavid": aid,
        },
    ) or {}
    account_uid = str(directory.get("account_uid") or "")
    event_scope = (
        "account_uid=?"
        if account_uid
        else "aavid=?"
    )
    event_scope_param = account_uid or aid
    total_rows = store.execute(
        "SELECT COUNT(*) AS n,SUM(CASE WHEN possible_duplicate=1 THEN 1 ELSE 0 END) AS duplicate_n "
        f"FROM account_operation_event WHERE {event_scope} AND occurred_at>=? AND occurred_at<=?",
        (event_scope_param, start, end),
        fetch=True,
    ) or [{"n": 0, "duplicate_n": 0}]
    total = int(total_rows[0].get("n") or 0)
    details = store.execute(
        "SELECT action_type,source,status,operator_name,operator_id,summary,"
        "plan_id,plan_name,material_id,material_name,product_id,product_name,"
        "regulate_task_id,possible_duplicate,occurred_at "
        f"FROM account_operation_event WHERE {event_scope} AND occurred_at>=? AND occurred_at<=? "
        "ORDER BY occurred_at DESC,id DESC LIMIT ?",
        (event_scope_param, start, end, DETAIL_LIMIT),
        fetch=True,
    ) or []
    account_rows = store.execute(
        "SELECT user_info_name FROM pmc_ad_detail_basic "
        "WHERE aadvid=? AND COALESCE(user_info_name,'')<>'' "
        "ORDER BY updated_at DESC,id DESC LIMIT 1",
        (aid,),
        fetch=True,
    ) or []
    sync_rows = store.execute(
        "SELECT coverage_from,coverage_to,last_sync_at,last_status,last_error "
        "FROM platform_log_sync_state WHERE account_uid=? AND aavid=? LIMIT 1",
        (account_uid, aid),
        fetch=True,
    ) or []
    sync = sync_rows[0] if sync_rows else {
        "coverage_from": "",
        "coverage_to": "",
        "last_sync_at": "",
        "last_status": "not_configured",
        "last_error": "",
    }
    coverage_complete = bool(
        str(sync.get("last_status") or "") == "ok"
        and str(sync.get("coverage_from") or "") <= start
        and str(sync.get("coverage_to") or "") >= end
    )
    return {
        "report_uid": "daily:" + uuid.uuid4().hex,
        "report_date": day.isoformat(),
        "aavid": aid,
        "qianchuan_account_uid": str(directory.get("account_uid") or ""),
        "account_name": str(directory.get("account_name") or "")
        or (
            str(account_rows[0].get("user_info_name") or "")
            if account_rows
            else ""
        )
        or f"千川账户 {aid}",
        "event_count": total,
        "shown_count": len(details),
        "duplicate_count": int(total_rows[0].get("duplicate_n") or 0),
        "action_counts": _group_counts(
            store, aid, start, end, "action_type", account_uid
        ),
        "source_counts": _group_counts(
            store, aid, start, end, "source", account_uid
        ),
        "status_counts": _group_counts(
            store, aid, start, end, "status", account_uid
        ),
        "details": details,
        "platform_sync": sync,
        "platform_coverage_complete": coverage_complete,
    }


def build_operation_daily_report_card(report: Dict[str, Any]) -> Dict[str, Any]:
    total = int(report.get("event_count") or 0)
    action_counts = report.get("action_counts") or {}
    status_counts = report.get("status_counts") or {}
    source_counts = report.get("source_counts") or {}
    action_summary = "、".join(
        f"{ACTION_LABELS.get(action, '其他')} {int(action_counts.get(action) or 0)}"
        for action in ACTION_ORDER
        if int(action_counts.get(action) or 0) > 0
    ) or "无操作"
    result_summary = "、".join(
        f"{STATUS_LABELS.get(status, status)} {int(count or 0)}"
        for status, count in status_counts.items()
        if int(count or 0) > 0
    ) or "无结果"
    source_summary = "、".join(
        f"{SOURCE_LABELS.get(source, source)} {int(count or 0)}"
        for source, count in source_counts.items()
        if int(count or 0) > 0
    ) or "无来源记录"

    coverage_complete = bool(report.get("platform_coverage_complete"))
    sync = report.get("platform_sync") or {}
    coverage_text = (
        "后台操作日志已覆盖完整日期"
        if coverage_complete
        else "后台操作日志覆盖不完整，本日报可能缺少工具外操作"
    )
    if sync.get("last_error"):
        coverage_text += f"；同步异常：{_one_line(sync.get('last_error'), 220)}"
    elif sync.get("last_sync_at"):
        coverage_text += f"；最后同步：{sync.get('last_sync_at')}"

    detail_lines: List[str] = []
    for item in report.get("details") or []:
        action = ACTION_LABELS.get(str(item.get("action_type") or ""), "其他")
        status = STATUS_LABELS.get(str(item.get("status") or ""), "结果未知")
        object_text = (
            _one_line(item.get("plan_name"), 70)
            or _one_line(item.get("material_name"), 70)
            or _one_line(item.get("product_name"), 70)
            or _one_line(item.get("plan_id"), 70)
            or _one_line(item.get("material_id"), 70)
            or _one_line(item.get("regulate_task_id"), 70)
            or "未命名对象"
        )
        operator = _one_line(item.get("operator_name") or item.get("operator_id"), 40)
        summary = _one_line(item.get("summary"), 130)
        line = (
            f"{str(item.get('occurred_at') or '')[11:16]}｜{action}｜{object_text}｜{status}"
        )
        if operator:
            line += f"｜{operator}"
        if summary and summary != object_text:
            line += f"\n{summary}"
        if int(item.get("possible_duplicate") or 0) == 1:
            line += "（可能重复）"
        detail_lines.append(line)
    if not detail_lines:
        detail_lines = ["昨日没有查到操作记录。"]
    elif total > len(detail_lines):
        detail_lines.append(f"共 {total} 条，卡片仅展示最近 {len(report.get('details') or [])} 条。")

    warning = ""
    duplicate_count = int(report.get("duplicate_count") or 0)
    if duplicate_count:
        warning = f"\n**去重提醒：** 有 {duplicate_count} 条记录标记为“可能重复”，已保留供核对。"
    return {
        "config": {
            "wide_screen_mode": True,
            "enable_forward": False,
        },
        "header": {
            "template": "blue" if coverage_complete else "orange",
            "title": {
                "tag": "plain_text",
                "content": f"千川账户操作日报 · {report.get('report_date')}",
            },
        },
        "elements": [
            {
                "tag": "markdown",
                "content": (
                    f"**千川账户：** {report.get('account_name') or '未命名账户'}"
                    f"\n**账户ID：** {report.get('aavid') or ''}"
                    f"\n**统计日期：** {report.get('report_date')} 00:00–23:59"
                ),
            },
            {
                "tag": "markdown",
                "content": (
                    f"**操作总数：** {total}"
                    f"\n**操作类型：** {action_summary}"
                    f"\n**执行结果：** {result_summary}"
                    f"\n**记录来源：** {source_summary}"
                ),
            },
            {"tag": "hr"},
            {
                "tag": "markdown",
                "content": "**操作明细（时间倒序）**\n" + "\n\n".join(detail_lines),
            },
            {
                "tag": "note",
                "elements": [
                    {
                        "tag": "plain_text",
                        "content": coverage_text + warning.replace("**", ""),
                    }
                ],
            },
        ],
    }


def build_operation_daily_summary_card(
    reports: List[Dict[str, Any]],
    report_date: str,
) -> Dict[str, Any]:
    total_events = sum(int(item.get("event_count") or 0) for item in reports)
    total_retarget = sum(
        int((item.get("action_counts") or {}).get("retarget") or 0)
        for item in reports
    )
    total_stop = sum(
        int((item.get("action_counts") or {}).get("stop") or 0)
        for item in reports
    )
    total_failed = sum(
        int((item.get("status_counts") or {}).get("failed") or 0)
        for item in reports
    )
    incomplete = [
        item for item in reports if not bool(item.get("platform_coverage_complete"))
    ]
    lines: List[str] = []
    for item in reports:
        counts = item.get("action_counts") or {}
        status_counts = item.get("status_counts") or {}
        coverage = "完整" if item.get("platform_coverage_complete") else "不完整"
        lines.append(
            f"**{item.get('account_name') or item.get('aavid')}**"
            f" · ID {item.get('aavid')}"
            f"\n操作 {int(item.get('event_count') or 0)}"
            f" · 追投 {int(counts.get('retarget') or 0)}"
            f" · 停投 {int(counts.get('stop') or 0)}"
            f" · 失败 {int(status_counts.get('failed') or 0)}"
            f" · 日志{coverage}"
        )
    if not lines:
        lines = ["尚未选择需要日报的千川账户。"]
    return {
        "config": {"wide_screen_mode": True, "enable_forward": False},
        "header": {
            "template": "orange" if incomplete else "blue",
            "title": {
                "tag": "plain_text",
                "content": f"千川多账户操作日报总览 · {report_date}",
            },
        },
        "elements": [
            {
                "tag": "markdown",
                "content": (
                    f"**账户数：** {len(reports)}"
                    f"\n**操作总数：** {total_events}"
                    f" · **追投：** {total_retarget}"
                    f" · **停投：** {total_stop}"
                    f" · **失败：** {total_failed}"
                    f"\n**后台日志不完整账户：** {len(incomplete)}"
                ),
            },
            {"tag": "hr"},
            {
                "tag": "markdown",
                "content": "\n\n".join(lines),
            },
            {
                "tag": "note",
                "elements": [
                    {
                        "tag": "plain_text",
                        "content": (
                            "总览包含全部已勾选账户；详细卡片仅发送给有操作记录、"
                            "日志异常或已明确开启空日报的账户。"
                        ),
                    }
                ],
            },
        ],
    }


def _delivery_key(
    account_username: str,
    aavid: str,
    report_date: str,
    receive_type: str,
    receive_id: str,
) -> str:
    raw = "|".join(
        [_account_key(account_username), aavid, report_date, receive_type, receive_id]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _record_delivery(
    store: SQLiteStore,
    *,
    delivery_key: Optional[str],
    report: Dict[str, Any],
    account_username: str,
    mode: str,
    receive_type: str,
    receive_id: str,
    status: str,
    message_id: str = "",
    error: str = "",
) -> None:
    data = {
        "delivery_key": delivery_key,
        "report_uid": str(report.get("report_uid") or "daily:" + uuid.uuid4().hex),
        "account_username": _account_key(account_username),
        "aavid": str(report.get("aavid") or ""),
        "qianchuan_account_uid": str(
            report.get("qianchuan_account_uid") or ""
        ),
        "report_date": str(report.get("report_date") or ""),
        "delivery_mode": mode,
        "receive_type": receive_type,
        "receive_id": receive_id,
        "message_id": message_id,
        "status": status,
        "event_count": int(report.get("event_count") or 0),
        "last_error": _one_line(error, 800),
        "sent_at": _now_text() if status == "success" else "",
    }
    if delivery_key:
        store.insert_or_update(
            "operation_daily_report_delivery",
            data,
            unique_fields=["delivery_key"],
        )
    else:
        store.insert("operation_daily_report_delivery", data)


def send_operation_daily_report(
    *,
    report_date: Any = None,
    mode: str = "manual",
    database: Optional[str] = None,
) -> Dict[str, Any]:
    account = current_local_feishu_account()
    if not account:
        return {"success": False, "message": "请先登录工具账号"}
    feishu = get_local_feishu_status()
    if not feishu.get("connected"):
        return {"success": False, "message": "飞书长连接尚未连接"}
    admin_targets = list_local_feishu_bound_targets()
    if not admin_targets:
        return {"success": False, "message": "尚未绑定个人或接收群"}
    config = _profile_config(account)
    options = list_operation_account_options(database)
    selected_from_directory = [
        str(item.get("aavid") or "")
        for item in options
        if bool(item.get("report_enabled"))
    ]
    aavids = (
        selected_from_directory
        if options
        else (config.get("aavids") or [])
    )
    if not aavids:
        return {"success": False, "message": "尚未勾选需要日报的千川账户"}
    day = _report_date(report_date)
    store = _store(database)
    from services.qianchuan_accounts import resolve_account_feishu_targets

    sent_count = 0
    skipped_count = 0
    errors: List[str] = []
    reports: List[Dict[str, Any]] = []
    for aavid in aavids:
        report = build_operation_daily_report(aavid, day, database=database)
        reports.append(report)
        sync = report.get("platform_sync") or {}
        needs_detail = bool(int(report.get("event_count") or 0)) or not bool(
            report.get("platform_coverage_complete")
        ) or bool(sync.get("last_error")) or bool(config.get("send_empty", False))
        if not needs_detail:
            skipped_count += 1
            continue
        detail_targets = resolve_account_feishu_targets(
            aavid,
            owner_username=account,
            db=store,
            default_targets=admin_targets,
        )
        if not detail_targets:
            errors.append(f"{aavid}: 未配置可用的飞书接收位置")
            continue
        card = build_operation_daily_report_card(report)
        for receive_type, receive_id in detail_targets:
            key = (
                _delivery_key(
                    account,
                    str(aavid),
                    str(report.get("report_date") or ""),
                    receive_type,
                    receive_id,
                )
                if mode == "scheduled"
                else None
            )
            if key:
                existing = store.select_one(
                    "operation_daily_report_delivery",
                    where={"delivery_key": key},
                )
                if existing and existing.get("status") == "success":
                    skipped_count += 1
                    continue
            try:
                sent = send_local_feishu_bound_card(
                    card,
                    targets=[(receive_type, receive_id)],
                )
                message_id = str(sent[0].get("message_id") or "") if sent else ""
                _record_delivery(
                    store,
                    delivery_key=key,
                    report=report,
                    account_username=account,
                    mode=mode,
                    receive_type=receive_type,
                    receive_id=receive_id,
                    status="success",
                    message_id=message_id,
                )
                sent_count += 1
            except Exception as exc:
                _record_delivery(
                    store,
                    delivery_key=key,
                    report=report,
                    account_username=account,
                    mode=mode,
                    receive_type=receive_type,
                    receive_id=receive_id,
                    status="failed",
                    error=str(exc),
                )
                errors.append(f"{aavid}/{receive_type}: {exc}")

    summary_report = {
        "report_uid": "daily-summary:" + uuid.uuid4().hex,
        "report_date": day.isoformat(),
        "aavid": "__summary__",
        "qianchuan_account_uid": "",
        "event_count": sum(int(item.get("event_count") or 0) for item in reports),
    }
    summary_card = build_operation_daily_summary_card(reports, day.isoformat())
    for receive_type, receive_id in admin_targets:
        key = (
            _delivery_key(
                account,
                "__summary__",
                day.isoformat(),
                receive_type,
                receive_id,
            )
            if mode == "scheduled"
            else None
        )
        if key:
            existing = store.select_one(
                "operation_daily_report_delivery",
                where={"delivery_key": key},
            )
            if existing and existing.get("status") == "success":
                skipped_count += 1
                continue
        try:
            sent = send_local_feishu_bound_card(
                summary_card,
                targets=[(receive_type, receive_id)],
            )
            message_id = str(sent[0].get("message_id") or "") if sent else ""
            _record_delivery(
                store,
                delivery_key=key,
                report=summary_report,
                account_username=account,
                mode=mode,
                receive_type=receive_type,
                receive_id=receive_id,
                status="success",
                message_id=message_id,
            )
            sent_count += 1
        except Exception as exc:
            _record_delivery(
                store,
                delivery_key=key,
                report=summary_report,
                account_username=account,
                mode=mode,
                receive_type=receive_type,
                receive_id=receive_id,
                status="failed",
                error=str(exc),
            )
            errors.append(f"总览/{receive_type}: {exc}")
    success = sent_count > 0 or (skipped_count > 0 and not errors)
    if sent_count:
        message = f"已发送 {sent_count} 张多账户昨日操作日报卡片"
    elif skipped_count and not errors:
        message = "昨日操作日报已发送过，无需重复发送"
    else:
        message = "昨日操作日报发送失败"
    if errors:
        message += "；" + "；".join(errors[:3])
    return {
        "success": success,
        "message": message,
        "report_date": day.isoformat(),
        "sent_count": sent_count,
        "skipped_count": skipped_count,
        "errors": errors,
        "reports": [
            {
                "aavid": str(item.get("aavid") or ""),
                "account_name": str(item.get("account_name") or ""),
                "event_count": int(item.get("event_count") or 0),
                "coverage_complete": bool(item.get("platform_coverage_complete")),
            }
            for item in reports
        ],
    }


def send_yesterday_operation_daily_report_now() -> Dict[str, Any]:
    return send_operation_daily_report(mode="manual")


def run_operation_daily_report_scheduler_once(
    *,
    now: Optional[datetime] = None,
    database: Optional[str] = None,
) -> Dict[str, Any]:
    current = now or datetime.now()
    account = current_local_feishu_account()
    if not account:
        return {"success": True, "skipped": True, "reason": "logged_out"}
    config = _profile_config(account)
    if not config.get("enabled"):
        return {"success": True, "skipped": True, "reason": "disabled"}
    if current.strftime("%H:%M") < str(config.get("send_time") or "09:00"):
        return {"success": True, "skipped": True, "reason": "not_due"}
    return send_operation_daily_report(
        report_date=current.date() - timedelta(days=1),
        mode="scheduled",
        database=database,
    )


def start_operation_daily_report_background_thread() -> None:
    global SCHEDULER_STARTED
    with SCHEDULER_LOCK:
        if SCHEDULER_STARTED:
            return
        SCHEDULER_STARTED = True

    def _loop() -> None:
        logger.info("[操作日报] 后台线程已启动，每30秒检查一次")
        time.sleep(8)
        while True:
            try:
                result = run_operation_daily_report_scheduler_once()
                if not result.get("success") and not result.get("skipped"):
                    logger.warning(
                        "[操作日报] 定时发送未完成: %s",
                        result.get("message") or result,
                    )
                elif int(result.get("sent_count") or 0) > 0:
                    logger.info(
                        "[操作日报] 定时发送完成 date=%s sent=%s",
                        result.get("report_date"),
                        result.get("sent_count"),
                    )
            except Exception as exc:
                logger.warning("[操作日报] 定时检查失败: %s", exc)
            time.sleep(30)

    threading.Thread(
        target=_loop,
        daemon=True,
        name="qcsckp-operation-daily-report",
    ).start()
