"""
服务运行模块（GUI 调用）

职责：
- 以线程方式启动/停止抓取服务（避免阻塞 GUI）
- Headful 浏览器，允许用户手动登录千川
- 监控当前页面；直播从详情 URL 识别，商品全域从主计划接口只读识别
- 轮询抓取并入库 SQLite
- 服务管理配置见 data/control_panel.json（crawl / feishu_table / robot）
- 日志写入 data/service.log，并提供读取末尾 N 行的方法给前端展示
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional, Tuple, List
from urllib.parse import urlparse, parse_qs

from utils.log import logger as app_logger
from utils.sqlite_store import SQLiteStore
from services.fetcher import QianChuanFetcher, build_qianchuan_url_by_params, GlobalAuthExpiredError
from services.promotion_browser_lock import exclusive_browser_operation
from services.promotion_readonly_probe import PromotionReadOnlyProbe
from services.product_scene_adapter import scope_product_scene_snapshot
from services.plan_system import detect_plan_system, normalize_plan_system
from services.qianchuan_accounts import (
    ensure_qianchuan_account,
    list_qianchuan_accounts,
    migrate_existing_qianchuan_accounts,
    record_target_duration,
    schedulable_promotion_targets,
)
from services.qianchuan_session import (
    automation_session_ready,
    current_session_owner,
    load_qianchuan_storage_state,
    mark_qianchuan_session_available,
    mark_qianchuan_session_invalid,
    migrate_legacy_qcookie,
    save_context_storage_state,
    session_status as qianchuan_session_status,
)
from api.promotion_targets import (
    detect_confirmed_detail_scene,
    detect_promotion_scene,
    extract_plan_name,
    list_promotion_targets,
    patch_target_sync_state,
    record_target_verification_failure,
    replace_material_product_links,
    update_target_catalog_evidence,
    update_target_sync_state,
    upsert_products,
    upsert_promotion_target,
)
from services.control_panel_config import (
    load_scrape_service_config,
    save_scrape_service_config,
    snapshot_feishu_bitable_for_fetch,
    save_feishu_bitable_panel_config,
    load_feishu_bitable_panel_config,
)
from config import PROJECT_ROOT, DATA_DIR, LOGS_DIR, DB_FILE
from utils.common import browser_runtime_info, require_executable_path
from utils.log import logger


"""
注意：PROJECT_ROOT / DATA_DIR / LOGS_DIR 统一从 config.py 引用
"""


def _require_session_owner(expected_owner: str) -> None:
    current = current_session_owner()
    if not expected_owner or current != expected_owner:
        raise RuntimeError(
            "工具账号已经切换或退出，旧千川浏览器会话已安全停止"
        )


class CatalogLoginRequired(RuntimeError):
    """目录同步期间发现千川登录失效，必须整体关闭自动化会话门。"""


def _require_catalog_login(page: Any) -> None:
    if _is_qianchuan_login_url(getattr(page, "url", "")):
        raise CatalogLoginRequired("千川登录状态已失效，请重新登录")


# 轮询抓取阶段：浏览器持续运行超过此时长则关闭并用 Cookie 重建，缓解长时间运行内存增长（秒）
POLL_BROWSER_RECYCLE_INTERVAL_SEC = 2 * 3600
# 自动监控按“整轮开始到下一轮开始”最多 5 分钟调度；整轮本身过长时立即进入下一轮，
# 由九分钟容量预算和十分钟延迟告警负责降级，避免“整轮耗时 + 配置等待”叠加。
AUTO_MONITOR_INTERVAL_SEC = 5 * 60
LAST_TARGET_FILE = os.path.join(DATA_DIR, "last_crawl_target.json")
PROMOTION_PROBE_FILE = os.path.join(DATA_DIR, "promotion_readonly_probe.json")
_LAST_TARGET_LOCK = threading.Lock()


_WRITE_CAPABILITY_SCOPE_KEYS = (
    "retarget_scene",
    "retarget_plan_system",
    "retarget_probe_version",
    "retarget_verified_at",
    "retarget_target_uid",
    "retarget_aavid",
    "retarget_ad_id",
    "retarget_batch_execute",
    "retarget_batch_probe_version",
    "retarget_batch_verified_at",
    "regulation_scene",
    "regulation_plan_system",
    "regulation_probe_version",
    "regulation_verified_at",
    "regulation_target_uid",
    "regulation_aavid",
    "regulation_ad_id",
)


def _explicit_platform_status(page_text: Any, payload_status: Any = None) -> str:
    """Return a status only when the read-only response/page makes it explicit."""
    value = str(payload_status or "").strip()
    if value:
        return value
    text = str(page_text or "")
    for marker, status in (
        ("已删除", "deleted"),
        ("已结束", "ended"),
        ("历史计划", "historical"),
        ("已暂停", "paused"),
        ("暂停中", "paused"),
        ("投放中", "active"),
        ("生效中", "active"),
        ("学习中", "learning"),
        ("已启用", "enabled"),
    ):
        if marker in text:
            return status
    return "unknown"


def _persist_product_snapshot(
    db: SQLiteStore,
    target_uid: str,
    snapshot: Optional[Dict[str, Any]],
) -> None:
    if not isinstance(snapshot, dict):
        return
    plan = snapshot.get("plan") or {}
    scoped = scope_product_scene_snapshot(
        snapshot,
        ad_id=plan.get("ad_id") or "",
    )
    products = scoped.get("products") or []
    if scoped.get("ad_rows"):
        db.execute(
            "DELETE FROM promotion_material_product WHERE target_uid=?",
            (target_uid,),
        )
        db.execute(
            "DELETE FROM promotion_product WHERE target_uid=?",
            (target_uid,),
        )
    if products:
        upsert_products(target_uid, products, db=db)
    for material in scoped.get("materials") or []:
        if not isinstance(material, dict):
            continue
        material_id = str(material.get("material_id") or "").strip()
        product_ids = material.get("product_ids") or []
        if not material_id or not product_ids:
            continue
        replace_material_product_links(
            target_uid,
            material_id,
            product_ids,
            material_name=str(material.get("material_name") or ""),
            db=db,
        )


def _sync_discovered_product_targets(
    db: SQLiteStore,
    *,
    aavid: str,
    snapshot: Optional[Dict[str, Any]],
    owner_username: str,
    page_url: str,
) -> Dict[str, int]:
    """登记商品列表中的全部计划候选；新计划默认关闭自动监控。"""
    if not isinstance(snapshot, dict):
        return {"discovered": 0, "created": 0}
    aid = str(aavid or "").strip()
    if not aid.isdigit():
        return {"discovered": 0, "created": 0}
    existing_rows = {
        str(item.get("ad_id") or ""): item
        for item in list_promotion_targets(
            owner_username=owner_username,
            db=db,
        )
        if str(item.get("aadvid") or "") == aid
    }
    discovered = 0
    created = 0
    seen = set()
    for row in snapshot.get("ad_rows") or []:
        if not isinstance(row, dict):
            continue
        ad_id = str(row.get("ad_id") or "").strip()
        if not ad_id.isdigit() or ad_id in seen:
            continue
        seen.add(ad_id)
        discovered += 1
        existing = existing_rows.get(ad_id)
        if existing is None:
            created += 1
        target = upsert_promotion_target(
            {
                "aavid": aid,
                "ad_id": ad_id,
                "plan_name": str(row.get("ad_name") or "").strip()[:256],
                "promotion_scene": "product",
                "plan_system": str(row.get("plan_system") or "unknown"),
                "platform_status": str(row.get("platform_status") or "unknown"),
                "verification_state": "candidate",
                "page_url": page_url,
                # 商品列表里的 adInfos 可能是子广告或调控行；保留展示，
                # 但只有精确主计划详情核验后才允许恢复原勾选意图。
                "enabled": bool(existing.get("enabled")) if existing else False,
                "last_status": (
                    str(existing.get("last_status") or "pending")
                    if existing
                    else "pending"
                ),
                "last_error": (
                    str(existing.get("last_error") or "")
                    if existing
                    else ""
                ),
            },
            owner_username=owner_username,
            trusted_catalog=True,
            db=db,
        )
        _persist_product_snapshot(db, target["target_uid"], snapshot)
    return {"discovered": discovered, "created": created}


def _persist_verified_catalog_class(
    db: SQLiteStore,
    *,
    aavid: str,
    account_name: str,
    promotion_scene: str,
    plan_system: str,
    page_url: str,
    candidates: List[Dict[str, Any]],
    verification: Dict[str, Any],
    owner_username: str,
    class_complete: bool,
) -> Dict[str, int]:
    """Persist one catalog class without allowing list candidates to write-enable."""
    existing_rows = {
        str(item.get("ad_id") or ""): item
        for item in list_promotion_targets(
            owner_username=owner_username,
            db=db,
        )
        if str(item.get("aadvid") or "") == str(aavid)
    }
    verified_by_id = {
        str(item.get("ad_id") or ""): item
        for item in verification.get("verified") or []
        if isinstance(item, dict)
    }
    seen_ids = set()
    verified_count = 0
    candidate_count = 0
    for candidate in candidates:
        ad_id = str(candidate.get("ad_id") or "").strip()
        if not ad_id.isdigit():
            continue
        seen_ids.add(ad_id)
        exact = verified_by_id.get(ad_id)
        existing = existing_rows.get(ad_id) or {}
        is_verified = isinstance(exact, dict)
        source = exact if is_verified else candidate
        target = upsert_promotion_target(
            {
                "aavid": aavid,
                "account_name": account_name,
                "ad_id": ad_id,
                "plan_name": str(
                    source.get("plan_name")
                    or candidate.get("plan_name")
                    or ""
                ).strip()[:256],
                "promotion_scene": promotion_scene,
                "plan_system": plan_system,
                "platform_status": str(
                    source.get("platform_status")
                    or candidate.get("platform_status")
                    or "unknown"
                ),
                "verification_state": (
                    "verified" if is_verified else "candidate"
                ),
                "page_url": page_url,
                "enabled": bool(existing.get("enabled")) if existing else False,
                "last_status": str(
                    existing.get("last_status") or "pending"
                ),
                "last_error": (
                    ""
                    if is_verified
                    else str(existing.get("last_error") or "")
                ),
            },
            owner_username=owner_username,
            trusted_catalog=True,
            db=db,
        )
        if is_verified:
            verified_count += 1
            if promotion_scene == "product":
                _persist_product_snapshot(
                    db,
                    target["target_uid"],
                    source.get("detail_snapshot"),
                )
        else:
            candidate_count += 1

    # 只有无日期/状态排除的全部分页真正完成后，才把消失计划标成 missing。
    # 接口失败或分类入口缺失时保留原状态，避免误停用。
    if class_complete:
        for ad_id, existing in existing_rows.items():
            if (
                str(existing.get("promotion_scene") or "") != promotion_scene
                or str(existing.get("plan_system") or "") != plan_system
                or ad_id in seen_ids
            ):
                continue
            update_target_catalog_evidence(
                existing["target_uid"],
                platform_status=existing.get("platform_status") or "unknown",
                verification_state="missing",
                db=db,
            )
            db.update(
                "promotion_target",
                {"enabled": 0, "capacity_state": "disabled"},
                where={"target_uid": existing["target_uid"]},
            )
    return {
        "seen": len(seen_ids),
        "verified": verified_count,
        "candidates": candidate_count,
    }


def _ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


def _last_target_owner(owner_username: Any = None) -> str:
    owner = str(owner_username or current_session_owner() or "").strip().casefold()
    return owner or "local_default"


def _load_last_target(
    path: Optional[str] = None,
    owner_username: Any = None,
):
    target_path = path or LAST_TARGET_FILE
    scoped = path is None or owner_username is not None
    try:
        with _LAST_TARGET_LOCK:
            with open(target_path, "r", encoding="utf-8-sig") as handle:
                data = json.load(handle)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    migrate_legacy_scope = False
    if scoped:
        profiles = data.get("profiles")
        if isinstance(profiles, dict):
            data = profiles.get(_last_target_owner(owner_username))
            if not isinstance(data, dict):
                return None
        else:
            migrate_legacy_scope = True
    aavid = str(data.get("aavid") or "").strip()
    ad_id = str(data.get("adId") or data.get("ad_id") or "").strip()
    if not aavid.isdigit() or not ad_id.isdigit():
        return None
    if migrate_legacy_scope:
        _save_last_target(
            aavid,
            ad_id,
            target_path,
            owner_username=owner_username,
        )
    return {"aavid": aavid, "adId": ad_id}


def _save_last_target(
    aavid,
    ad_id,
    path: Optional[str] = None,
    owner_username: Any = None,
) -> bool:
    aavid_text = str(aavid or "").strip()
    ad_id_text = str(ad_id or "").strip()
    if not aavid_text.isdigit() or not ad_id_text.isdigit():
        return False
    target_path = path or LAST_TARGET_FILE
    scoped = path is None or owner_username is not None
    parent = os.path.dirname(os.path.abspath(target_path))
    os.makedirs(parent, exist_ok=True)
    temp_path = target_path + ".tmp"
    try:
        with _LAST_TARGET_LOCK:
            payload: Dict[str, Any]
            if scoped:
                try:
                    with open(target_path, "r", encoding="utf-8-sig") as handle:
                        existing_payload = json.load(handle)
                except Exception:
                    existing_payload = {}
                profiles = (
                    dict(existing_payload.get("profiles") or {})
                    if isinstance(existing_payload, dict)
                    else {}
                )
                profiles[_last_target_owner(owner_username)] = {
                    "aavid": aavid_text,
                    "adId": ad_id_text,
                }
                payload = {"version": 2, "profiles": profiles}
            else:
                payload = {"aavid": aavid_text, "adId": ad_id_text}
            with open(temp_path, "w", encoding="utf-8") as handle:
                json.dump(
                    payload,
                    handle,
                    ensure_ascii=False,
                    indent=2,
                )
            os.replace(temp_path, target_path)
        return True
    except Exception:
        try:
            if os.path.isfile(temp_path):
                os.remove(temp_path)
        except Exception:
            pass
        return False


def _reuse_last_target_enabled() -> bool:
    return os.getenv("QCSCKP_FORCE_TARGET_RESELECT", "").strip() != "1"


def _target_is_excluded(
    aavid: Any,
    ad_id: Any,
    excluded_target: Optional[Dict[str, Any]],
) -> bool:
    if not excluded_target:
        return False
    return (
        str(aavid or "").strip() == str(excluded_target.get("aavid") or "").strip()
        and str(ad_id or "").strip()
        == str(
            excluded_target.get("adId")
            or excluded_target.get("ad_id")
            or ""
        ).strip()
    )


def _promotion_target_key(aavid: Any, ad_id: Any) -> Tuple[str, str]:
    return (
        str(aavid or "").strip(),
        str(ad_id or "").strip(),
    )


def _known_promotion_target_keys(
    targets: List[Dict[str, Any]],
) -> set[Tuple[str, str]]:
    keys: set[Tuple[str, str]] = set()
    for target in targets:
        key = _promotion_target_key(
            target.get("aadvid") or target.get("aavid"),
            target.get("ad_id") or target.get("adId"),
        )
        if key[0] and key[1]:
            keys.add(key)
    return keys


def _is_qianchuan_login_url(url: Any) -> bool:
    text = str(url or "").strip()
    if not text:
        return False
    parsed = urlparse(text)
    if parsed.scheme in {"about", "data"}:
        return False
    host = str(parsed.netloc or "").lower()
    path = str(parsed.path or "").lower()
    return (
        host != "qianchuan.jinritemai.com"
        or "login" in path
        or "passport" in path
    )


async def _qianchuan_authenticated_shell_visible(page: Any) -> bool:
    """登录成功总闸：必须位于千川域名且出现登录后的账户导航。"""
    if page is None or _is_qianchuan_login_url(getattr(page, "url", "")):
        return False
    try:
        if await page.is_closed():
            return False
    except Exception:
        pass
    for selector in (
        "#navigator-right-account",
        ".qc-ui-navigator-account",
    ):
        try:
            locator = page.locator(selector).first
            if await locator.is_visible(timeout=1_000):
                return True
        except Exception:
            continue
    return False


def _choose_startup_target(
    known_targets: List[Dict[str, Any]],
    remembered_target: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    if remembered_target:
        remembered_aavid = str(remembered_target.get("aavid") or "").strip()
        remembered_ad_id = str(
            remembered_target.get("adId")
            or remembered_target.get("ad_id")
            or ""
        ).strip()
        for target in known_targets:
            if (
                str(target.get("aadvid") or target.get("aavid") or "").strip()
                == remembered_aavid
                and str(target.get("ad_id") or target.get("adId") or "").strip()
                == remembered_ad_id
            ):
                return target
        if remembered_aavid and remembered_ad_id:
            return {
                "aadvid": remembered_aavid,
                "ad_id": remembered_ad_id,
                "promotion_scene": "live",
                "plan_system": "unknown",
                "sanitized_page_url": "",
            }
    return known_targets[0] if known_targets else None


def _trusted_startup_discovery(
    startup_target: Optional[Dict[str, Any]],
    startup_url: str,
) -> Optional[Dict[str, Any]]:
    """Reuse a stored monitor target without trusting the product page's default plan."""
    if not startup_target:
        return None
    aavid = str(
        startup_target.get("aadvid") or startup_target.get("aavid") or ""
    ).strip()
    ad_id = str(
        startup_target.get("ad_id") or startup_target.get("adId") or ""
    ).strip()
    promotion_scene = str(
        startup_target.get("promotion_scene") or ""
    ).strip().lower()
    if not (aavid.isdigit() and ad_id.isdigit()):
        return None
    if promotion_scene not in {"live", "product"}:
        return None
    return {
        "url": str(
            startup_target.get("sanitized_page_url")
            or startup_target.get("page_url")
            or startup_url
            or ""
        ).strip(),
        "aavid": aavid,
        "ad_id": ad_id,
        "promotion_scene": promotion_scene,
        "plan_system": normalize_plan_system(
            startup_target.get("plan_system") or "unknown"
        ),
        "plan_name": str(startup_target.get("plan_name") or "").strip(),
        "snapshot": {},
    }


