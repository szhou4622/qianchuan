"""直播/商品全域监控目标与商品关系管理。"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlencode, urlparse, urlunparse

from services.plan_system import (
    ALLOWED_PLAN_SYSTEMS,
    detect_plan_system,
    normalize_plan_system,
)
from utils.sqlite_store import SQLiteStore, init_sqlite_schema


# 启用数量不再使用固定条数截断，改由账户容量模型把超量计划
# 标记为 capacity_waiting。
MAX_ENABLED_TARGETS = 0
LEGACY_TARGET_UID = "legacy_unscoped"
ALLOWED_SCENES = frozenset({"live", "product"})
ALLOWED_FILTER_MODES = frozenset({"all", "selected"})
ALLOWED_VERIFICATION_STATES = frozenset(
    {"legacy_unverified", "candidate", "verified", "missing", "error"}
)
ACTIVE_PLATFORM_STATUSES = frozenset(
    {"active", "enabled", "delivering", "learning", "running"}
)


def normalize_platform_status(value: Any) -> str:
    """Normalize only explicit platform status evidence; unknown remains blocked."""
    raw = str(value or "").strip().lower()
    aliases = {
        "1": "active",
        "2": "paused",
        "3": "ended",
        "4": "deleted",
        "投放中": "active",
        "生效中": "active",
        "学习中": "learning",
        "启用": "enabled",
        "已启用": "enabled",
        "暂停": "paused",
        "已暂停": "paused",
        "系统暂停": "paused",
        "结束": "ended",
        "已结束": "ended",
        "删除": "deleted",
        "已删除": "deleted",
        "历史": "historical",
    }
    return aliases.get(raw, raw or "unknown")[:64]


def normalize_verification_state(value: Any) -> str:
    state = str(value or "").strip().lower()
    return state if state in ALLOWED_VERIFICATION_STATES else "legacy_unverified"


def target_eligibility(
    *,
    promotion_scene: Any,
    plan_system: Any,
    platform_status: Any,
    verification_state: Any,
    capability: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Derive all automation gates from persisted, explicit catalog evidence."""
    scene = str(promotion_scene or "").strip().lower()
    system = normalize_plan_system(plan_system)
    status = normalize_platform_status(platform_status)
    verification = normalize_verification_state(verification_state)
    reasons: List[str] = []
    if verification != "verified":
        reasons.append("计划身份尚未通过精确详情核验")
    if scene not in ALLOWED_SCENES:
        reasons.append("推广方式待确认")
    if system not in {"global", "chengfang"}:
        reasons.append("计划体系待确认")
    if status not in ACTIVE_PLATFORM_STATUSES:
        if status == "unknown":
            reasons.append("平台状态待确认")
        else:
            reasons.append(f"平台状态为{status}，不可参与自动化")
    monitor = not reasons
    cap = capability if isinstance(capability, dict) else {}
    from services.promotion_capability import capability_is_required

    requires_capability = capability_is_required(
        promotion_scene=scene,
        plan_system=system,
    )
    retarget_capable = bool(
        cap.get("retarget_supported")
        or cap.get("retarget_submit_supported")
        or cap.get("retarget")
        or cap.get("retarget_execute")
        or not requires_capability
    )
    stop_capable = bool(
        cap.get("stop_supported")
        or cap.get("regulation_supported")
        or cap.get("pause_supported")
        or cap.get("delete_supported")
        or cap.get("regulation_execute")
        or not requires_capability
    )
    return {
        "platform_status": status,
        "verification_state": verification,
        "monitor_eligible": monitor,
        "retarget_eligible": monitor and retarget_capable,
        "stop_eligible": monitor and stop_capable,
        "ineligible_reason": "；".join(reasons),
    }


def make_target_uid(aavid: Any, ad_id: Any) -> str:
    """稳定、无敏感信息的账户 + 计划标识。"""
    aid = str(aavid or "").strip()
    pid = str(ad_id or "").strip()
    if not aid or not pid:
        raise ValueError("缺少 aavid 或 ad_id")
    digest = hashlib.sha256(f"{aid}:{pid}".encode("utf-8")).hexdigest()[:24]
    return f"target_{digest}"


def make_scoped_target_uid(
    account_uid: Any,
    aavid: Any,
    ad_id: Any,
) -> str:
    """同一千川计划被不同工具账号登记时使用的隔离标识。"""
    scope = str(account_uid or "").strip()
    aid = str(aavid or "").strip()
    pid = str(ad_id or "").strip()
    if not scope or not aid or not pid:
        raise ValueError("缺少 account_uid、aavid 或 ad_id")
    digest = hashlib.sha256(
        f"{scope}:{aid}:{pid}".encode("utf-8")
    ).hexdigest()[:24]
    return f"target_{digest}"


def _owner_key(value: Any = None) -> str:
    text = str(value or "").strip().casefold()
    if text:
        return text
    try:
        from services.qianchuan_session import current_session_owner

        text = str(current_session_owner() or "").strip().casefold()
    except Exception:
        text = ""
    return text or "local_default"


def _legacy_quarantine_account_uid(
    scoped_target_uid: Any,
    legacy_target_uid: Any,
) -> str:
    digest = hashlib.sha256(
        (
            f"{str(scoped_target_uid or '').strip()}:"
            f"{str(legacy_target_uid or '').strip()}"
        ).encode("utf-8")
    ).hexdigest()[:24]
    return f"legacy_quarantined_{digest}"


