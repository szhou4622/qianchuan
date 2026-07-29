"""直播/商品全域监控目标与商品关系管理。"""

from __future__ import annotations

import hashlib
import json
import re
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


def make_target_uid(aavid: Any, ad_id: Any) -> str:
    """稳定、无敏感信息的账户 + 计划标识。"""
    aid = str(aavid or "").strip()
    pid = str(ad_id or "").strip()
    if not aid or not pid:
        raise ValueError("缺少 aavid 或 ad_id")
    digest = hashlib.sha256(f"{aid}:{pid}".encode("utf-8")).hexdigest()[:24]
    return f"target_{digest}"


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
    db: Optional[SQLiteStore] = None,
) -> List[Dict[str, Any]]:
    init_sqlite_schema()
    store = db or SQLiteStore()
    where = None if enabled is None else {"enabled": 1 if enabled else 0}
    rows = store.select(
        "promotion_target",
        where=where,
        order_by="enabled DESC, updated_at DESC, id DESC",
    )
    return [_target_row(row) for row in rows]


def get_promotion_target(
    target_uid: Any,
    *,
    db: Optional[SQLiteStore] = None,
) -> Optional[Dict[str, Any]]:
    uid = str(target_uid or "").strip()
    if not uid:
        return None
    init_sqlite_schema()
    store = db or SQLiteStore()
    row = store.select_one("promotion_target", where={"target_uid": uid})
    return _target_row(row) if row else None


def upsert_promotion_target(
    data: Dict[str, Any],
    *,
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
    stable_target_uid = make_target_uid(aavid, ad_id)
    requested_target_uid = str(data.get("target_uid") or "").strip()
    if requested_target_uid and requested_target_uid != stable_target_uid:
        raise ValueError("target_uid 与账户、计划不匹配")
    target_uid = stable_target_uid
    enabled = 1 if data.get("enabled", True) else 0
    existing = store.select_one(
        "promotion_target",
        where={"aadvid": aavid, "ad_id": ad_id},
    )
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
    from services.qianchuan_accounts import ensure_qianchuan_account

    account = ensure_qianchuan_account(
        aavid,
        account_name=data.get("account_name") or "",
        seen=True,
        db=store,
    )
    row = {
        "target_uid": target_uid,
        "account_uid": account["account_uid"],
        "aadvid": aavid,
        "ad_id": ad_id,
        "plan_name": str(
            data.get("plan_name")
            or (existing.get("plan_name") if existing else "")
            or ""
        ).strip()[:256],
        "promotion_scene": scene,
        "plan_system": plan_system,
        "enabled": enabled,
        "product_filter_mode": filter_mode,
        "product_ids_json": _json_dumps(product_ids),
        "sanitized_page_url": page_url,
        "capability_json": _json_dumps(capability),
        "last_status": str(last_status_source or "pending").strip()[:64],
        "last_error": str(last_error_source or "").strip()[:2000],
    }
    store.insert_or_update(
        "promotion_target",
        row,
        unique_fields=["aadvid", "ad_id"],
    )
    saved = store.select_one(
        "promotion_target",
        where={"aadvid": aavid, "ad_id": ad_id},
    )
    assert saved is not None
    from services.qianchuan_accounts import refresh_monitor_capacity

    refresh_monitor_capacity(db=store)
    saved = store.select_one(
        "promotion_target",
        where={"aadvid": aavid, "ad_id": ad_id},
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
    target = store.select_one(
        "promotion_target",
        where={"target_uid": str(target_uid or "").strip()},
    )
    if not target:
        raise ValueError("监控目标不存在")
    if enabled:
        from services.qianchuan_accounts import get_qianchuan_account

        account = get_qianchuan_account(target.get("account_uid"), db=store)
        if account and not account.get("enabled"):
            raise ValueError("请先启用该千川账户")
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
        return
    store.update(
        "promotion_target",
        values,
        where={"target_uid": str(target_uid or "").strip()},
    )


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
    return store.select(
        "promotion_product",
        where={"target_uid": str(target_uid or "").strip()},
        order_by="product_name ASC, product_id ASC",
    )


def migrate_legacy_target_scope(*, db: Optional[SQLiteStore] = None) -> int:
    """把可明确归属的旧直播数据迁入稳定 target_uid；歧义数据保持 legacy。"""
    init_sqlite_schema()
    store = db or SQLiteStore()
    details = store.select(
        "pmc_ad_detail_basic",
        fields="aadvid, ad_id, target_uid, plan_name, promotion_scene, plan_system",
        order_by="updated_at DESC, id DESC",
    )
    migrated = 0
    by_account: Dict[str, List[Dict[str, Any]]] = {}
    for detail in details:
        aavid = str(detail.get("aadvid") or "").strip()
        ad_id = str(detail.get("ad_id") or "").strip()
        if not aavid or not ad_id:
            continue
        uid = make_target_uid(aavid, ad_id)
        try:
            scene = normalize_scene(detail.get("promotion_scene") or "live")
        except ValueError:
            scene = "live"
        existing_target = store.select_one(
            "promotion_target", where={"aadvid": aavid, "ad_id": ad_id}
        )
        # 历史明细只用于补齐可查看范围；没有明确启用记录的旧计划
        # 不能自动进入追投/停投监控。
        enable_target = bool(existing_target and existing_target.get("enabled"))
        upsert_promotion_target(
            {
                "target_uid": uid,
                "aavid": aavid,
                "ad_id": ad_id,
                "plan_name": detail.get("plan_name") or "",
                "promotion_scene": scene,
                "plan_system": detail.get("plan_system") or "unknown",
                "enabled": enable_target,
            },
            db=store,
        )
        store.update(
            "pmc_ad_detail_basic",
            {
                "target_uid": uid,
                "promotion_scene": scene,
                "plan_system": detail.get("plan_system") or "unknown",
            },
            where={"aadvid": aavid, "ad_id": ad_id},
        )
        by_account.setdefault(aavid, []).append(
            {
                "target_uid": uid,
                "ad_id": ad_id,
                "promotion_scene": scene,
                "plan_system": detail.get("plan_system") or "unknown",
            }
        )
        migrated += 1

    for aavid, targets in by_account.items():
        # 旧素材没有 ad_id；仅当账户只有一条计划时才安全迁移。
        unique = {x["target_uid"]: x for x in targets}
        if len(unique) != 1:
            continue
        target = next(iter(unique.values()))
        for table, account_col, extra in (
            ("pmc_promotion_material", "aadvid", {"ad_id": target["ad_id"]}),
            ("pmc_retargeting_run", "aavid", {}),
            ("pmc_regulation_run", "aavid", {}),
            ("pmc_roi2_assist_task", "aadvid", {}),
            ("account_operation_event", "aavid", {}),
        ):
            values = {
                "target_uid": target["target_uid"],
                "promotion_scene": target["promotion_scene"],
                "plan_system": target.get("plan_system") or "unknown",
                **extra,
            }
            store.update(
                table,
                values,
                where=(
                    f"{account_col} = ? AND "
                    "(target_uid IS NULL OR target_uid = '' OR target_uid = ?)"
                ),
                params=(aavid, LEGACY_TARGET_UID),
            )
    return migrated