def _can_reuse_startup_target(
    *,
    storage_state_available: bool,
    reuse_last_target: bool,
    startup_target: Optional[Dict[str, Any]],
    current_url: Any,
) -> bool:
    """Never bypass visible login merely because an old target is remembered."""
    return bool(
        storage_state_available
        and reuse_last_target
        and startup_target
        and not _is_qianchuan_login_url(current_url)
    )


def _parse_query_and_fragment(url: str) -> dict:
    """
    千川很多参数会出现在 query 或 fragment（# 后面），这里合并解析。
    """
    parsed = urlparse(url)
    params = {}
    params.update({k: v[0] for k, v in parse_qs(parsed.query).items() if v})

    frag = parsed.fragment or ""
    # fragment 可能是 "a=1&b=2" 或带路径的形式，尽量 parse
    if frag:
        if "?" in frag:
            frag = frag.split("?", 1)[1]
        params.update({k: v[0] for k, v in parse_qs(frag).items() if v})
    return params


def _extract_aavid_adid(url: str) -> Tuple[Optional[str], Optional[str]]:
    params = _parse_query_and_fragment(url)
    aavid = params.get("aavid") or params.get("aavid".upper()) or params.get("aAvid")
    ad_id = params.get("adId") or params.get("ad_id") or params.get("adID")
    return aavid, ad_id


def _feishu_hourly_push_window_sync(
    db: SQLiteStore,
    app_token: str,
    personal_base_token: str,
    table_id: str,
    aadvid: Optional[str],
    target_uid: Optional[str],
    last_window_end: Optional[str],
    log_fn,
) -> Tuple[Optional[str], int]:
    """
    当前本地时间已过本小时整点时触发一次：从 SQLite 取「近 1 小时」内数据
    （created_at > datetime('now', '+8 hours', '-1 hours')），
    每个素材 ID 只取 id 最大的一条写入飞书；同一整点窗口不重复推送。
    """
    from services.feishu_bitable import BitableTable

    fmt = "%Y-%m-%d %H:%M:%S"
    now = datetime.now()
    hour_floor = now.replace(minute=0, second=0, microsecond=0)
    window_end = hour_floor
    window_end_str = window_end.strftime(fmt)
    if last_window_end and last_window_end >= window_end_str:
        return last_window_end, 0

    aid = (aadvid or "").strip() or None
    rows = db.select_pmc_latest_per_material_in_last_hour_utc8(
        aadvid=aid,
        target_uid=(target_uid or "").strip() or None,
    )
    if not rows:
        log_fn(
            "[飞书·整点] 周期内（created_at > datetime('now', '+8 hours', '-1 hours')）无数据，跳过同步"
        )
        return window_end_str, 0

    out_rows = []
    for r in rows:
        o = dict(r)
        o.pop("id", None)
        out_rows.append(o)

    try:
        BitableTable(app_token, personal_base_token, table_id).insert_pmc_material_rows(out_rows)
        log_fn(
            f"[飞书·整点] 已同步 {len(out_rows)} 条（近 1 小时、每素材取周期内最新；"
            f"条件 created_at > datetime('now', '+8 hours', '-1 hours')）"
        )
        return window_end_str, len(out_rows)
    except Exception as e:
        app_logger.warning(f"[飞书·整点] 同步失败（将下轮重试）: {e}")
        return last_window_end, 0


@dataclass
class ServiceConfig:
    interval: int = 600
    round_timeout: int = 600
    headless: bool = False  # 必须 False（有头）
    cookie_path: str = os.path.join(DATA_DIR, "qcookie.json")
    db_path: str = DB_FILE
    auto_start: bool = False
    wait_url_prefix: str = "https://qianchuan.jinritemai.com/uni-prom/deta"
    open_url: str = "https://qianchuan.jinritemai.com/login"
    base_url: str = "https://qianchuan.jinritemai.com/uni-prom/detail"

    def normalize_paths(self) -> "ServiceConfig":
        # cookie/db 支持相对项目根目录
        if self.cookie_path and not os.path.isabs(self.cookie_path):
            self.cookie_path = os.path.join(PROJECT_ROOT, self.cookie_path)
        if self.db_path and not os.path.isabs(self.db_path):
            self.db_path = os.path.join(PROJECT_ROOT, self.db_path)
        return self