def _quarantine_unowned_target_conflicts(
    store: SQLiteStore,
    *,
    aavid: str,
    ad_id: str,
    scoped_target_uid: str,
    connection: Any = None,
) -> int:
    """保留冲突旧行用于审计，但永久移除其自动化和待认领权力。"""
    rows = store.execute(
        "SELECT target_uid FROM promotion_target "
        "WHERE aadvid=? AND ad_id=? AND COALESCE(account_uid,'')=''",
        (aavid, ad_id),
        connection=connection,
        fetch=True,
    ) or []
    changed = 0
    for row in rows:
        legacy_uid = str(row.get("target_uid") or "").strip()
        if not legacy_uid or legacy_uid == scoped_target_uid:
            continue
        reason = "旧计划与当前账号计划重复，已安全隔离并保留历史记录"
        store.update(
            "promotion_target",
            {
                "account_uid": _legacy_quarantine_account_uid(
                    scoped_target_uid,
                    legacy_uid,
                ),
                "enabled": 0,
                "platform_status": "unknown",
                "verification_state": "legacy_unverified",
                "catalog_seen_at": None,
                "last_verified_at": None,
                "last_verification_error": reason,
                "monitor_eligible": 0,
                "retarget_eligible": 0,
                "stop_eligible": 0,
                "ineligible_reason": reason,
                "capability_json": "{}",
                "automation_write_blocked": 1,
                "write_block_reason": reason,
                "write_block_origin": "legacy_quarantine",
                "write_blocked_at": datetime.now().strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
                "capacity_state": "disabled",
            },
            where={"target_uid": legacy_uid},
            connection=connection,
        )
        changed += 1
    return changed


def _reconcile_legacy_target_conflicts(
    store: SQLiteStore,
    *,
    owner_username: Any = None,
) -> int:
    owner = _owner_key(owner_username)
    conflicts = store.execute(
        "SELECT l.target_uid AS legacy_uid,s.target_uid AS scoped_uid,"
        "s.account_uid,s.aadvid,s.ad_id "
        "FROM promotion_target l "
        "JOIN promotion_target s ON s.aadvid=l.aadvid AND s.ad_id=l.ad_id "
        "JOIN qianchuan_account a ON a.account_uid=s.account_uid "
        "WHERE COALESCE(l.account_uid,'')='' "
        "AND COALESCE(s.account_uid,'')<>'' AND a.owner_username=?",
        (owner,),
        fetch=True,
    ) or []
    if not conflicts:
        return 0
    changed = 0
    with store.transaction() as conn:
        store.execute("BEGIN IMMEDIATE", connection=conn)
        for conflict in conflicts:
            changed += _quarantine_unowned_target_conflicts(
                store,
                aavid=str(conflict.get("aadvid") or ""),
                ad_id=str(conflict.get("ad_id") or ""),
                scoped_target_uid=str(conflict.get("scoped_uid") or ""),
                connection=conn,
            )
    return changed


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _json_list(raw: Any) -> List[str]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            raw = [x.strip() for x in raw.split(",") if x.strip()]
    if not isinstance(raw, (list, tuple, set)):
        return []
    result: List[str] = []
    seen = set()
    for value in raw:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def normalize_scene(value: Any) -> str:
    scene = str(value or "").strip().lower()
    aliases = {
        "live_room": "live",
        "liveroom": "live",
        "直播": "live",
        "goods": "product",
        "commodity": "product",
        "商品": "product",
    }
    scene = aliases.get(scene, scene)
    if scene not in ALLOWED_SCENES:
        raise ValueError("promotion_scene 仅支持 live 或 product")
    return scene


def detect_promotion_scene(
    url: str,
    *,
    page_text: str = "",
    explicit_scene: Any = None,
) -> Optional[str]:
    """从详情页 URL/可见文本识别直播或商品全域；不明确时返回 None。"""
    if explicit_scene not in (None, ""):
        return normalize_scene(explicit_scene)
    raw = f"{url or ''}\n{page_text or ''}".lower()
    product_tokens = (
        "推商品",
        "商品全域",
        "product",
        "commodity",
        "goods",
        "productrace",
    )
    live_tokens = (
        "推直播",
        "直播全域",
        "liverace",
        "live_room",
        "liveroom",
    )
    product_hit = any(x in raw for x in product_tokens)
    live_hit = any(x in raw for x in live_tokens)
    if product_hit and not live_hit:
        return "product"
    if live_hit and not product_hit:
        return "live"
    return None


def detect_confirmed_detail_scene(url: str, *, page_text: str = "") -> Optional[str]:
    """只在明确的计划详情上下文中确认推广场景，避免把导航页误登记为计划。"""
    parsed = urlparse(str(url or "").strip())
    if "/uni-prom/detail" not in parsed.path:
        return None
    aavid, ad_id = extract_target_ids(url)
    if not aavid or not ad_id:
        return None

    decoded_url = unquote(str(url or "")).lower()
    product_url = "productrace" in decoded_url
    live_url = "liverace" in decoded_url
    if product_url != live_url:
        return "product" if product_url else "live"

    text = re.sub(r"\s+", " ", str(page_text or "")).strip()
    product_context = (
        ("商品全域" in text or "推商品" in text)
        and ("商品自选" in text or "素材追投" in text or "调控工具" in text)
    )
    live_context = (
        ("直播全域" in text or "推直播" in text)
        and ("直播间" in text or "素材追投" in text or "调控工具" in text)
    )
    if product_context != live_context:
        return "product" if product_context else "live"
    return None


def extract_plan_name(
    *,
    page_text: str = "",
    page_title: str = "",
    ad_id: Any = "",
) -> str:
    """优先读取页面显式计划名称；通用后台标题不再冒充计划名。"""
    text = str(page_text or "")
    for pattern in (
        r"(?:计划名称|广告名称)\s*[：:]\s*([^\r\n]{1,120})",
        r"(?:计划名称|广告名称)\s+([^\r\n]{1,120})",
    ):
        match = re.search(pattern, text)
        if match:
            value = match.group(1).strip(" \t：:")
            if value:
                return value[:256]

    title = str(page_title or "").strip()
    generic_titles = {
        "投放管理",
        "巨量千川",
        "千川",
        "巨量千川工作台",
    }
    if title and title not in generic_titles and "投放管理" not in title:
        return title[:256]
    plan_id = str(ad_id or "").strip()
    return f"计划 {plan_id}" if plan_id else "未命名计划"


def extract_target_ids(url: str) -> Tuple[str, str]:
    parsed = urlparse(str(url or "").strip())
    query = parse_qs(parsed.query)
    fragment = parsed.fragment or ""
    if "?" in fragment:
        fragment = fragment.split("?", 1)[1]
    frag_query = parse_qs(fragment)
    merged = {**query, **frag_query}

    def first(*names: str) -> str:
        for name in names:
            values = merged.get(name)
            if values:
                text = str(values[0] or "").strip()
                if text:
                    return text
        return ""

    return first("aavid", "aAvid", "AAVID"), first("adId", "ad_id", "adID")


def sanitize_target_url(url: str) -> str:
    """仅保留重建详情页所需参数，剔除 token、签名和临时状态。"""
    parsed = urlparse(str(url or "").strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ""
    allowed = {
        "aavid",
        "adId",
        "ad_id",
        "ct",
        "liveQcpxMode",
        "dr",
    }
    query = parse_qs(parsed.query)
    safe_query = []
    for key in allowed:
        for value in query.get(key, []):
            safe_query.append((key, value))
    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            "",
            urlencode(safe_query),
            "",
        )
    )