class ServiceController:
    """
    GUI 可调用的服务控制器（线程 + 状态 + 配置 + 日志）
    """

    def __init__(self):
        _ensure_data_dir()

        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        self._phase: str = "stopped"  # stopped|starting|waiting_login|running|error
        self._message: str = ""
        self._last_target: dict = {}

        # 轮询阶段当前浏览器是否无头（与 control_panel.json → crawl 同步；切换时重启浏览器）
        self._active_poll_headless: Optional[bool] = None

        # 轮询抓取就绪（用于 status 等）
        self._fetch_ready = False
        self._last_fetch_time: float = 0  # 上次抓取完成的时间戳（秒）
        self._last_round_started_time: float = 0  # 上一轮自动监控开始时间
        self._last_catalog_sync_time: float = 0

        # 启动服务时校验通过的账号密码，仅内存保存，用于入库后云端备份 API（不写盘）
        self._cloud_backup_username: Optional[str] = None
        self._cloud_backup_password: str = ""
        self._target_discovery_thread: Optional[threading.Thread] = None
        self._target_discovery_login_only = False
        self._target_discovery_account_only = False
        self._target_discovery_launch_event = threading.Event()
        self._catalog_sync_thread: Optional[threading.Thread] = None
        self._catalog_scheduler_thread: Optional[threading.Thread] = None
        self._catalog_startup_sync_pending = True
        self._target_discovery_status: dict = {
            "success": True,
            "running": False,
            "message": "",
            "target": None,
            "account": None,
            "relogin_complete": False,
        }

        # 飞书「整点推送」：避免重复推同一小时窗口（进程内；重启后会从当前整点窗口重新判断）
        self._feishu_hourly_last_window_end: Optional[str] = None

    def set_cloud_backup_credentials(self, username: str, password: str) -> None:
        """由 Api.startService 在校验通过后调用，供每轮 fetch 同步云端。"""
        u = (username or "").strip()
        self._cloud_backup_username = u if u else None
        self._cloud_backup_password = password if password is not None else ""

    # ---------------- logs ----------------
    def _log(self, msg: str):
        with self._lock:
            self._message = msg
        try:
            app_logger.info(msg)
        except Exception:
            pass

    @staticmethod
    def _tail_lines(path: str, limit: int) -> List[str]:
        """
        高效读取文件末尾 N 行，避免大日志全量读取。
        """
        if limit <= 0:
            return []
        # 以二进制倒读，再按 utf-8 解码
        chunk_size = 8192
        data = b""
        lines: List[bytes] = []
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            pos = f.tell()
            while pos > 0 and len(lines) <= limit:
                read_size = chunk_size if pos >= chunk_size else pos
                pos -= read_size
                f.seek(pos, os.SEEK_SET)
                data = f.read(read_size) + data
                lines = data.splitlines()
        tail = lines[-limit:]
        return [b.decode("utf-8", errors="ignore") for b in tail]

    @staticmethod
    def _pick_latest_app_log() -> Optional[str]:
        """
        选择 logs/ 下最新的 app 日志文件：
        - 当前文件：logs/app
        - 轮转文件：logs/app.YYYYMMDD-HH
        """
        if not os.path.isdir(LOGS_DIR):
            return None
        candidates = []
        for name in os.listdir(LOGS_DIR):
            if name == "app" or name.startswith("app."):
                full = os.path.join(LOGS_DIR, name)
                if os.path.isfile(full):
                    try:
                        mtime = os.path.getmtime(full)
                    except Exception:
                        mtime = 0
                    candidates.append((mtime, full))
        if not candidates:
            return None
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1]

    def read_logs(self, limit: int = 300) -> dict:
        limit = int(limit) if limit else 300
        if limit <= 0:
            limit = 300
        try:
            latest = self._pick_latest_app_log()
            if not latest or not os.path.exists(latest):
                return {"success": True, "lines": []}
            return {"success": True, "lines": self._tail_lines(latest, limit)}
        except Exception as e:
            return {"success": False, "lines": [], "message": str(e)}

    def clear_logs(self) -> dict:
        """清空日志内容"""
        try:
            latest = self._pick_latest_app_log()
            if latest and os.path.exists(latest):
                with open(latest, 'w', encoding='utf-8') as f:
                    f.write('')
            return {"success": True}
        except Exception as e:
            return {"success": False, "message": str(e)}

    # ---------------- status ----------------
    def status(self) -> dict:
        with self._lock:
            running = self._thread is not None and self._thread.is_alive()
            try:
                scrape_status_cfg = load_scrape_service_config()
                interval = int(scrape_status_cfg.get("interval_seconds") or 60)
            except Exception:
                scrape_status_cfg = {}
                interval = 60
            interval = max(5, interval)
            browser_info = browser_runtime_info(
                str(scrape_status_cfg.get("browser_executable_path") or "").strip() or None
            )
            session = qianchuan_session_status()
            cookie_exists = bool(session.get("available"))
            cookie_updated_at = str(session.get("updated_at") or "")
            if self._phase == "waiting_login":
                login_status = "等待在可见Chrome中完成登录"
            elif cookie_exists:
                login_status = "已保存千川登录状态"
            else:
                login_status = "尚未保存千川登录状态"
            if self._phase == "running":
                browser_phase = (
                    "后台无头运行"
                    if self._active_poll_headless is not False
                    else "可见窗口运行"
                )
            elif self._phase in {"starting", "waiting_login"}:
                browser_phase = "可见窗口登录"
            else:
                browser_phase = "未运行"

            # 获取抓取进度（素材 + 可选调控任务）
            fetch_progress = None
            assist_progress = None
            if running and self._phase == "running" and hasattr(self, '_fetcher') and self._fetcher:
                try:
                    current = getattr(self._fetcher, '_material_current_count', 0) or 0
                    total = getattr(self._fetcher, '_material_total_count', 0) or 0
                    # 始终返回进度信息（即使为0），让前端能正确显示状态
                    fetch_progress = {"current": current, "total": total}

                    fetch_assist = bool(load_scrape_service_config().get("fetch_assist_tasks"))
                    if fetch_assist:
                        ac = getattr(self._fetcher, "_assist_current_count", 0) or 0
                        at = getattr(self._fetcher, "_assist_total_count", 0) or 0
                        is_as = bool(getattr(self._fetcher, "_is_assist_collecting", False))
                        if is_as or ac > 0 or at > 0:
                            assist_progress = {"current": ac, "total": at, "active": is_as}

                    # 更新 message 包含进度
                    if current > 0 or total > 0:
                        self._message = f"抓取中（素材 {current}/{total}"
                        if assist_progress:
                            self._message += f"；调控 {assist_progress['current']}/{assist_progress['total']}"
                        self._message += "）"
                    else:
                        self._message = "抓取中（等待数据...）"
                except Exception:
                    pass

            return {
                "success": True,
                "running": running,
                "phase": self._phase,
                "message": self._message,
                "target": self._last_target,
                "lastFetchTime": self._last_fetch_time,
                "interval": interval,
                "fetchProgress": fetch_progress,
                "assistProgress": assist_progress,
                "browser": {
                    **browser_info,
                    "phase": browser_phase,
                },
                "qianchuanLogin": {
                    "status": login_status,
                    "cookie_saved": cookie_exists,
                    "cookie_updated_at": cookie_updated_at,
                    "encrypted": bool(session.get("encrypted")),
                    "owner_username": str(session.get("owner_username") or ""),
                },
            }

    # ---------------- interval 配置 ----------------
    def setInterval(self, interval: int) -> dict:
        """
        更新轮询间隔（写入 control_panel.json → crawl，在下一轮等待起算时生效）
        """
        try:
            interval = int(interval)
            if interval < 5:
                return {"success": False, "message": "间隔不能小于5秒"}
            save_scrape_service_config(interval_seconds=interval)
            return {"success": True, "interval": interval}
        except Exception as e:
            return {"success": False, "message": str(e)}

    @staticmethod
    def _effective_interval_sec(cfg: ServiceConfig) -> int:
        """自动监控间隔（秒）：允许更快调试，但正式轮询不会慢于每5分钟启动一轮。"""
        try:
            base = int(load_scrape_service_config().get("interval_seconds") or cfg.interval)
        except Exception:
            base = cfg.interval
        return max(5, min(base, AUTO_MONITOR_INTERVAL_SEC))

    def setFeishuBitableConfig(
        self,
        app_token: Optional[str] = None,
        personal_base_token: Optional[str] = None,
        table_id: Optional[str] = None,
        enabled: Optional[bool] = None,
        push_mode: Optional[str] = None,
    ) -> dict:
        """更新飞书 Base 配置（写入 control_panel.json → feishu_table，每轮抓取前读取）。"""
        try:
            save_feishu_bitable_panel_config(
                enabled=enabled,
                app_token=app_token,
                personal_base_token=personal_base_token,
                table_id=table_id,
                push_mode=push_mode,
            )
            return {"success": True}
        except Exception as e:
            return {"success": False, "message": str(e)}

    async def _maybe_feishu_hourly_push_after_fetch(
        self,
        db: SQLiteStore,
        fa: Optional[str],
        fp: Optional[str],
        ft: Optional[str],
        aadvid: Optional[str],
        target_uid: Optional[str] = None,
    ) -> None:
        cfg = load_feishu_bitable_panel_config()
        if cfg.get("push_mode") != "hourly_latest":
            return
        if not (fa and fp and ft):
            return

        def _run():
            return _feishu_hourly_push_window_sync(
                db,
                fa,
                fp,
                ft,
                aadvid,
                target_uid,
                self._feishu_hourly_last_window_end,
                self._log,
            )

        new_last, _n = await asyncio.to_thread(_run)
        self._feishu_hourly_last_window_end = new_last

    # ---------------- lifecycle ----------------
    def start(self) -> dict:
        """启动采集线程；轮询间隔与无头模式以 control_panel.json → crawl 为准（由 Api.startService 在调用前写入）。"""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                self._message = "服务已在运行"
                return self.status()

            self._stop_event.clear()
            self._fetch_ready = False
            self._phase = "starting"
            self._message = "启动中..."

            self._thread = threading.Thread(target=self._thread_entry, daemon=True)
            self._thread.start()
        self._log("[服务] 已发起启动")
        return self.status()

    def stop(self) -> dict:
        self._stop_event.set()
        self._log("[服务] 已发起停止")
        return self.status()

    def stop_and_wait(self, timeout_seconds: float = 30.0) -> dict:
        """账号切换/退出时等待旧浏览器线程退出，禁止两套会话重叠。"""
        self._stop_event.set()
        thread = self._thread
        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout=max(0.0, float(timeout_seconds)))
        status = self.status()
        if status.get("running"):
            status["success"] = False
            status["message"] = "旧千川会话仍在安全退出，请稍后重试"
        return status

    def start_target_discovery(
        self,
        *,
        login_only: bool = False,
        account_only: bool = False,
    ) -> dict:
        """打开独立有头浏览器，执行重新登录、选择账户或旧版计划识别。"""
        if login_only and account_only:
            return {
                "success": False,
                "running": False,
                "message": "登录核验和账户选择不能同时启动",
            }
        with self._lock:
            if (
                self._target_discovery_thread is not None
                and self._target_discovery_thread.is_alive()
            ):
                logger.info(
                    "[千川可见登录] 复用正在运行的浏览器任务 "
                    "login_only=%s account_only=%s status=%s",
                    self._target_discovery_login_only,
                    self._target_discovery_account_only,
                    self._target_discovery_status.get("message"),
                )
                return {
                    "success": True,
                    **self._target_discovery_status,
                }
            self._target_discovery_launch_event.clear()
            self._target_discovery_status = {
                "success": True,
                "running": True,
                "message": "正在启动独立Google Chrome，请稍候…",
                "target": None,
                "account": None,
                "relogin_complete": False,
            }
            self._target_discovery_login_only = bool(login_only)
            self._target_discovery_account_only = bool(account_only)
            self._target_discovery_thread = threading.Thread(
                target=self._target_discovery_entry,
                args=(
                    (bool(login_only), bool(account_only))
                    if account_only
                    else (bool(login_only),)
                ),
                name="promotion-target-discovery",
                daemon=True,
            )
            self._target_discovery_thread.start()
        logger.info(
            "[千川可见登录] 已收到启动请求 login_only=%s account_only=%s",
            bool(login_only),
            bool(account_only),
        )
        self._target_discovery_launch_event.wait(timeout=8.0)
        with self._lock:
            result = dict(self._target_discovery_status)
        if (
            result.get("running")
            and not self._target_discovery_launch_event.is_set()
        ):
            result["message"] = (
                "Google Chrome仍在启动；若10秒后仍未出现，"
                "请查看页面上的失败原因并重试"
            )
        return result

    def target_discovery_status(self) -> dict:
        with self._lock:
            return dict(self._target_discovery_status)

    def start_catalog_sync(self) -> dict:
        """Start the independent read-only all-account catalog scanner."""
        from services.qianchuan_catalog import (
            catalog_sync_status,
            mark_catalog_sync_started,
        )

        with self._lock:
            running = (
                self._catalog_sync_thread is not None
                and self._catalog_sync_thread.is_alive()
            )
            visible_browser_running = (
                self._target_discovery_thread is not None
                and self._target_discovery_thread.is_alive()
            )
        if visible_browser_running:
            relogin_running = bool(self._target_discovery_login_only)
            return {
                "success": False,
                "running": False,
                "message": (
                    "正在可见Chrome中确认千川登录，完成后再同步目录"
                    if relogin_running
                    else "正在可见Chrome中选择账户，完成后再同步目录"
                ),
                "failure_kind": (
                    "relogin_in_progress"
                    if relogin_running
                    else "account_selection_in_progress"
                ),
            }
        if running:
            return catalog_sync_status()
        owner = current_session_owner()
        if not owner:
            return {
                "success": False,
                "message": "请先登录工具账号，再同步千川账户计划目录",
            }
        session_gate = automation_session_ready()
        if not session_gate.get("ready"):
            return {
                "success": False,
                "message": str(
                    session_gate.get("message")
                    or "请先在可见Chrome完成一次千川登录"
                ),
                "failure_kind": "login_required",
                "recovery_action": "open_visible_chrome",
            }
        cfg = ServiceConfig().normalize_paths()
        if not list_qianchuan_accounts(
            owner_username=owner,
            db=SQLiteStore(database=cfg.db_path),
        ):
            return {
                "success": False,
                "running": False,
                "message": "尚未添加千川账户，请先选择并添加当前账户",
                "failure_kind": "account_required",
            }
        mark_catalog_sync_started(
            owner_username=owner
        )
        with self._lock:
            self._catalog_sync_thread = threading.Thread(
                target=self._catalog_sync_entry,
                args=(owner,),
                name="qianchuan-catalog-sync",
                daemon=True,
            )
            self._catalog_sync_thread.start()
        return catalog_sync_status()

    def catalog_sync_status(self) -> dict:
        from services.qianchuan_catalog import catalog_sync_status

        base = catalog_sync_status()
        return base

    def start_catalog_scheduler(self) -> None:
        with self._lock:
            if (
                self._catalog_scheduler_thread is not None
                and self._catalog_scheduler_thread.is_alive()
            ):
                return
            self._catalog_scheduler_thread = threading.Thread(
                target=self._catalog_scheduler_entry,
                name="qianchuan-catalog-scheduler",
                daemon=True,
            )
            self._catalog_scheduler_thread.start()

    def _catalog_scheduler_entry(self) -> None:
        while True:
            try:
                from services.qianchuan_catalog import catalog_sync_due

                if (
                    current_session_owner()
                    and load_qianchuan_storage_state()
                    and (
                        self._catalog_startup_sync_pending
                        or catalog_sync_due()
                    )
                ):
                    started = self.start_catalog_sync()
                    if started.get("success"):
                        self._catalog_startup_sync_pending = False
            except Exception:
                pass
            time.sleep(60)

    def _catalog_sync_entry(self, owner_username: str) -> None:
        try:
            asyncio.run(
                self._catalog_sync_async(owner_username=owner_username)
            )
        except CatalogLoginRequired as exc:
            from services.qianchuan_catalog import finalize_catalog_sync

            with self._lock:
                visible_login_running = (
                    self._target_discovery_thread is not None
                    and self._target_discovery_thread.is_alive()
                    and self._target_discovery_login_only
                )
            if visible_login_running:
                finalize_catalog_sync(
                    owner_username=owner_username,
                    error=(
                        "旧目录同步检测到登录异常，但可见Chrome正在重新登录；"
                        "已忽略旧会话结果"
                    ),
                )
                logger.info(
                    "[千川目录] 可见Chrome重新登录进行中，"
                    "忽略旧目录同步的登录失效结果"
                )
                return
            mark_qianchuan_session_invalid(
                str(exc),
                owner_username=owner_username,
            )
            finalize_catalog_sync(
                owner_username=owner_username,
                error=str(exc),
            )
        except Exception as exc:
            from services.qianchuan_catalog import finalize_catalog_sync

            finalize_catalog_sync(
                owner_username=owner_username,
                error=f"账户计划目录同步失败：{exc}",
            )

    async def _wait_catalog_class(
        self,
        probe: PromotionReadOnlyProbe,
        *,
        aavid: str,
        promotion_scene: str,
        plan_system: str,
        timeout_seconds: float = 18.0,
    ) -> Dict[str, Any]:
        deadline = time.monotonic() + max(3.0, float(timeout_seconds))
        last = {}
        while time.monotonic() < deadline:
            await probe.wait_for_product_pagination(timeout=0.5)
            last = probe.catalog_class_status(
                aavid=aavid,
                promotion_scene=promotion_scene,
                plan_system=plan_system,
            )
            if last.get("complete"):
                break
            await asyncio.sleep(0.35)
        return last or probe.catalog_class_status(
            aavid=aavid,
            promotion_scene=promotion_scene,
            plan_system=plan_system,
        )

    @staticmethod
    async def _click_visible_exact(
        page: Any,
        texts: List[str],
        *,
        timeout_ms: int = 8_000,
    ) -> bool:
        for text in texts:
            locator = page.get_by_text(text, exact=True)
            try:
                await locator.last.wait_for(
                    state="visible",
                    timeout=timeout_ms,
                )
                await locator.last.click()
                return True
            except Exception:
                continue
        return False

    @staticmethod
    async def _open_explicit_chengfang_catalog(
        page: Any,
        *,
        aavid: str,
    ) -> bool:
        """Open Chengfang only from an explicit clickable platform label."""
        entry_texts = ["千川乘方", "乘方投放", "乘方计划"]
        roles = ["tab", "link", "button", "menuitem"]
        for pass_index in range(2):
            if pass_index:
                await page.goto(
                    "https://qianchuan.jinritemai.com/home"
                    f"?aavid={aavid}",
                    wait_until="domcontentloaded",
                    timeout=60_000,
                )
                _require_catalog_login(page)
            for text in entry_texts:
                for role in roles:
                    locator = page.get_by_role(role, name=text, exact=True)
                    try:
                        count = await locator.count()
                    except Exception:
                        count = 0
                    for index in range(count - 1, -1, -1):
                        candidate = locator.nth(index)
                        try:
                            if not await candidate.is_visible():
                                continue
                            await candidate.click()
                            try:
                                await page.wait_for_load_state(
                                    "domcontentloaded",
                                    timeout=15_000,
                                )
                            except Exception:
                                pass
                            await page.wait_for_timeout(1_000)
                            page_text = await page.locator("body").inner_text(
                                timeout=5_000
                            )
                            visible_lines = {
                                " ".join(line.split())
                                for line in page_text.splitlines()
                                if line.strip()
                            }
                            if (
                                detect_plan_system(page_text=page_text)
                                == "chengfang"
                                and visible_lines.intersection(
                                    {
                                        "千川乘方",
                                        "乘方投放",
                                        "乘方计划",
                                        "千川乘方投放",
                                    }
                                )
                            ):
                                return True
                        except Exception:
                            continue
        return False

    async def _scan_catalog_class(
        self,
        *,
        fetcher: QianChuanFetcher,
        probe: PromotionReadOnlyProbe,
        db: SQLiteStore,
        owner_username: str,
        account: Dict[str, str],
        promotion_scene: str,
        plan_system: str,
        page_url: str,
    ) -> Dict[str, Any]:
        aavid = str(account["aavid"])
        _require_catalog_login(fetcher.page)
        # 调用方必须在导航/切换分类前设置探针上下文。列表响应通常会在
        # 页面加载或点击页签后立刻返回；这里若再次 reset，会把刚收到的
        # 全部分页证据清掉，最终错误地显示“没有计划”。
        status = await self._wait_catalog_class(
            probe,
            aavid=aavid,
            promotion_scene=promotion_scene,
            plan_system=plan_system,
        )
        _require_catalog_login(fetcher.page)
        candidates = probe.catalog_rows(
            aavid=aavid,
            promotion_scene=promotion_scene,
            plan_system=plan_system,
        )
        _require_catalog_login(fetcher.page)
        verification = await probe.verify_catalog_plans(
            fetcher.page,
            aavid=aavid,
            promotion_scene=promotion_scene,
            plan_system=plan_system,
        )
        _require_catalog_login(fetcher.page)
        _require_session_owner(owner_username)
        persisted = _persist_verified_catalog_class(
            db,
            aavid=aavid,
            account_name=str(account.get("account_name") or ""),
            promotion_scene=promotion_scene,
            plan_system=plan_system,
            page_url=page_url,
            candidates=candidates,
            verification=verification,
            owner_username=owner_username,
            class_complete=bool(status.get("complete")),
        )
        complete = bool(status.get("complete")) and bool(
            verification.get("complete")
        )
        message = str(status.get("message") or "")
        if status.get("complete") and not verification.get("complete"):
            message = (
                f"{len(verification.get('rejected') or [])} 条候选未通过"
                "账户＋精确计划详情核验"
            )
        return {
            **persisted,
            "complete": complete,
            "message": message,
        }

    async def _scan_global_account_catalog(
        self,
        *,
        fetcher: QianChuanFetcher,
        probe: PromotionReadOnlyProbe,
        db: SQLiteStore,
        owner_username: str,
        account: Dict[str, str],
    ) -> Dict[str, Any]:
        aavid = str(account["aavid"])
        classes: Dict[str, Dict[str, Any]] = {}
        probe.reset_catalog_class(
            aavid=aavid,
            promotion_scene="product",
            plan_system="global",
        )
        probe.set_catalog_context(
            aavid=aavid,
            promotion_scene="product",
            plan_system="global",
        )
        page_url = f"https://qianchuan.jinritemai.com/uni-prom?aavid={aavid}"
        await fetcher.page.goto(
            page_url,
            wait_until="domcontentloaded",
            timeout=60_000,
        )
        _require_catalog_login(fetcher.page)
        try:
            title = fetcher.page.get_by_text("全域投放", exact=True)
            await title.last.wait_for(state="visible", timeout=20_000)
            page_text = await fetcher.page.locator("body").inner_text(
                timeout=5_000
            )
        except Exception as exc:
            raise RuntimeError(f"未找到明确的全域投放页面证据：{exc}") from exc
        if detect_plan_system(page_text=page_text) != "global":
            raise RuntimeError("当前页面无法明确确认是全域计划体系")
        classes["global_product"] = await self._scan_catalog_class(
            fetcher=fetcher,
            probe=probe,
            db=db,
            owner_username=owner_username,
            account=account,
            promotion_scene="product",
            plan_system="global",
            page_url=page_url,
        )

        probe.reset_catalog_class(
            aavid=aavid,
            promotion_scene="live",
            plan_system="global",
        )
        probe.set_catalog_context(
            aavid=aavid,
            promotion_scene="live",
            plan_system="global",
        )
        opened_live = await self._click_visible_exact(
            fetcher.page,
            ["推直播间", "推直播"],
        )
        _require_catalog_login(fetcher.page)
        if opened_live:
            classes["global_live"] = await self._scan_catalog_class(
                fetcher=fetcher,
                probe=probe,
                db=db,
                owner_username=owner_username,
                account=account,
                promotion_scene="live",
                plan_system="global",
                page_url=str(fetcher.page.url or page_url),
            )
        else:
            classes["global_live"] = {
                "complete": False,
                "message": "未找到推直播计划目录入口",
            }

        # 乘方只从千川页面上明确可点击的“千川乘方/乘方投放/乘方计划”
        # 入口进入。没有该证据就保持目录不完整，绝不按 URL 或计划名称猜测。
        probe.reset_catalog_class(
            aavid=aavid,
            promotion_scene="product",
            plan_system="chengfang",
        )
        probe.set_catalog_context(
            aavid=aavid,
            promotion_scene="product",
            plan_system="chengfang",
        )
        opened_chengfang = await self._open_explicit_chengfang_catalog(
            fetcher.page,
            aavid=aavid,
        )
        _require_catalog_login(fetcher.page)
        if opened_chengfang:
            await self._click_visible_exact(
                fetcher.page,
                ["推商品", "商品自选"],
                timeout_ms=3_000,
            )
            classes["chengfang_product"] = await self._scan_catalog_class(
                fetcher=fetcher,
                probe=probe,
                db=db,
                owner_username=owner_username,
                account=account,
                promotion_scene="product",
                plan_system="chengfang",
                page_url=str(fetcher.page.url or ""),
            )
            probe.reset_catalog_class(
                aavid=aavid,
                promotion_scene="live",
                plan_system="chengfang",
            )
            probe.set_catalog_context(
                aavid=aavid,
                promotion_scene="live",
                plan_system="chengfang",
            )
            opened_chengfang_live = await self._click_visible_exact(
                fetcher.page,
                ["推直播间", "推直播"],
            )
            _require_catalog_login(fetcher.page)
            if opened_chengfang_live:
                classes["chengfang_live"] = await self._scan_catalog_class(
                    fetcher=fetcher,
                    probe=probe,
                    db=db,
                    owner_username=owner_username,
                    account=account,
                    promotion_scene="live",
                    plan_system="chengfang",
                    page_url=str(fetcher.page.url or ""),
                )
            else:
                classes["chengfang_live"] = {
                    "complete": False,
                    "message": "千川乘方页面未找到推直播计划目录入口",
                }
        else:
            classes["chengfang_product"] = {
                "complete": False,
                "message": "未取得千川乘方·推商品目录的明确页面证据",
            }
            classes["chengfang_live"] = {
                "complete": False,
                "message": "未取得千川乘方·推直播目录的明确页面证据",
            }
        return {
            "complete": all(
                bool(item.get("complete")) for item in classes.values()
            ),
            "classes": classes,
            "error": "；".join(
                f"{key}: {item.get('message')}"
                for key, item in classes.items()
                if not item.get("complete") and item.get("message")
            )[:2000],
        }

    async def _catalog_sync_async(self, *, owner_username: str) -> None:
        from services.qianchuan_catalog import (
            finalize_catalog_sync,
            mark_catalog_sync_progress,
        )

        owner = str(owner_username or "").strip().casefold()
        if not owner:
            raise RuntimeError("工具账号未登录")
        _require_session_owner(owner)
        state = load_qianchuan_storage_state(owner_username=owner)
        if not state:
            raise RuntimeError("千川登录状态不存在")
        cfg = ServiceConfig().normalize_paths()
        db = SQLiteStore(database=cfg.db_path)
        accounts = list_qianchuan_accounts(
            owner_username=owner,
            db=db,
        )
        fetcher = QianChuanFetcher(headless=True, storage_state=state)
        account_results: Dict[str, Dict[str, Any]] = {}
        async with exclusive_browser_operation(
            "已添加账户计划目录同步",
            timeout_seconds=1800,
        ):
            await fetcher._init_browser()
            probe = PromotionReadOnlyProbe(PROMOTION_PROBE_FILE)
            probe.attach(fetcher.page)
            try:
                await fetcher.page.goto(
                    "https://qianchuan.jinritemai.com/home",
                    wait_until="domcontentloaded",
                    timeout=60_000,
                )
                _require_catalog_login(fetcher.page)
                _require_session_owner(owner)
                if not accounts:
                    finalize_catalog_sync(
                        owner_username=owner,
                        account_results={},
                        error="尚未添加千川账户；请先选择当前账户并添加",
                        db=db,
                    )
                    return
                # 只迁移用户已经明确添加的账户。右上角授权账户列表不再用于
                # 自动建目录，也不会把同一登录身份下的全部 aavid 写入工具。
                migrate_existing_qianchuan_accounts(
                    owner_username=owner,
                    authorized_aavids={
                        str(item.get("aavid") or "") for item in accounts
                    },
                    db=db,
                )
                total = len(accounts)
                for index, account in enumerate(accounts, 1):
                    _require_session_owner(owner)
                    mark_catalog_sync_progress(
                        processed_accounts=index - 1,
                        total_accounts=total,
                        current_account=account.get("account_name"),
                        message=(
                            f"正在同步 {index}/{total}："
                            f"{account.get('account_name') or account.get('aavid')}"
                        ),
                    )
                    account_row = ensure_qianchuan_account(
                        account["aavid"],
                        account_name=account.get("account_name") or "",
                        owner_username=owner,
                        seen=True,
                        db=db,
                    )
                    try:
                        result = await self._scan_global_account_catalog(
                            fetcher=fetcher,
                            probe=probe,
                            db=db,
                            owner_username=owner,
                            account=account,
                        )
                    except CatalogLoginRequired:
                        raise
                    except Exception as exc:
                        result = {
                            "complete": False,
                            "classes": {},
                            "error": str(exc)[:2000],
                        }
                    account_results[str(account_row["account_uid"])] = result
                    mark_catalog_sync_progress(
                        processed_accounts=index,
                        total_accounts=total,
                        current_account=account.get("account_name"),
                        message=f"已处理 {index}/{total} 个已添加账户",
                    )
                _require_session_owner(owner)
                await save_context_storage_state(
                    fetcher.context,
                    owner_username=owner,
                )
            finally:
                await fetcher.close()
        finalize_catalog_sync(
            owner_username=owner,
            account_results=account_results,
            error="",
            db=db,
        )

    def _target_discovery_entry(
        self,
        login_only: bool = False,
        account_only: bool = False,
    ) -> None:
        try:
            asyncio.run(
                self._target_discovery_async(
                    login_only=login_only,
                    account_only=account_only,
                )
            )
        except Exception as e:
            logger.exception(
                "[千川可见登录] 浏览器启动或识别失败 "
                "login_only=%s account_only=%s",
                bool(login_only),
                bool(account_only),
            )
            with self._lock:
                self._target_discovery_status = {
                    "success": False,
                    "running": False,
                    "message": f"识别失败：{e}",
                    "target": None,
                    "account": None,
                    "relogin_complete": False,
                }
            self._target_discovery_launch_event.set()

    async def _target_discovery_async(
        self,
        *,
        login_only: bool = False,
        account_only: bool = False,
    ) -> None:
        # 这是用户控制的只读Chrome，可能停留10分钟等待选账户/计划；不长期占用
        # 自动化队列，否则会阻塞已确认追投。实际采集和所有写操作仍走全局锁。
        session_owner = current_session_owner()
        if not session_owner:
            raise RuntimeError("请先登录工具账号，再识别千川账户和计划")
        cfg = ServiceConfig().normalize_paths()
        db = SQLiteStore(database=cfg.db_path)
        migrate_legacy_qcookie()
        known_targets = list_promotion_targets(
            owner_username=session_owner,
            db=db,
        )
        selected_accounts = list_qianchuan_accounts(
            owner_username=session_owner,
            db=db,
        )
        selected_account_uids = {
            str(item.get("account_uid") or "") for item in selected_accounts
        }
        selected_aavids = {
            str(item.get("aavid") or "")
            for item in selected_accounts
            if str(item.get("aavid") or "").isdigit()
        }
        # 已登记但尚未经过 rc27 详情核验的旧计划，允许用户重新打开详情
        # 完成验证；只有已验证主计划才提示“已在列表中”。
        known_verified_target_keys = {
            _promotion_target_key(
                str(item.get("aadvid") or ""),
                str(item.get("ad_id") or ""),
            )
            for item in known_targets
            if str(item.get("account_uid") or "") in selected_account_uids
            if str(item.get("verification_state") or "").strip().lower()
            == "verified"
        }
        storage_state = load_qianchuan_storage_state(
            owner_username=session_owner
        )
        fetcher = QianChuanFetcher(headless=False, storage_state=storage_state)
        browser_path = require_executable_path(
            str(
                load_scrape_service_config().get(
                    "browser_executable_path"
                )
                or ""
            ).strip()
            or None
        )
        logger.info(
            "[千川可见登录] 正在启动Google Chrome path=%s "
            "login_only=%s account_only=%s",
            browser_path,
            bool(login_only),
            bool(account_only),
        )
        await fetcher._init_browser()
        logger.info("[千川可见登录] Google Chrome进程已创建")
        probe = PromotionReadOnlyProbe(PROMOTION_PROBE_FILE)
        probe.attach(fetcher.page)
        # 已有登录态时先进入计划列表，避免登录页自动跳回上次打开的旧计划并被误登记。
        discovery_start_url = (
            "https://qianchuan.jinritemai.com/uni-prom"
            if storage_state
            else cfg.open_url
        )
        try:
            await fetcher.page.goto(
                discovery_start_url,
                wait_until="domcontentloaded",
                timeout=60_000,
            )
        except Exception:
            pass
        try:
            await fetcher.page.bring_to_front()
        except Exception:
            pass
        with self._lock:
            self._target_discovery_status["message"] = (
                "独立Google Chrome已打开；请完成千川登录，"
                "工具会自动确认授权账户并保存会话"
                if login_only
                else (
                    "独立Google Chrome已打开；请切换到要添加的千川账户。"
                    "识别稳定后会自动保存该账户并关闭Chrome，不需要进入计划详情"
                    if account_only
                    else
                    "独立Google Chrome已打开；如显示登录页，请先登录千川，"
                    "再进入要监控的计划详情"
                )
            )
        self._target_discovery_launch_event.set()
        try:
            target_url = None
            target_scene = None
            target_plan_system = "unknown"
            target_page_text = ""
            product_snapshot: Dict[str, Any] = {}
            stable_candidate = None
            stable_count = 0
            last_probe_at = 0.0
            last_ignored_existing = None
            deadline = time.time() + 600
            while time.time() < deadline:
                try:
                    if fetcher.context and fetcher.context.pages:
                        fetcher.page = fetcher.context.pages[-1]
                except Exception:
                    pass
                cur = str(getattr(fetcher.page, "url", "") or "").strip()
                if _is_qianchuan_login_url(cur):
                    with self._lock:
                        self._target_discovery_status["message"] = (
                            "等待千川登录：请在新Chrome完成登录，"
                            + (
                                "工具会自动确认授权账户并保存会话"
                                if login_only
                                else (
                                    "然后切换到要添加的千川账户"
                                    if account_only
                                    else "然后进入要监控的计划详情"
                                )
                            )
                        )
                    stable_candidate = None
                    stable_count = 0
                    await asyncio.sleep(0.5)
                    continue
                if time.time() - last_probe_at >= 1.0:
                    await probe.observe_page(fetcher.page)
                    last_probe_at = time.time()
                if login_only:
                    try:
                        page_closed = await fetcher.page.is_closed()
                    except Exception:
                        page_closed = False
                    if page_closed:
                        raise RuntimeError(
                            "登录Chrome已关闭，尚未完成千川登录确认"
                        )
                    authenticated = (
                        await _qianchuan_authenticated_shell_visible(
                            fetcher.page
                        )
                    )
                    accounts_loader = getattr(
                        probe,
                        "authorized_accounts",
                        None,
                    )
                    accounts = (
                        accounts_loader()
                        if callable(accounts_loader)
                        else []
                    )
                    if not authenticated and not accounts:
                        accounts = await probe.discover_authorized_accounts(
                            fetcher.page,
                            timeout_ms=4_000,
                        )
                        authenticated = bool(accounts)
                    if authenticated:
                        _require_session_owner(session_owner)
                        if fetcher.context:
                            await save_context_storage_state(
                                fetcher.context,
                                owner_username=session_owner,
                            )
                        mark_qianchuan_session_available(
                            owner_username=session_owner,
                        )
                        from services.qianchuan_catalog import (
                            clear_catalog_login_failure,
                        )

                        clear_catalog_login_failure(
                            owner_username=session_owner,
                            db=db,
                        )
                        with self._lock:
                            self._target_discovery_status = {
                                "success": True,
                                "running": False,
                                "message": (
                                    "千川重新登录成功，会话已加密保存；"
                                    "登录Chrome已自动关闭。不会自动添加授权账户，"
                                    "请按需选择并添加当前账户"
                                ),
                                "target": None,
                                "account": None,
                                "relogin_complete": True,
                            }
                        logger.info(
                            "[千川可见登录] 登录成功，Cookie已保存，"
                            "将关闭可见Chrome accounts=%s",
                            len(accounts),
                        )
                        return
                if account_only:
                    latest_aavid_loader = getattr(
                        probe,
                        "latest_observed_aavid",
                        None,
                    )
                    latest_aavid = (
                        str(latest_aavid_loader() or "").strip()
                        if callable(latest_aavid_loader)
                        else ""
                    )
                    if latest_aavid in selected_aavids:
                        stable_candidate = None
                        stable_count = 0
                        with self._lock:
                            self._target_discovery_status["message"] = (
                                "当前千川账户已经添加；请在Chrome右上角切换到"
                                "另一个要添加的账户。识别后窗口会自动关闭"
                            )
                        await asyncio.sleep(0.5)
                        continue
                    if latest_aavid.isdigit():
                        candidate = ("account", latest_aavid)
                        if candidate == stable_candidate:
                            stable_count += 1
                        else:
                            stable_candidate = candidate
                            stable_count = 1
                    else:
                        stable_candidate = None
                        stable_count = 0
                    if stable_count < 3:
                        with self._lock:
                            self._target_discovery_status["message"] = (
                                "正在识别Chrome当前千川账户；只需完成账户切换，"
                                "不需要进入计划详情"
                            )
                        await asyncio.sleep(0.5)
                        continue

                    account_name = ""
                    account_name_loader = getattr(
                        probe,
                        "current_account_name",
                        None,
                    )
                    if callable(account_name_loader):
                        try:
                            account_name = str(
                                await account_name_loader(fetcher.page) or ""
                            ).strip()
                        except Exception:
                            account_name = ""
                    if not account_name:
                        selected_account = next(
                            (
                                item
                                for item in probe.authorized_accounts()
                                if str(item.get("aavid") or "")
                                == latest_aavid
                            ),
                            {},
                        )
                        account_name = str(
                            selected_account.get("account_name") or ""
                        ).strip()
                    existing_account = db.select_one(
                        "qianchuan_account",
                        where={
                            "owner_username": session_owner,
                            "aavid": latest_aavid,
                        },
                    )
                    if not account_name:
                        account_name = str(
                            (existing_account or {}).get("account_name") or ""
                        ).strip()
                    _require_session_owner(session_owner)
                    account = ensure_qianchuan_account(
                        latest_aavid,
                        account_name=account_name,
                        owner_username=session_owner,
                        directory_selected=True,
                        seen=True,
                        db=db,
                    )
                    if fetcher.context:
                        await save_context_storage_state(
                            fetcher.context,
                            owner_username=session_owner,
                        )
                    mark_qianchuan_session_available(
                        owner_username=session_owner,
                    )
                    from services.qianchuan_catalog import (
                        clear_catalog_login_failure,
                    )

                    clear_catalog_login_failure(
                        owner_username=session_owner,
                        db=db,
                    )
                    with self._lock:
                        self._target_discovery_status = {
                            "success": True,
                            "running": False,
                            "message": (
                                f"已添加千川账户："
                                f"{account.get('account_name') or latest_aavid}；"
                                "Chrome已自动关闭，接下来将只同步已添加账户的计划"
                            ),
                            "target": None,
                            "account": account,
                            "relogin_complete": False,
                        }
                    logger.info(
                        "[千川账户选择] 已添加账户并将关闭可见Chrome "
                        "aavid=%s account_name=%s",
                        latest_aavid,
                        account.get("account_name") or "",
                    )
                    return
                if urlparse(cur).path == "/uni-prom":
                    product_target = probe.confirmed_product_target()
                    if product_target:
                        candidate = (
                            str(product_target["aavid"]),
                            str(product_target["ad_id"]),
                            "product",
                        )
                        # 商品列表接口会一次返回账户下的计划候选。即使当前主计划
                        # 已登记，也要继续完成分页同步，而不是要求用户逐条打开。
                        last_ignored_existing = None
                        if candidate == stable_candidate:
                            stable_count += 1
                        else:
                            stable_candidate = candidate
                            stable_count = 1
                        if stable_count >= 3:
                            target_url = cur
                            target_scene = "product"
                            target_plan_system = normalize_plan_system(
                                product_target.get("plan_system") or "unknown"
                            )
                            aavid_probe = str(product_target["aavid"])
                            ad_id_probe = str(product_target["ad_id"])
                            product_snapshot = dict(
                                product_target.get("snapshot") or {}
                            )
                            target_page_text = ""
                            break
                        await asyncio.sleep(0.5)
                        continue
                aavid_probe, ad_id_probe = _extract_aavid_adid(cur)
                if (
                    cur
                    and cur.startswith(cfg.wait_url_prefix)
                    and aavid_probe
                    and ad_id_probe
                ):
                    try:
                        page_text = await fetcher.page.locator("body").inner_text(
                            timeout=3000
                        )
                    except Exception:
                        page_text = ""
                    scene_probe = detect_confirmed_detail_scene(
                        cur,
                        page_text=page_text,
                    )
                    candidate = (
                        str(aavid_probe),
                        str(ad_id_probe),
                        str(scene_probe or ""),
                    )
                    candidate_key = _promotion_target_key(
                        candidate[0],
                        candidate[1],
                    )
                    if scene_probe and candidate_key in known_verified_target_keys:
                        stable_candidate = None
                        stable_count = 0
                        if candidate_key != last_ignored_existing:
                            last_ignored_existing = candidate_key
                            with self._lock:
                                self._target_discovery_status["message"] = (
                                    "当前计划已在监控列表中，请在新浏览器中"
                                    "打开另一条计划详情"
                                )
                        await asyncio.sleep(0.5)
                        continue
                    if scene_probe:
                        last_ignored_existing = None
                    if scene_probe and candidate == stable_candidate:
                        stable_count += 1
                    elif scene_probe:
                        stable_candidate = candidate
                        stable_count = 1
                    else:
                        stable_candidate = None
                        stable_count = 0
                    if scene_probe and stable_count >= 3:
                        target_url = cur
                        target_scene = scene_probe
                        target_plan_system = detect_plan_system(
                            page_text=page_text
                        )
                        target_page_text = page_text
                        break
                await asyncio.sleep(0.5)
            if not target_url:
                raise RuntimeError(
                    "10分钟内未识别到可添加的千川账户"
                    if account_only
                    else "10分钟内未识别到计划详情页"
                )
            if target_scene == "product" and stable_candidate:
                aavid, ad_id = stable_candidate[0], stable_candidate[1]
            else:
                aavid, ad_id = _extract_aavid_adid(target_url)
            if not target_scene:
                raise RuntimeError("无法确认当前计划是直播还是商品全域")
            try:
                page_title = await fetcher.page.title()
            except Exception:
                page_title = ""
            plan_name = extract_plan_name(
                page_text=target_page_text,
                page_title=page_title,
                ad_id=ad_id,
            )
            if target_scene == "product":
                await probe.wait_for_product_pagination()
                product_snapshot = probe.latest_product_snapshot()
                plan = product_snapshot.get("plan") or {}
                plan_name = str(plan.get("plan_name") or plan_name).strip()[:256]
            existing_target = next(
                (
                    item
                    for item in known_targets
                    if str(item.get("aadvid") or "") == str(aavid)
                    and str(item.get("ad_id") or "") == str(ad_id)
                ),
                None,
            )
            explicit_status = _explicit_platform_status(
                target_page_text,
                (product_snapshot.get("plan") or {}).get("platform_status")
                if target_scene == "product"
                else None,
            )
            _require_session_owner(session_owner)
            selected_account = next(
                (
                    item
                    for item in probe.authorized_accounts()
                    if str(item.get("aavid") or "") == str(aavid)
                ),
                {},
            )
            target = upsert_promotion_target(
                {
                    "aavid": aavid,
                    "ad_id": ad_id,
                    "account_name": str(
                        selected_account.get("account_name") or ""
                    ),
                    "plan_name": plan_name,
                    "promotion_scene": target_scene,
                    "plan_system": target_plan_system,
                    "platform_status": explicit_status,
                    "verification_state": "verified",
                    "page_url": target_url,
                    "enabled": (
                        bool(existing_target.get("enabled"))
                        if existing_target
                        else False
                    ),
                    "last_status": "pending",
                },
                owner_username=session_owner,
                trusted_catalog=True,
                db=db,
            )
            if target_scene == "product":
                _persist_product_snapshot(db, target["target_uid"], product_snapshot)
                synced = _sync_discovered_product_targets(
                    db,
                    aavid=aavid,
                    snapshot=product_snapshot,
                    owner_username=session_owner,
                    page_url=target_url,
                )
            else:
                synced = {"discovered": 0, "created": 0}
            if fetcher.context:
                _require_session_owner(session_owner)
                await save_context_storage_state(
                    fetcher.context,
                    owner_username=session_owner,
                )
                mark_qianchuan_session_available(
                    owner_username=session_owner,
                )
            with self._lock:
                self._target_discovery_status = {
                    "success": True,
                    "running": False,
                    "message": (
                        "监控计划已添加；"
                        f"同步发现 {synced['discovered']} 条商品计划，"
                        f"新增 {synced['created']} 条候选（默认未启用）"
                        if target_scene == "product"
                        else "监控计划已添加"
                    ),
                    "target": target,
                    "account": None,
                    "relogin_complete": False,
                }
        finally:
            logger.info(
                "[千川可见登录] 正在关闭独立Google Chrome "
                "login_only=%s account_only=%s",
                bool(login_only),
                bool(account_only),
            )
            await fetcher.close()

    # ---------------- thread main ----------------
    def _thread_entry(self):
        try:
            asyncio.run(self._run_async())
        except Exception as e:
            with self._lock:
                self._phase = "error"
                self._message = f"服务异常退出：{e}"
            self._log(f"[服务] 异常退出：{e}")

    async def _run_async(self):
        session_owner = current_session_owner()
        if not session_owner:
            raise RuntimeError("请先登录工具账号，再启动千川采集服务")
        cfg = ServiceConfig()
        scrape0 = load_scrape_service_config()
        # headless_poll=True：轮询阶段无头；登录阶段恒有头
        headless_mode = bool(scrape0.get("headless_poll", True))
        try:
            cfg.interval = max(5, int(scrape0.get("interval_seconds") or cfg.interval))
        except Exception:
            pass
        cfg.headless = headless_mode
        cfg.normalize_paths()

        poll_desc = "无头" if headless_mode else "有头"
        self._log(f"[服务] 登录阶段使用有头浏览器；识别目标并保存 Cookie 后，轮询抓取为{poll_desc}模式")
        with self._lock:
            self._phase = "starting"
            self._message = "初始化浏览器..."

        db = SQLiteStore(database=cfg.db_path)
        migrate_legacy_qcookie()
        storage_state_path = load_qianchuan_storage_state(
            owner_username=session_owner
        )
        # 首次/登录阶段必须可见窗口，与「无头」选项无关
        fetcher = QianChuanFetcher(headless=False, storage_state=storage_state_path)
        await fetcher._init_browser()
        probe = PromotionReadOnlyProbe(PROMOTION_PROBE_FILE)
        probe.attach(fetcher.page)

        # ---------------- 新开标签页/弹窗处理 ----------------
        # 千川页面某些按钮会触发新开标签页；如果不切换 page，会一直读到旧 page.url
        active_page_lock = threading.Lock()
        active_page = fetcher.page

        async def _switch_active_page(new_page):
            nonlocal active_page
            try:
                await new_page.bring_to_front()
            except Exception:
                pass
            with active_page_lock:
                active_page = new_page
                fetcher.page = new_page
            probe.attach(new_page)
            self._log(f"[浏览器] 检测到新标签页，已切换（url={getattr(new_page, 'url', '')}）")

        def _on_new_page(p):
            # playwright 事件回调可能不在 asyncio 上下文，丢到 loop 执行
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(_switch_active_page(p))
            except RuntimeError:
                # 若没有 running loop（极少），直接忽略
                pass

        try:
            if fetcher.context:
                fetcher.context.on("page", _on_new_page)
            if fetcher.page:
                fetcher.page.on("popup", _on_new_page)
        except Exception:
            pass

        # 打开一个起始页，让用户手动登录
        startup_url = cfg.open_url
        known_targets = list_promotion_targets(
            enabled=True,
            owner_username=session_owner,
            db=db,
        )
        reuse_last_target = _reuse_last_target_enabled()
        last_target = _load_last_target(owner_username=session_owner)
        remembered_target = last_target if reuse_last_target else None
        excluded_target = last_target if not reuse_last_target else None
        startup_target = _choose_startup_target(
            known_targets,
            remembered_target,
        )
        if storage_state_path and not reuse_last_target:
            # 用户明确要求重新选择时，从计划列表开始，不能自动复用数据库里的首个旧目标。
            startup_url = "https://qianchuan.jinritemai.com/uni-prom"
        elif storage_state_path and (startup_target or remembered_target):
            try:
                target_data = startup_target
                startup_url = build_qianchuan_url_by_params(
                    base_url=cfg.base_url,
                    aavid=int(target_data["aadvid"]),
                    ad_id=int(target_data["ad_id"]),
                    promotion_scene=target_data.get("promotion_scene") or "live",
                    source_url=target_data.get("sanitized_page_url") or None,
                )
            except Exception:
                startup_url = cfg.open_url

        try:
            await fetcher.page.goto(startup_url, wait_until="domcontentloaded", timeout=60000)
        except Exception:
            # 即便打开失败也继续等待用户操作
            pass

        self._log("[服务] 等待用户登录并进入投放详情页...")
        with self._lock:
            self._phase = "waiting_login"
            self._message = "等待识别 URL（进入投放详情页后自动开始抓取）"

        discovered = None
        if _can_reuse_startup_target(
            storage_state_available=bool(storage_state_path),
            reuse_last_target=reuse_last_target,
            startup_target=startup_target,
            current_url=fetcher.page.url,
        ):
            discovered = _trusted_startup_discovery(startup_target, startup_url)
            if discovered:
                self._log(
                    "[服务] 已复用选定的监控计划；每轮采集仍会按精确账户和计划号重新校验"
                )
        if not discovered:
            discovered = await self._wait_for_target_url(
                fetcher,
                cfg,
                probe=probe,
                excluded_target=excluded_target,
            )
        if not discovered:
            self._log("[服务] 已停止（未进入详情页）")
            with self._lock:
                self._phase = "stopped"
                self._message = "已停止"
            await fetcher.close()
            return

        target_url = str(discovered.get("url") or "").strip()
        aavid = str(discovered.get("aavid") or "").strip()
        ad_id = str(discovered.get("ad_id") or "").strip()
        try:
            page_text = await fetcher.page.locator("body").inner_text(timeout=3000)
        except Exception:
            page_text = ""
        try:
            page_title = await fetcher.page.title()
        except Exception:
            page_title = ""
        plan_name = str(discovered.get("plan_name") or "").strip()
        if not plan_name:
            plan_name = extract_plan_name(
                page_text=page_text,
                page_title=page_title,
                ad_id=ad_id,
            )
        promotion_scene = str(
            discovered.get("promotion_scene") or ""
        ).strip()
        if not promotion_scene:
            promotion_scene = detect_confirmed_detail_scene(
                target_url,
                page_text=page_text,
            )
        if not promotion_scene:
            for known in known_targets:
                if (
                    str(known.get("aadvid") or "") == str(aavid or "")
                    and str(known.get("ad_id") or "") == str(ad_id or "")
                ):
                    promotion_scene = known.get("promotion_scene")
                    break
        if not promotion_scene:
            self._log("[服务] 当前详情页无法确认是直播还是商品全域计划，已安全停止")
            with self._lock:
                self._phase = "error"
                self._message = "无法识别推广场景，请打开直播或商品全域计划详情页"
            await fetcher.close()
            return
        plan_system = normalize_plan_system(
            discovered.get("plan_system") or "unknown"
        )
        if plan_system == "unknown":
            plan_system = detect_plan_system(
                page_text=page_text,
                payload=discovered.get("snapshot"),
            )
        if plan_system == "unknown":
            for known in known_targets:
                if (
                    str(known.get("aadvid") or "") == str(aavid or "")
                    and str(known.get("ad_id") or "") == str(ad_id or "")
                ):
                    plan_system = normalize_plan_system(
                        known.get("plan_system") or "unknown"
                    )
                    break
        _require_session_owner(session_owner)
        selected_account = next(
            (
                item
                for item in probe.authorized_accounts()
                if str(item.get("aavid") or "") == str(aavid)
            ),
            {},
        )
        target = upsert_promotion_target(
            {
                "aavid": aavid,
                "ad_id": ad_id,
                "account_name": str(
                    selected_account.get("account_name") or ""
                ),
                "plan_name": plan_name,
                "promotion_scene": promotion_scene,
                "plan_system": plan_system,
                "platform_status": _explicit_platform_status(
                    page_text,
                    (
                        (discovered.get("snapshot") or {}).get("plan") or {}
                    ).get("platform_status"),
                ),
                "verification_state": "verified",
                "page_url": target_url,
                "enabled": bool(
                    next(
                        (
                            item.get("enabled")
                            for item in known_targets
                            if str(item.get("aadvid") or "") == str(aavid)
                            and str(item.get("ad_id") or "") == str(ad_id)
                        ),
                        False,
                    )
                ),
                "last_status": "pending",
            },
            owner_username=session_owner,
            trusted_catalog=True,
            db=db,
        )
        if promotion_scene == "product":
            await probe.wait_for_product_pagination()
            complete_snapshot = probe.latest_product_snapshot()
            if complete_snapshot.get("ad_rows"):
                discovered["snapshot"] = complete_snapshot
            _persist_product_snapshot(
                db,
                target["target_uid"],
                discovered.get("snapshot"),
            )
            _sync_discovered_product_targets(
                db,
                aavid=aavid,
                snapshot=discovered.get("snapshot"),
                owner_username=session_owner,
                page_url=target_url,
            )
        _save_last_target(
            aavid,
            ad_id,
            owner_username=session_owner,
        )
        with self._lock:
            self._last_target = {
                "targetUid": target["target_uid"],
                "aavid": aavid,
                "adId": ad_id,
                "promotionScene": promotion_scene,
                "planSystem": plan_system,
                "planName": target.get("plan_name") or "",
                "url": target_url,
            }
            self._phase = "starting"
            self._message = (
                f"已识别{('推商品' if promotion_scene == 'product' else '推直播')}计划，"
                f"体系={plan_system}，"
                "保存登录状态并启动轮询..."
            )
        self._log(
            f"[服务] 识别到目标：target={target['target_uid']} aavid={aavid}, "
            f"adId={ad_id}, scene={promotion_scene}, system={plan_system}，"
            "准备保存 cookies 并重启"
        )

        # -------- 阶段切换：识别成功后先保存 cookies，然后关闭当前浏览器，再用 cookies 重启抓取 --------
        try:
            if fetcher.context:
                _require_session_owner(session_owner)
                await save_context_storage_state(
                    fetcher.context,
                    owner_username=session_owner,
                )
                mark_qianchuan_session_available(
                    owner_username=session_owner,
                )
                self._log(f"[Cookie] 已保存")
        except Exception as e:
            self._log(f"[Cookie] 保存失败（仍继续重启）：{e}")

        try:
            await fetcher.close()
        except Exception:
            pass

        # 用标准构建逻辑生成“正确的抓取 URL”
        fetch_url = None
        try:
            if aavid and ad_id:
                fetch_url = build_qianchuan_url_by_params(
                    base_url=cfg.base_url,
                    aavid=int(aavid),
                    ad_id=int(ad_id),
                    promotion_scene=promotion_scene,
                    source_url=target.get("sanitized_page_url") or target_url,
                )
        except Exception as e:
            self._log(f"[URL] 构建抓取URL失败：{e}")

        if not fetch_url:
            self._log("[服务] 未能构建抓取URL，已停止")
            with self._lock:
                self._phase = "error"
                self._message = "构建抓取URL失败"
            return

        # 重启抓取器：登录完成后再次读取配置（用户可能在等待登录期间改过无头/间隔）
        scrape_poll = load_scrape_service_config()
        headless_mode = bool(scrape_poll.get("headless_poll", True))
        try:
            cfg.interval = max(5, int(scrape_poll.get("interval_seconds") or cfg.interval))
        except Exception:
            pass

        storage_state_path = load_qianchuan_storage_state(
            owner_username=session_owner
        )
        fetcher = QianChuanFetcher(headless=headless_mode, storage_state=storage_state_path)
        await fetcher._init_browser()
        self._active_poll_headless = headless_mode
        poll_browser_started_at = time.time()

        # 首屏由 fetch() 内 goto；若先在此 goto 会导致与 fetch 内「自定义列预设 + 再次 goto」重复导航

        with self._lock:
            self._phase = "running"
            self._message = f"抓取中（aavid={aavid}, adId={ad_id}）"
        self._log(f"[服务] 浏览器已重启，进入轮询抓取（fetch_url={fetch_url}）")

        # 标记已准备好轮询抓取（首轮立即执行；之后仅由服务端按间隔计时）
        self._fetch_ready = True
        self._fetch_url = fetch_url
        self._fetch_db = db
        self._fetch_cfg = cfg
        self._fetcher = fetcher
        self._last_fetch_time = 0  # 尚未完成第一次抓取

        with self._lock:
            self._phase = "running"
            self._message = f"抓取中（aavid={aavid}, adId={ad_id}）"

        # 轮询抓取：仅服务端调度，与前端页面无关
        first_poll = True
        auto_stopped_auth_expired = False
        while not self._stop_event.is_set():
            _require_session_owner(session_owner)
            while not self._stop_event.is_set():
                if first_poll:
                    break
                interval_sec = self._effective_interval_sec(cfg)
                # 用“上一轮开始时间”调度，而不是在整轮完成后再完整等待一次。
                # 当一轮耗时超过五分钟时，下一轮会在短暂让出事件循环后立即开始。
                last = self._last_round_started_time or self._last_fetch_time
                if last > 0 and (time.time() - last) >= interval_sec:
                    break
                if last <= 0:
                    # 上一轮失败未写入 last_fetch_time：短歇后重试
                    await asyncio.sleep(2.0)
                    break
                await asyncio.sleep(0.5)

            if self._stop_event.is_set():
                break

            first_poll = False
            self._last_round_started_time = time.time()

            scrape_cfg = load_scrape_service_config()
            new_headless = bool(scrape_cfg.get("headless_poll", True))
            if self._active_poll_headless is not None and new_headless != self._active_poll_headless:
                self._log(
                    f"[服务] 轮询无头模式已变更（{self._active_poll_headless} -> {new_headless}），"
                    f"关闭浏览器后按 Cookie 重启并直达抓取页"
                )
                try:
                    await fetcher.close()
                except Exception:
                    pass
                storage_state_path = load_qianchuan_storage_state(
                    owner_username=session_owner
                )
                fetcher = QianChuanFetcher(headless=new_headless, storage_state=storage_state_path)
                await fetcher._init_browser()
                self._fetcher = fetcher
                self._active_poll_headless = new_headless
                poll_browser_started_at = time.time()

            if (time.time() - poll_browser_started_at) >= POLL_BROWSER_RECYCLE_INTERVAL_SEC:
                self._log(
                    f"[服务] 轮询浏览器已持续运行约 {POLL_BROWSER_RECYCLE_INTERVAL_SEC // 3600} 小时，"
                    "关闭并重新启动以释放内存（Cookie 已保留）..."
                )
                try:
                    if fetcher.context:
                        _require_session_owner(session_owner)
                        await save_context_storage_state(
                            fetcher.context,
                            owner_username=session_owner,
                        )
                except Exception as e:
                    self._log(f"[Cookie] 周期重启前保存失败（仍继续重启）：{e}")
                try:
                    await fetcher.close()
                except Exception:
                    pass
                storage_state_path = load_qianchuan_storage_state(
                    owner_username=session_owner
                )
                fetcher = QianChuanFetcher(headless=new_headless, storage_state=storage_state_path)
                await fetcher._init_browser()
                self._fetcher = fetcher
                poll_browser_started_at = time.time()
                self._log("[服务] 轮询浏览器已按周期重启，继续抓取")

            try:
                targets = schedulable_promotion_targets(
                    owner_username=session_owner,
                    db=db,
                )
                if not targets:
                    self._log("[抓取] 没有启用的监控计划，本轮跳过")
                    if (
                        self._last_catalog_sync_time <= 0
                        or time.time() - self._last_catalog_sync_time >= 30 * 60
                    ):
                        self.start_catalog_sync()
                        self._last_catalog_sync_time = time.time()
                    self._last_fetch_time = time.time()
                    continue
                self._log(f"[抓取] 开始一轮抓取，共 {len(targets)} 条监控计划")
                fa, fp, ft = snapshot_feishu_bitable_for_fetch()
                fs_panel = load_feishu_bitable_panel_config()
                fpm = fs_panel.get("push_mode") or "each_crawl"
                for target_index, current_target in enumerate(targets, start=1):
                    _require_session_owner(session_owner)
                    if self._stop_event.is_set():
                        break
                    current_aavid = str(current_target.get("aadvid") or "").strip()
                    current_ad_id = str(current_target.get("ad_id") or "").strip()
                    current_uid = str(current_target.get("target_uid") or "").strip()
                    current_scene = str(
                        current_target.get("promotion_scene") or "live"
                    ).strip()
                    current_plan_system = normalize_plan_system(
                        current_target.get("plan_system") or "unknown"
                    )
                    try:
                        fetch_url = build_qianchuan_url_by_params(
                            base_url=cfg.base_url,
                            aavid=int(current_aavid),
                            ad_id=int(current_ad_id),
                            promotion_scene=current_scene,
                            source_url=current_target.get("sanitized_page_url") or None,
                        )
                    except Exception as e:
                        patch_target_sync_state(
                            current_uid,
                            status="error",
                            error=f"构建抓取地址失败：{e}",
                            capability_updates={
                                "assist_sync_in_progress": False,
                                "assist_sync_ok": False,
                                "assist_synced_at": "",
                            },
                            db=db,
                        )
                        self._log(
                            f"[抓取 {target_index}/{len(targets)}] "
                            f"{current_target.get('plan_name') or current_ad_id} 地址构建失败：{e}"
                        )
                        continue
                    with self._lock:
                        self._last_target = {
                            "targetUid": current_uid,
                            "aavid": current_aavid,
                            "adId": current_ad_id,
                            "promotionScene": current_scene,
                            "planSystem": current_plan_system,
                            "planName": current_target.get("plan_name") or "",
                            "url": fetch_url,
                        }
                        self._message = (
                            f"抓取中（{target_index}/{len(targets)}："
                            f"{current_target.get('plan_name') or current_ad_id}）"
                        )
                    self._fetch_url = fetch_url
                    self._log(
                        f"[抓取 {target_index}/{len(targets)}] 开始 "
                        f"target={current_uid} scene={current_scene} "
                        f"system={current_plan_system}"
                    )
                    target_started_at = time.monotonic()
                    try:
                        patch_target_sync_state(
                            current_uid,
                            # 先关闭旧的 ok 状态，再等待浏览器锁。否则上一轮
                            # 的成功状态会在本轮门禁超时尚未落库时继续放行发卡
                            # 或写操作。
                            status="verifying",
                            error="调控任务正在同步，本轮自动停投暂不可用",
                            capability_updates={
                                "assist_sync_in_progress": True,
                                "assist_sync_ok": False,
                                "assist_synced_at": "",
                            },
                            db=db,
                        )
                        async with exclusive_browser_operation(
                            f"采集:{current_uid}",
                            timeout_seconds=max(60, int(cfg.round_timeout)),
                            priority=30,
                        ):
                            result = await fetcher.fetch(
                                fetch_url,
                                db=db,
                                timeout=int(cfg.round_timeout),
                                feishu_app_token=fa,
                                feishu_personal_base_token=fp,
                                feishu_table_id=ft,
                                feishu_push_mode=fpm,
                                cloud_backup_username=self._cloud_backup_username,
                                cloud_backup_password=self._cloud_backup_password,
                                target_uid=current_uid,
                                promotion_scene=current_scene,
                                plan_system=current_plan_system,
                                plan_name=current_target.get("plan_name") or "",
                            )
                        material_count = int(result.get("material_total_count") or 0)
                        assist_sync_enabled = bool(
                            result.get("assist_sync_enabled")
                        )
                        assist_sync_ok = bool(result.get("assist_sync_ok"))
                        assist_synced_at = (
                            datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                            if assist_sync_enabled and assist_sync_ok
                            else ""
                        )
                        delivery_gate = result.get("delivery_gate") or {}
                        delivery_reason = str(
                            delivery_gate.get("reason") or ""
                        ).strip()
                        delivery_gate_ok = (
                            delivery_gate.get("ok") is True
                            and delivery_reason == "delivering"
                        )
                        delivery_name = str(
                            delivery_gate.get("delivery_name") or ""
                        ).strip()
                        product_link_count = db.count(
                            "promotion_material_product",
                            where={"target_uid": current_uid},
                        )
                        if delivery_reason == "not_delivering":
                            target_status = (
                                "paused" if "暂停" in delivery_name else "not_delivering"
                            )
                            update_target_catalog_evidence(
                                current_uid,
                                platform_status=target_status,
                                verification_state="verified",
                                db=db,
                            )
                            patch_target_sync_state(
                                current_uid,
                                status=target_status,
                                error=(
                                    f"计划当前状态：{delivery_name or '非投放中'}；"
                                    "已禁止追投，未执行任何写操作"
                                ),
                                capability_updates={
                                    "product_relation": (
                                        current_scene == "live"
                                        or product_link_count > 0
                                    ),
                                    "retarget_execute": False,
                                    "regulation_execute": False,
                                    "assist_sync_enabled": assist_sync_enabled,
                                    "assist_sync_in_progress": False,
                                    "assist_sync_ok": False,
                                    "assist_synced_at": "",
                                },
                                capability_remove_keys=_WRITE_CAPABILITY_SCOPE_KEYS,
                                db=db,
                            )
                            self._log(
                                f"[抓取 {target_index}/{len(targets)}] "
                                f"计划当前为{delivery_name or '非投放中'}，已安全跳过"
                            )
                        elif not delivery_gate_ok:
                            verification_error = (
                                "未取得明确的投放中证据"
                                + (
                                    f"（{delivery_reason}）"
                                    if delivery_reason
                                    else ""
                                )
                                + "；本轮保持历史目录状态并禁止自动写入"
                            )
                            record_target_verification_failure(
                                current_uid,
                                verification_error,
                                db=db,
                            )
                            patch_target_sync_state(
                                current_uid,
                                status="verification_error",
                                error=verification_error,
                                capability_updates={
                                    "assist_sync_enabled": assist_sync_enabled,
                                    "assist_sync_in_progress": False,
                                    "assist_sync_ok": False,
                                    "assist_synced_at": "",
                                },
                                db=db,
                            )
                            self._log(
                                f"[抓取 {target_index}/{len(targets)}] "
                                f"{verification_error}"
                            )
                        elif current_scene == "product" and material_count <= 0:
                            patch_target_sync_state(
                                current_uid,
                                status="capability_mismatch",
                                error="商品全域页面未识别到素材接口，本轮未执行任何写操作",
                                capability_updates={
                                    "material_read": False,
                                    "product_relation": product_link_count > 0,
                                    "retarget_execute": False,
                                    "regulation_execute": False,
                                    "assist_sync_enabled": assist_sync_enabled,
                                    "assist_sync_in_progress": False,
                                    "assist_sync_ok": False,
                                    "assist_synced_at": "",
                                },
                                capability_remove_keys=_WRITE_CAPABILITY_SCOPE_KEYS,
                                db=db,
                            )
                            self._log(
                                f"[抓取 {target_index}/{len(targets)}] 商品页面能力未识别，已安全跳过"
                            )
                        else:
                            update_target_catalog_evidence(
                                current_uid,
                                platform_status="active",
                                verification_state="verified",
                                db=db,
                            )
                            patch_target_sync_state(
                                current_uid,
                                status="ok",
                                synced=True,
                                error=(
                                    "素材同步正常，但调控任务本轮未完整同步；"
                                    "自动停投已暂停"
                                    if assist_sync_enabled
                                    and not assist_sync_ok
                                    else ""
                                ),
                                capability_updates={
                                    "material_read": True,
                                    "product_relation": (
                                        current_scene == "live"
                                        or product_link_count > 0
                                    ),
                                    "assist_sync_enabled": assist_sync_enabled,
                                    "assist_sync_in_progress": False,
                                    "assist_sync_ok": (
                                        assist_sync_enabled and assist_sync_ok
                                    ),
                                    "assist_synced_at": assist_synced_at,
                                },
                                # 常规只读采集只能更新读取能力，不能提升、降级或抹掉
                                # 已由受控追投/停投验证写入的场景、体系、版本和时间证据。
                                db=db,
                            )
                            self._log(
                                f"[抓取 {target_index}/{len(targets)}] 完成，素材 {material_count} 条"
                            )
                        try:
                            await self._maybe_feishu_hourly_push_after_fetch(
                                db,
                                fa,
                                fp,
                                ft,
                                current_aavid or None,
                                current_uid or None,
                            )
                        except Exception as e:
                            self._log(f"[飞书·整点] 检查/同步异常（已忽略）：{e}")
                    except GlobalAuthExpiredError:
                        raise
                    except Exception as e:
                        patch_target_sync_state(
                            current_uid,
                            status="error",
                            error=str(e),
                            capability_updates={
                                "assist_sync_in_progress": False,
                                "assist_sync_ok": False,
                                "assist_synced_at": "",
                            },
                            db=db,
                        )
                        self._log(
                            f"[抓取 {target_index}/{len(targets)}] 异常：{e}"
                        )
                    finally:
                        try:
                            record_target_duration(
                                current_uid,
                                int((time.monotonic() - target_started_at) * 1000),
                                db=db,
                            )
                        except Exception:
                            pass
                        try:
                            patch_target_sync_state(
                                current_uid,
                                status=None,
                                capability_updates={
                                    "assist_sync_in_progress": False,
                                },
                                db=db,
                            )
                        except Exception:
                            pass
                        try:
                            fetcher._material_total_count = 0
                            fetcher._material_current_count = 0
                            fetcher._reset_assist_fetch_state()
                        except Exception:
                            pass

                # 整轮结束后保存一次 Cookie。
                try:
                    if fetcher.context:
                        _require_session_owner(session_owner)
                        await save_context_storage_state(
                            fetcher.context,
                            owner_username=session_owner,
                        )
                except Exception:
                    pass
                self._last_fetch_time = time.time()
                if (
                    self._last_catalog_sync_time <= 0
                    or time.time() - self._last_catalog_sync_time >= 30 * 60
                ):
                    self.start_catalog_sync()
                    self._last_catalog_sync_time = time.time()
                mark_qianchuan_session_available(
                    owner_username=session_owner,
                )
                self._log("[抓取] 本轮全部监控计划处理完成")
                # 本轮已结束，清空进度计数；否则 status 里一直带着上一轮的 current/total，
                # 前端会永远走「抓取中」分支，无法显示轮询间隔内的「等待中 / 倒计时」。
                # 调控任务进度也需清零，否则会残留 797/797，顶栏一直显示「采集中」而无法进入倒计时。
                try:
                    fetcher._material_total_count = 0
                    fetcher._material_current_count = 0
                    fetcher._reset_assist_fetch_state()
                except Exception:
                    pass
            except GlobalAuthExpiredError:
                auto_stopped_auth_expired = True
                mark_qianchuan_session_invalid(
                    "千川全域投放授权已失效",
                    owner_username=session_owner,
                )
                self._log("[服务] 检测到千川「全域投放授权已失效」弹窗，抓取已自动终止；请在平台重新授权后重启服务。")
                try:
                    fetcher._material_total_count = 0
                    fetcher._material_current_count = 0
                    fetcher._reset_assist_fetch_state()
                except Exception:
                    pass
                with self._lock:
                    self._phase = "stopped"
                    self._message = "程序自动终止（原因：授权已失效）"
                self._stop_event.set()
                break
            except Exception as e:
                self._log(f"[抓取] 异常：{e}")

        if auto_stopped_auth_expired:
            self._log("[服务] 因授权失效已终止，正在关闭浏览器...")
        else:
            self._log("[服务] 收到停止信号，正在退出...")
            with self._lock:
                self._phase = "stopped"
                self._message = "已停止"
        await fetcher.close()

    async def _wait_for_target_url(
        self,
        fetcher: QianChuanFetcher,
        cfg: ServiceConfig,
        *,
        probe: Optional[PromotionReadOnlyProbe] = None,
        excluded_target: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        prefix = (cfg.wait_url_prefix or "").strip()
        stable_candidate = None
        stable_count = 0
        last_probe_at = 0.0
        excluded_candidate_reported = False
        while not self._stop_event.is_set():
            try:
                # 注意：fetcher.page 可能被 “新标签页” 回调更新
                cur = (fetcher.page.url or "").strip()
            except Exception:
                cur = ""

            if probe and time.time() - last_probe_at >= 1.0:
                await probe.observe_page(fetcher.page)
                last_probe_at = time.time()

            # 商品全域实际页面保持在 /uni-prom，主计划 ID 来自 ad-detail-plus /
            # shop-prom/get-config。只有主计划接口和唯一账户 ID 同时确认时才登记。
            if probe and urlparse(cur).path == "/uni-prom":
                product_target = probe.confirmed_product_target()
                if product_target:
                    candidate = (
                        str(product_target["aavid"]),
                        str(product_target["ad_id"]),
                        "product",
                    )
                    if _target_is_excluded(
                        product_target["aavid"],
                        product_target["ad_id"],
                        excluded_target,
                    ):
                        stable_candidate = None
                        stable_count = 0
                        if not excluded_candidate_reported:
                            self._log(
                                "[服务] 当前仍是上一次计划；重新选择模式正在等待另一条计划"
                            )
                            with self._lock:
                                self._message = (
                                    "请在千川中选择另一条状态为“投放中”的商品全域计划"
                                )
                            excluded_candidate_reported = True
                        await asyncio.sleep(0.5)
                        continue
                    if candidate == stable_candidate:
                        stable_count += 1
                    else:
                        stable_candidate = candidate
                        stable_count = 1
                    if stable_count >= 3:
                        return {
                            **product_target,
                            "url": cur,
                        }
                    await asyncio.sleep(0.5)
                    continue

            if cur and (not prefix or cur.startswith(prefix)):
                aavid, ad_id = _extract_aavid_adid(cur)
                if aavid and ad_id:
                    try:
                        page_text = await fetcher.page.locator("body").inner_text(
                            timeout=3000
                        )
                    except Exception:
                        page_text = ""
                    scene = detect_confirmed_detail_scene(
                        cur,
                        page_text=page_text,
                    )
                    candidate = (str(aavid), str(ad_id), str(scene or ""))
                    if scene and _target_is_excluded(
                        aavid,
                        ad_id,
                        excluded_target,
                    ):
                        stable_candidate = None
                        stable_count = 0
                        if not excluded_candidate_reported:
                            self._log(
                                "[服务] 当前仍是上一次计划；重新选择模式正在等待另一条计划"
                            )
                            with self._lock:
                                self._message = (
                                    "请在千川中选择另一条状态为“投放中”的计划"
                                )
                            excluded_candidate_reported = True
                    elif scene and candidate == stable_candidate:
                        stable_count += 1
                    elif scene:
                        stable_candidate = candidate
                        stable_count = 1
                    else:
                        stable_candidate = None
                        stable_count = 0
                    if scene and stable_count >= 3:
                        return {
                            "url": cur,
                            "aavid": str(aavid),
                            "ad_id": str(ad_id),
                            "promotion_scene": scene,
                            "plan_system": detect_plan_system(
                                page_text=page_text
                            ),
                            "plan_name": "",
                            "snapshot": {},
                        }

            await asyncio.sleep(0.5)
        return None


_GLOBAL_CONTROLLER: Optional[ServiceController] = None


def get_service_controller() -> ServiceController:
    global _GLOBAL_CONTROLLER
    if _GLOBAL_CONTROLLER is None:
        _GLOBAL_CONTROLLER = ServiceController()
    return _GLOBAL_CONTROLLER