def _target_row(row: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(row)
    out["enabled"] = bool(out.get("enabled"))
    for key in ("monitor_eligible", "retarget_eligible", "stop_eligible"):
        out[key] = bool(out.get(key))
    out["automation_write_blocked"] = bool(
        out.get("automation_write_blocked")
    )
    out["product_ids"] = _json_list(out.pop("product_ids_json", None))
    for key in ("capability_json",):
        raw = out.get(key)
        if isinstance(raw, str) and raw.strip():
            try:
                out[key[:-5]] = json.loads(raw)
            except (TypeError, ValueError, json.JSONDecodeError):
                out[key[:-5]] = {}
        else:
            out[key[:-5]] = {}
    return out


def list_promotion_targets(
    *,
    enabled: Optional[bool] = None,
    owner_username: Any = None,
    db: Optional[SQLiteStore] = None,
    ensure_schema: bool = True,
    perform_repairs: bool = True,
) -> List[Dict[str, Any]]:
    store = db or SQLiteStore()
    if ensure_schema:
        init_sqlite_schema(database=store.config.get("database"))
    from services.qianchuan_accounts import _initialize_directory_selection

    if perform_repairs:
        _initialize_directory_selection(store)
    owner = _owner_key(owner_username)
    sql = (
        "SELECT t.*,a.account_name AS account_name,"
        "a.enabled AS account_enabled FROM promotion_target t "
        "JOIN qianchuan_account a ON a.account_uid=t.account_uid "
        "WHERE a.owner_username=? AND a.directory_selected=1"
    )
    params: List[Any] = [owner]
    if enabled is not None:
        sql += " AND t.enabled=?"
        params.append(1 if enabled else 0)
    sql += " ORDER BY t.enabled DESC,t.updated_at DESC,t.id DESC"
    rows = store.execute(sql, tuple(params), fetch=True) or []
    return [_target_row(row) for row in rows]


def get_promotion_target(
    target_uid: Any,
    *,
    owner_username: Any = None,
    db: Optional[SQLiteStore] = None,
) -> Optional[Dict[str, Any]]:
    uid = str(target_uid or "").strip()
    if not uid:
        return None
    store = db or SQLiteStore()
    if not hasattr(store, "config"):
        row = store.select_one(
            "promotion_target",
            where={"target_uid": uid},
        )
        return _target_row(row) if row else None
    init_sqlite_schema(database=store.config.get("database"))
    from services.qianchuan_accounts import _initialize_directory_selection

    _initialize_directory_selection(store)
    rows = store.execute(
        "SELECT t.* FROM promotion_target t "
        "JOIN qianchuan_account a ON a.account_uid=t.account_uid "
        "WHERE t.target_uid=? AND a.owner_username=? "
        "AND a.directory_selected=1 LIMIT 1",
        (uid, _owner_key(owner_username)),
        fetch=True,
    ) or []
    row = rows[0] if rows else None
    return _target_row(row) if row else None


def refresh_target_eligibility(
    target_uid: Any,
    *,
    db: Optional[SQLiteStore] = None,
) -> Optional[Dict[str, Any]]:
    """Recalculate persisted gates after capability or catalog evidence changes."""
    uid = str(target_uid or "").strip()
    if not uid:
        return None
    store = db or SQLiteStore()
    row = store.select_one("promotion_target", where={"target_uid": uid})
    if not row:
        return None
    try:
        capability = json.loads(str(row.get("capability_json") or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        capability = {}
    eligibility = target_eligibility(
        promotion_scene=row.get("promotion_scene"),
        plan_system=row.get("plan_system"),
        platform_status=row.get("platform_status"),
        verification_state=row.get("verification_state"),
        capability=capability,
    )
    store.update(
        "promotion_target",
        {
            "platform_status": eligibility["platform_status"],
            "verification_state": eligibility["verification_state"],
            "monitor_eligible": 1 if eligibility["monitor_eligible"] else 0,
            "retarget_eligible": 1 if eligibility["retarget_eligible"] else 0,
            "stop_eligible": 1 if eligibility["stop_eligible"] else 0,
            "ineligible_reason": str(eligibility["ineligible_reason"] or "")[:1000],
        },
        where={"target_uid": uid},
    )
    saved = store.select_one("promotion_target", where={"target_uid": uid})
    return _target_row(saved) if saved else None


def update_target_catalog_evidence(
    target_uid: Any,
    *,
    platform_status: Any,
    verification_state: Any,
    plan_system: Any = None,
    promotion_scene: Any = None,
    db: Optional[SQLiteStore] = None,
) -> Dict[str, Any]:
    """Persist trusted read-only catalog evidence and recompute safety gates."""
    uid = str(target_uid or "").strip()
    store = db or SQLiteStore()
    normalized_status = normalize_platform_status(platform_status)
    normalized_verification = normalize_verification_state(
        verification_state
    )
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    account_uid = ""
    with store.transaction() as conn:
        store.execute("BEGIN IMMEDIATE", connection=conn)
        row = store.select_one(
            "promotion_target",
            where={"target_uid": uid},
            connection=conn,
        )
        if not row:
            raise ValueError("监控计划不存在")
        account_uid = str(row.get("account_uid") or "")
        scene = (
            normalize_scene(promotion_scene)
            if promotion_scene is not None
            else normalize_scene(row.get("promotion_scene"))
        )
        system = (
            normalize_plan_system(plan_system)
            if plan_system is not None
            else normalize_plan_system(row.get("plan_system"))
        )
        try:
            capability = json.loads(str(row.get("capability_json") or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            capability = {}
        eligibility = target_eligibility(
            promotion_scene=scene,
            plan_system=system,
            platform_status=normalized_status,
            verification_state=normalized_verification,
            capability=capability,
        )
        values: Dict[str, Any] = {
            "platform_status": eligibility["platform_status"],
            "verification_state": eligibility["verification_state"],
            "catalog_seen_at": now,
            "promotion_scene": scene,
            "plan_system": system,
            "monitor_eligible": 1 if eligibility["monitor_eligible"] else 0,
            "retarget_eligible": 1 if eligibility["retarget_eligible"] else 0,
            "stop_eligible": 1 if eligibility["stop_eligible"] else 0,
            "ineligible_reason": str(
                eligibility["ineligible_reason"] or ""
            )[:1000],
        }
        if values["verification_state"] == "verified":
            values["last_verified_at"] = now
            values["last_verification_error"] = ""
        store.update(
            "promotion_target",
            values,
            where={"target_uid": uid},
            connection=conn,
        )
        explicitly_delivering = (
            values["verification_state"] == "verified"
            and values["platform_status"] in ACTIVE_PLATFORM_STATUSES
        )
        if explicitly_delivering:
            store.update(
                "promotion_target",
                {
                    "automation_write_blocked": 0,
                    "write_block_reason": "",
                    "write_block_origin": "",
                    "write_blocked_at": None,
                },
                where=(
                    "target_uid=? AND automation_write_blocked=1 "
                    "AND write_block_origin='verification_failure'"
                ),
                params=(uid,),
                connection=conn,
            )
    from services.qianchuan_accounts import refresh_monitor_capacity

    account = store.select_one(
        "qianchuan_account",
        fields="owner_username",
        where={"account_uid": account_uid},
    )
    refresh_monitor_capacity(
        owner_username=(account or {}).get("owner_username"),
        db=store,
    )
    saved = store.select_one("promotion_target", where={"target_uid": uid})
    assert saved is not None
    return _target_row(saved)


def record_target_verification_failure(
    target_uid: Any,
    error: Any,
    *,
    db: Optional[SQLiteStore] = None,
) -> Dict[str, Any]:
    """原子关闭自动化权力，且不改写上一次成功核验时间。"""
    uid = str(target_uid or "").strip()
    store = db or SQLiteStore()
    reason = str(error or "本轮未取得明确投放状态")[:1000]
    with store.transaction() as conn:
        store.execute("BEGIN IMMEDIATE", connection=conn)
        row = store.select_one(
            "promotion_target",
            where={"target_uid": uid},
            connection=conn,
        )
        if not row:
            raise ValueError("监控计划不存在")
        block_values: Dict[str, Any] = {}
        if not bool(row.get("automation_write_blocked")):
            block_values = {
                "automation_write_blocked": 1,
                "write_block_reason": reason,
                "write_block_origin": "verification_failure",
                "write_blocked_at": datetime.now().strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
            }
        store.update(
            "promotion_target",
            {
                "platform_status": "unknown",
                "verification_state": "error",
                "last_verification_error": reason,
                "monitor_eligible": 0,
                "retarget_eligible": 0,
                "stop_eligible": 0,
                "ineligible_reason": reason,
                "capacity_state": "disabled",
                "last_status": "verification_error",
                "last_error": reason,
                **block_values,
            },
            where={"target_uid": uid},
            connection=conn,
        )
    saved = store.select_one("promotion_target", where={"target_uid": uid})
    assert saved is not None
    return _target_row(saved)


def upsert_promotion_target(
    data: Dict[str, Any],
    *,
    owner_username: Any = None,
    trusted_catalog: bool = False,
    db: Optional[SQLiteStore] = None,
) -> Dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("监控目标须为对象")
    init_sqlite_schema()
    store = db or SQLiteStore()
    aavid = str(data.get("aavid") or data.get("aadvid") or "").strip()
    ad_id = str(data.get("ad_id") or data.get("adId") or "").strip()
    if not aavid.isdigit() or not ad_id.isdigit():
        raise ValueError("aavid 和 ad_id 必须为数字")
    scene = normalize_scene(data.get("promotion_scene"))
    plan_system_provided = "plan_system" in data
    enabled = 1 if data.get("enabled", True) else 0
    from services.qianchuan_accounts import ensure_qianchuan_account

    account = ensure_qianchuan_account(
        aavid,
        account_name=data.get("account_name") or "",
        owner_username=owner_username,
        directory_selected=True,
        seen=True,
        db=store,
    )
    account_uid = str(account["account_uid"])
    existing = store.select_one(
        "promotion_target",
        where={
            "account_uid": account_uid,
            "aadvid": aavid,
            "ad_id": ad_id,
        },
    )
    existing_any = existing or store.select_one(
        "promotion_target",
        where={"aadvid": aavid, "ad_id": ad_id},
        order_by="id ASC",
    )
    # 普通 upsert 绝不能把无归属的历史目标直接认领给当前工具账号。
    # 旧数据只能经显式迁移流程归属；迁移时会清空启用状态和写能力证据。
    target_uid = (
        str(existing.get("target_uid") or "")
        if existing
        else (
            make_target_uid(aavid, ad_id)
            if not existing_any
            else make_scoped_target_uid(account_uid, aavid, ad_id)
        )
    )
    requested_target_uid = str(data.get("target_uid") or "").strip()
    if requested_target_uid and requested_target_uid != target_uid:
        raise ValueError("target_uid 与当前工具账号、账户或计划不匹配")
    plan_system = normalize_plan_system(
        data.get("plan_system")
        if plan_system_provided
        else (existing.get("plan_system") if existing else "unknown")
    )
    if (
        plan_system == "unknown"
        and existing
        and normalize_plan_system(existing.get("plan_system") or "unknown")
        != "unknown"
    ):
        # 一次缺少体系标志的只读刷新不能抹掉此前已确认的传统全域/乘方分类。
        plan_system = normalize_plan_system(existing.get("plan_system"))
    has_filter_update = (
        "product_filter_mode" in data
        or "product_ids" in data
        or "product_ids_json" in data
    )
    existing_filter_mode = (
        str(existing.get("product_filter_mode") or "all")
        if existing
        else "all"
    )
    filter_mode = str(
        data.get("product_filter_mode")
        or (existing_filter_mode if not has_filter_update else "all")
    ).strip().lower()
    if filter_mode not in ALLOWED_FILTER_MODES:
        raise ValueError("product_filter_mode 仅支持 all 或 selected")
    product_ids_source = data.get("product_ids", data.get("product_ids_json"))
    if not has_filter_update and existing:
        product_ids_source = existing.get("product_ids_json")
    product_ids = _json_list(product_ids_source)
    if scene == "live":
        filter_mode, product_ids = "all", []
    elif filter_mode == "selected" and not product_ids:
        raise ValueError("选择指定商品时至少需要一个 product_id")

    page_url_source = str(
        data.get("sanitized_page_url") or data.get("page_url") or ""
    )
    if not page_url_source and existing:
        page_url_source = str(existing.get("sanitized_page_url") or "")
    page_url = sanitize_target_url(page_url_source)
    # 写操作能力只能由只读探测或真实受控操作的内部接口写入。普通页面保存
    # 会回传完整目标对象，因此绝不能信任客户端提供的 capability 字段。
    capability: Any = None
    capability_json_source = (
        existing.get("capability_json") if existing else None
    )
    if isinstance(capability_json_source, str):
        try:
            capability = json.loads(capability_json_source)
        except (TypeError, ValueError, json.JSONDecodeError):
            capability = {}
    if not isinstance(capability, dict):
        capability = {}
    platform_status = normalize_platform_status(
        (
            data.get("platform_status")
            if trusted_catalog and "platform_status" in data
            else (existing.get("platform_status") if existing else "unknown")
        )
    )
    verification_state = normalize_verification_state(
        (
            data.get("verification_state")
            if trusted_catalog and "verification_state" in data
            else (
                existing.get("verification_state")
                if existing
                else "legacy_unverified"
            )
        )
    )
    incoming_verification_state = verification_state
    fresh_verification_evidence = bool(
        trusted_catalog
        and incoming_verification_state == "verified"
        and data.get("verification_evidence_fresh", True)
    )
    if (
        existing
        and normalize_verification_state(existing.get("verification_state"))
        == "verified"
        and verification_state == "candidate"
    ):
        verification_state = "verified"
        # 列表候选只能用于“仍可看见”的只读发现，不能把未知、暂停、
        # 消失或失败状态提升回 active。恢复写资格必须依赖本轮精确详情。
        existing_status = normalize_platform_status(
            existing.get("platform_status")
        )
        if platform_status in ACTIVE_PLATFORM_STATUSES:
            platform_status = existing_status
    eligibility = target_eligibility(
        promotion_scene=scene,
        plan_system=plan_system,
        platform_status=platform_status,
        verification_state=verification_state,
        capability=capability,
    )
    last_status_source = (
        data.get("last_status")
        if "last_status" in data
        else (existing.get("last_status") if existing else "pending")
    )
    last_error_source = (
        data.get("last_error")
        if "last_error" in data
        else (existing.get("last_error") if existing else "")
    )
    row = {
        "target_uid": target_uid,
        "account_uid": account_uid,
        "aadvid": aavid,
        "ad_id": ad_id,
        "plan_name": str(
            data.get("plan_name")
            or (existing.get("plan_name") if existing else "")
            or ""
        ).strip()[:256],
        "promotion_scene": scene,
        "plan_system": plan_system,
        "platform_status": eligibility["platform_status"],
        "verification_state": eligibility["verification_state"],
        "catalog_seen_at": (
            datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if trusted_catalog
            else (existing.get("catalog_seen_at") if existing else None)
        ),
        "last_verified_at": (
            datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if fresh_verification_evidence
            else (existing.get("last_verified_at") if existing else None)
        ),
        "last_verification_error": (
            ""
            if fresh_verification_evidence
            else (
                existing.get("last_verification_error")
                if existing
                else None
            )
        ),
        "monitor_eligible": 1 if eligibility["monitor_eligible"] else 0,
        "retarget_eligible": 1 if eligibility["retarget_eligible"] else 0,
        "stop_eligible": 1 if eligibility["stop_eligible"] else 0,
        "ineligible_reason": str(eligibility["ineligible_reason"] or "")[:1000],
        "enabled": enabled,
        "product_filter_mode": filter_mode,
        "product_ids_json": _json_dumps(product_ids),
        "sanitized_page_url": page_url,
        "capability_json": _json_dumps(capability),
        "last_status": str(last_status_source or "pending").strip()[:64],
        "last_error": str(last_error_source or "").strip()[:2000],
    }
    with store.transaction() as conn:
        store.execute("BEGIN IMMEDIATE", connection=conn)
        _quarantine_unowned_target_conflicts(
            store,
            aavid=aavid,
            ad_id=ad_id,
            scoped_target_uid=target_uid,
            connection=conn,
        )
        store.insert_or_update(
            "promotion_target",
            row,
            unique_fields=["account_uid", "aadvid", "ad_id"],
            connection=conn,
        )
        # A transient verification timeout is fail-closed, but it must not
        # become a permanent extra user step.  Only a fresh exact-detail proof
        # for the same scoped active plan may release this particular latch.
        # Manual, legacy-quarantine and execution-failure locks remain intact.
        if (
            fresh_verification_evidence
            and eligibility["platform_status"] in ACTIVE_PLATFORM_STATUSES
        ):
            store.update(
                "promotion_target",
                {
                    "automation_write_blocked": 0,
                    "write_block_reason": "",
                    "write_block_origin": "",
                    "write_blocked_at": None,
                },
                where=(
                    "target_uid=? AND automation_write_blocked=1 "
                    "AND write_block_origin='verification_failure'"
                ),
                params=(target_uid,),
                connection=conn,
            )
    saved = store.select_one(
        "promotion_target",
        where={
            "account_uid": account_uid,
            "aadvid": aavid,
            "ad_id": ad_id,
        },
    )
    assert saved is not None
    from services.qianchuan_accounts import refresh_monitor_capacity

    refresh_monitor_capacity(db=store)
    saved = store.select_one(
        "promotion_target",
        where={
            "account_uid": account_uid,
            "aadvid": aavid,
            "ad_id": ad_id,
        },
    )
    assert saved is not None
    return _target_row(saved)


def set_promotion_target_enabled(
    target_uid: Any,
    enabled: bool,
    *,
    db: Optional[SQLiteStore] = None,
) -> Dict[str, Any]:
    init_sqlite_schema()
    store = db or SQLiteStore()
    target = get_promotion_target(target_uid, db=store)
    if not target:
        raise ValueError("监控目标不存在")
    if enabled:
        from services.qianchuan_accounts import get_qianchuan_account

        account = get_qianchuan_account(target.get("account_uid"), db=store)
        if account and not account.get("enabled"):
            raise ValueError("请先启用该千川账户")
        if not target.get("monitor_eligible"):
            raise ValueError(
                str(target.get("ineligible_reason") or "该计划尚未通过目录核验，不能启用自动监控")
            )
    store.update(
        "promotion_target",
        {
            "enabled": 1 if enabled else 0,
            "capacity_state": "active" if enabled else "disabled",
        },
        where={"target_uid": target["target_uid"]},
    )
    from services.qianchuan_accounts import refresh_monitor_capacity

    refresh_monitor_capacity(db=store)
    result = get_promotion_target(target["target_uid"], db=store)
    assert result is not None
    return result


def set_target_automation_write_block(
    target_uid: Any,
    blocked: bool,
    *,
    reason: Any = "",
    db: Optional[SQLiteStore] = None,
) -> Dict[str, Any]:
    """Persist a safety latch that read-only collection is never allowed to clear."""
    uid = str(target_uid or "").strip()
    if not uid:
        raise ValueError("缺少监控目标")
    init_sqlite_schema()
    store = db or SQLiteStore()
    target = get_promotion_target(uid, db=store)
    if not target:
        raise ValueError("监控目标不存在或不属于当前工具账号")
    if blocked:
        store.execute(
            "UPDATE promotion_target SET automation_write_blocked=1,"
            "write_block_reason=?,write_block_origin='manual',"
            "write_blocked_at=datetime('now','+8 hours'),"
            "updated_at=datetime('now','+8 hours') WHERE target_uid=?",
            (str(reason or "自动写入安全封锁")[:2000], uid),
        )
    else:
        store.execute(
            "UPDATE promotion_target SET automation_write_blocked=0,"
            "write_block_reason='',write_block_origin='',write_blocked_at=NULL,"
            "updated_at=datetime('now','+8 hours') WHERE target_uid=?",
            (uid,),
        )
    result = get_promotion_target(uid, db=store)
    assert result is not None
    return result


def update_target_sync_state(
    target_uid: Any,
    *,
    status: str,
    error: Optional[str] = None,
    synced: bool = False,
    capability: Optional[Dict[str, Any]] = None,
    db: Optional[SQLiteStore] = None,
) -> None:
    init_sqlite_schema()
    store = db or SQLiteStore()
    values: Dict[str, Any] = {
        "last_status": str(status or "unknown")[:64],
        "last_error": str(error or "")[:2000],
    }
    if synced:
        values["last_sync_at"] = SQLiteStore.SQL_EXPR_DB_NOW
    if capability is not None:
        values["capability_json"] = _json_dumps(capability)
    # update() 不支持表达式，最后同步时间单独走 SQL。
    if values.get("last_sync_at") == SQLiteStore.SQL_EXPR_DB_NOW:
        values.pop("last_sync_at", None)
        store.execute(
            "UPDATE promotion_target SET last_status=?, last_error=?, "
            "last_sync_at=datetime('now','+8 hours'), updated_at=datetime('now','+8 hours') "
            "WHERE target_uid=?",
            (
                values["last_status"],
                values["last_error"],
                str(target_uid or "").strip(),
            ),
        )
        if capability is not None:
            store.update(
                "promotion_target",
                {"capability_json": values["capability_json"]},
                where={"target_uid": str(target_uid or "").strip()},
            )
        refresh_target_eligibility(target_uid, db=store)
        return
    store.update(
        "promotion_target",
        values,
        where={"target_uid": str(target_uid or "").strip()},
    )
    if capability is not None:
        refresh_target_eligibility(target_uid, db=store)


def patch_target_sync_state(
    target_uid: Any,
    *,
    status: Optional[str],
    error: str = "",
    synced: bool = False,
    capability_updates: Optional[Dict[str, Any]] = None,
    capability_remove_keys: Iterable[str] = (),
    db: Optional[SQLiteStore] = None,
) -> Dict[str, Any]:
    """原子合并采集状态，避免覆盖同时写入的受控追投/停投能力证据。"""
    uid = str(target_uid or "").strip()
    if not uid:
        raise ValueError("缺少监控目标")
    init_sqlite_schema()
    store = db or SQLiteStore()
    with store.transaction() as conn:
        # 先取得写锁，再读取能力快照；其他线程只能在本事务提交后写入。
        store.execute("BEGIN IMMEDIATE", connection=conn)
        row = store.select_one(
            "promotion_target",
            fields="capability_json,last_status,last_error",
            where={"target_uid": uid},
            connection=conn,
        )
        if not row:
            raise ValueError("监控目标不存在")
        raw = row.get("capability_json")
        try:
            capability = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
        except (TypeError, ValueError, json.JSONDecodeError):
            capability = {}
        if not isinstance(capability, dict):
            capability = {}
        capability.update(dict(capability_updates or {}))
        for key in capability_remove_keys or ():
            capability.pop(str(key), None)

        values: Dict[str, Any] = {
            "last_status": str(
                status
                if status is not None
                else row.get("last_status") or "unknown"
            )[:64],
            "last_error": str(
                error
                if error is not None
                else row.get("last_error") or ""
            )[:2000],
            "capability_json": _json_dumps(capability),
        }
        if synced:
            store.execute(
                "UPDATE promotion_target SET last_status=?, last_error=?, "
                "capability_json=?, last_sync_at=datetime('now','+8 hours'), "
                "updated_at=datetime('now','+8 hours') WHERE target_uid=?",
                (
                    values["last_status"],
                    values["last_error"],
                    values["capability_json"],
                    uid,
                ),
                connection=conn,
            )
        else:
            store.update(
                "promotion_target",
                values,
                where={"target_uid": uid},
                connection=conn,
            )
    refresh_target_eligibility(uid, db=store)
    return capability


def upsert_products(
    target_uid: Any,
    products: Iterable[Dict[str, Any]],
    *,
    db: Optional[SQLiteStore] = None,
) -> int:
    uid = str(target_uid or "").strip()
    if not uid:
        raise ValueError("缺少 target_uid")
    init_sqlite_schema()
    store = db or SQLiteStore()
    count = 0
    for product in products or []:
        if not isinstance(product, dict):
            continue
        product_id = str(
            product.get("product_id")
            or product.get("productId")
            or product.get("id")
            or ""
        ).strip()
        if not product_id:
            continue
        name = str(
            product.get("product_name")
            or product.get("productName")
            or product.get("name")
            or product.get("title")
            or ""
        ).strip()
        store.insert_or_update(
            "promotion_product",
            {
                "target_uid": uid,
                "product_id": product_id,
                "product_name": name[:512],
                "product_status": str(
                    product.get("product_status")
                    or product.get("status")
                    or ""
                )[:128],
                "image_url": str(
                    product.get("image_url")
                    or product.get("image")
                    or product.get("cover")
                    or ""
                )[:2000],
                "raw_json": _json_dumps(product),
            },
            unique_fields=["target_uid", "product_id"],
        )
        count += 1
    return count


def replace_material_product_links(
    target_uid: Any,
    material_id: Any,
    product_ids: Iterable[Any],
    *,
    material_name: str = "",
    db: Optional[SQLiteStore] = None,
) -> int:
    uid = str(target_uid or "").strip()
    mid = str(material_id or "").strip()
    if not uid or not mid:
        return 0
    ids = _json_list(list(product_ids or []))
    init_sqlite_schema()
    store = db or SQLiteStore()
    with store.transaction() as conn:
        store.execute(
            "DELETE FROM promotion_material_product WHERE target_uid=? AND material_id=?",
            (uid, mid),
            connection=conn,
        )
        count = 0
        for product_id in ids:
            product = store.select_one(
                "promotion_product",
                fields="product_name",
                where={"target_uid": uid, "product_id": product_id},
                connection=conn,
            )
            store.insert(
                "promotion_material_product",
                {
                    "target_uid": uid,
                    "material_id": mid,
                    "product_id": product_id,
                    "material_name": str(material_name or "")[:512],
                    "product_name": str((product or {}).get("product_name") or "")[:512],
                },
                connection=conn,
            )
            count += 1
    return count


def list_target_products(
    target_uid: Any,
    *,
    db: Optional[SQLiteStore] = None,
) -> List[Dict[str, Any]]:
    init_sqlite_schema()
    store = db or SQLiteStore()
    uid = str(target_uid or "").strip()
    if not get_promotion_target(uid, db=store):
        return []
    return store.select(
        "promotion_product",
        where={"target_uid": uid},
        order_by="product_name ASC, product_id ASC",
    )


def migrate_legacy_target_scope(*, db: Optional[SQLiteStore] = None) -> int:
    """把可明确归属的旧直播数据迁入稳定 target_uid；歧义数据保持 legacy。"""
    init_sqlite_schema()
    store = db or SQLiteStore()
    reconciled = _reconcile_legacy_target_conflicts(store)
    details = store.select(
        "pmc_ad_detail_basic",
        fields=(
            "id,account_uid,aadvid,ad_id,target_uid,plan_name,"
            "promotion_scene,plan_system"
        ),
        order_by="updated_at DESC, id DESC",
    )
    owner = _owner_key()
    migrated = reconciled
    by_account: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for detail in details:
        detail_account_uid = str(detail.get("account_uid") or "").strip()
        if detail_account_uid:
            account_row = store.select_one(
                "qianchuan_account",
                fields="owner_username",
                where={"account_uid": detail_account_uid},
            )
            if (
                account_row
                and str(account_row.get("owner_username") or "").casefold()
                != owner
            ):
                continue
        aavid = str(detail.get("aadvid") or "").strip()
        ad_id = str(detail.get("ad_id") or "").strip()
        if not aavid or not ad_id:
            continue
        try:
            scene = normalize_scene(detail.get("promotion_scene") or "live")
        except ValueError:
            scene = "live"
        if detail_account_uid:
            existing_target = store.select_one(
                "promotion_target",
                where={
                    "account_uid": detail_account_uid,
                    "aadvid": aavid,
                    "ad_id": ad_id,
                },
            )
        else:
            existing_rows = store.execute(
                "SELECT t.* FROM promotion_target t "
                "JOIN qianchuan_account a ON a.account_uid=t.account_uid "
                "WHERE a.owner_username=? AND t.aadvid=? AND t.ad_id=? "
                "LIMIT 1",
                (owner, aavid, ad_id),
                fetch=True,
            ) or []
            existing_target = existing_rows[0] if existing_rows else None
        # 历史明细只用于补齐可查看范围；没有明确启用记录的旧计划
        # 不能自动进入追投/停投监控。
        enable_target = bool(existing_target and existing_target.get("enabled"))
        saved_target = upsert_promotion_target(
            {
                "aavid": aavid,
                "ad_id": ad_id,
                "plan_name": detail.get("plan_name") or "",
                "promotion_scene": scene,
                "plan_system": detail.get("plan_system") or "unknown",
                "enabled": enable_target,
            },
            db=store,
        )
        uid = str(saved_target["target_uid"])
        saved_account_uid = str(saved_target.get("account_uid") or "")
        store.update(
            "pmc_ad_detail_basic",
            {
                "account_uid": saved_account_uid,
                "target_uid": uid,
                "promotion_scene": scene,
                "plan_system": detail.get("plan_system") or "unknown",
            },
            where={"id": detail["id"]},
        )
        by_account.setdefault((saved_account_uid, aavid), []).append(
            {
                "target_uid": uid,
                "ad_id": ad_id,
                "promotion_scene": scene,
                "plan_system": detail.get("plan_system") or "unknown",
            }
        )
        migrated += 1

    for (account_uid, aavid), targets in by_account.items():
        # 旧素材没有 ad_id；仅当账户只有一条计划时才安全迁移。
        unique = {x["target_uid"]: x for x in targets}
        if len(unique) != 1:
            continue
        target = next(iter(unique.values()))
        for table, account_col, has_account_uid, extra in (
            ("pmc_promotion_material", "aadvid", False, {"ad_id": target["ad_id"]}),
            ("pmc_retargeting_run", "aavid", True, {}),
            ("pmc_regulation_run", "aavid", True, {}),
            ("pmc_roi2_assist_task", "aadvid", True, {}),
            ("account_operation_event", "aavid", True, {}),
        ):
            values = {
                "target_uid": target["target_uid"],
                "promotion_scene": target["promotion_scene"],
                "plan_system": target.get("plan_system") or "unknown",
                **extra,
            }
            if has_account_uid:
                values["account_uid"] = account_uid
            account_scope = (
                " AND (COALESCE(account_uid,'')='' OR account_uid=?)"
                if has_account_uid
                else ""
            )
            params: Tuple[Any, ...] = (
                (aavid, LEGACY_TARGET_UID, account_uid)
                if has_account_uid
                else (aavid, LEGACY_TARGET_UID)
            )
            store.update(
                table,
                values,
                where=(
                    f"{account_col} = ? AND "
                    "(target_uid IS NULL OR target_uid = '' OR target_uid = ?)"
                    + account_scope
                ),
                params=params,
            )
    return migrated
