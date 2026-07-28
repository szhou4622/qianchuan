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
from services.plan_system import detect_plan_system, normalize_plan_system
from api.promotion_targets import (
    detect_confirmed_detail_scene,
    detect_promotion_scene,
    extract_plan_name,
    list_promotion_targets,
    make_target_uid,
    replace_material_product_links,
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


"""
注意：PROJECT_ROOT / DATA_DIR / LOGS_DIR 统一从 config.py 引用
"""

# 轮询抓取阶段：浏览器持续运行超过此时长则关闭并用 Cookie 重建，缓解长时间运行内存增长（秒）
POLL_BROWSER_RECYCLE_INTERVAL_SEC = 2 * 3600
LAST_TARGET_FILE = os.path.join(DATA_DIR, "last_crawl_target.json")
PROMOTION_PROBE_FILE = os.path.join(DATA_DIR, "promotion_readonly_probe.json")


def _persist_product_snapshot(
    db: SQLiteStore,
    target_uid: str,
    snapshot: Optional[Dict[str, Any]],
) -> None:
    if not isinstance(snapshot, dict):
        return
    products = snapshot.get("products") or []
    if products:
        upsert_products(target_uid, products, db=db)
    for material in snapshot.get("materials") or []:
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


def _ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


def _load_last_target(path: Optional[str] = None):
    target_path = path or LAST_TARGET_FILE
    try:
        with open(target_path, "r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    aavid = str(data.get("aavid") or "").strip()
    ad_id = str(data.get("adId") or data.get("ad_id") or "").strip()
    if not aavid.isdigit() or not ad_id.isdigit():
        return None
    return {"aavid": aavid, "adId": ad_id}


def _save_last_target(aavid, ad_id, path: Optional[str] = None) -> bool:
    aavid_text = str(aavid or "").strip()
    ad_id_text = str(ad_id or "").strip()
    if not aavid_text.isdigit() or not ad_id_text.isdigit():
        return False
    target_path = path or LAST_TARGET_FILE
    parent = os.path.dirname(os.path.abspath(target_path))
    os.makedirs(parent, exist_ok=True)
    temp_path = target_path + ".tmp"
    try:
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(
                {"aavid": aavid_text, "adId": ad_id_text},
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

        # 启动服务时校验通过的账号密码，仅内存保存，用于入库后云端备份 API（不写盘）
        self._cloud_backup_username: Optional[str] = None
        self._cloud_backup_password: str = ""
        self._target_discovery_thread: Optional[threading.Thread] = None
        self._target_discovery_status: dict = {
            "running": False,
            "message": "",
            "target": None,
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
                interval = int(load_scrape_service_config().get("interval_seconds") or 60)
            except Exception:
                interval = 60
            interval = max(5, interval)

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
        """轮询间隔（秒）：每轮从 control_panel.json → crawl 读取。"""
        try:
            base = int(load_scrape_service_config().get("interval_seconds") or cfg.interval)
        except Exception:
            base = cfg.interval
        return max(5, base)

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

    def start_target_discovery(self) -> dict:
        """打开独立有头浏览器，用户进入计划详情后自动登记监控目标。"""
        with self._lock:
            if (
                self._target_discovery_thread is not None
                and self._target_discovery_thread.is_alive()
            ):
                return {
                    "success": True,
                    **self._target_discovery_status,
                }
            self._target_discovery_status = {
                "running": True,
                "message": "请在新浏览器中打开一条直播或商品全域计划详情",
                "target": None,
            }
            self._target_discovery_thread = threading.Thread(
                target=self._target_discovery_entry,
                name="promotion-target-discovery",
                daemon=True,
            )
            self._target_discovery_thread.start()
        return {"success": True, **self._target_discovery_status}

    def target_discovery_status(self) -> dict:
        with self._lock:
            return {"success": True, **dict(self._target_discovery_status)}

    def _target_discovery_entry(self) -> None:
        try:
            asyncio.run(self._target_discovery_async())
        except Exception as e:
            with self._lock:
                self._target_discovery_status = {
                    "running": False,
                    "message": f"识别失败：{e}",
                    "target": None,
                }

    async def _target_discovery_async(self) -> None:
        cfg = ServiceConfig().normalize_paths()
        db = SQLiteStore(database=cfg.db_path)
        storage_state = (
            cfg.cookie_path
            if cfg.cookie_path and os.path.isfile(cfg.cookie_path)
            else None
        )
        fetcher = QianChuanFetcher(headless=False, storage_state=storage_state)
        await fetcher._init_browser()
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
            target_url = None
            target_scene = None
            target_plan_system = "unknown"
            target_page_text = ""
            product_snapshot: Dict[str, Any] = {}
            stable_candidate = None
            stable_count = 0
            last_probe_at = 0.0
            deadline = time.time() + 600
            while time.time() < deadline:
                try:
                    if fetcher.context and fetcher.context.pages:
                        fetcher.page = fetcher.context.pages[-1]
                except Exception:
                    pass
                cur = str(getattr(fetcher.page, "url", "") or "").strip()
                if time.time() - last_probe_at >= 1.0:
                    await probe.observe_page(fetcher.page)
                    last_probe_at = time.time()
                if urlparse(cur).path == "/uni-prom":
                    product_target = probe.confirmed_product_target()
                    if product_target:
                        candidate = (
                            str(product_target["aavid"]),
                            str(product_target["ad_id"]),
                            "product",
                        )
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
                raise RuntimeError("10分钟内未识别到计划详情页")
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
                plan = product_snapshot.get("plan") or {}
                plan_name = str(plan.get("plan_name") or plan_name).strip()[:256]
            target = upsert_promotion_target(
                {
                    "aavid": aavid,
                    "ad_id": ad_id,
                    "plan_name": plan_name,
                    "promotion_scene": target_scene,
                    "plan_system": target_plan_system,
                    "page_url": target_url,
                    "enabled": True,
                    "last_status": "pending",
                },
                db=db,
            )
            if target_scene == "product":
                _persist_product_snapshot(db, target["target_uid"], product_snapshot)
            if fetcher.context:
                _ensure_data_dir()
                await fetcher.context.storage_state(path=cfg.cookie_path)
            with self._lock:
                self._target_discovery_status = {
                    "running": False,
                    "message": "监控计划已添加",
                    "target": target,
                }
        finally:
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
        # 首次启动可能没有 cookie 文件；只有存在时才传入 storage_state，避免 playwright 报错
        storage_state_path = cfg.cookie_path if (cfg.cookie_path and os.path.exists(cfg.cookie_path)) else None
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
        known_targets = list_promotion_targets(enabled=True, db=db)
        reuse_last_target = _reuse_last_target_enabled()
        last_target = _load_last_target()
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
        if storage_state_path and reuse_last_target and startup_target:
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
        target = upsert_promotion_target(
            {
                "target_uid": make_target_uid(aavid, ad_id),
                "aavid": aavid,
                "ad_id": ad_id,
                "plan_name": plan_name,
                "promotion_scene": promotion_scene,
                "plan_system": plan_system,
                "page_url": target_url,
                "enabled": True,
                "last_status": "pending",
            },
            db=db,
        )
        if promotion_scene == "product":
            _persist_product_snapshot(
                db,
                target["target_uid"],
                discovered.get("snapshot"),
            )
        _save_last_target(aavid, ad_id)
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
                _ensure_data_dir()
                await fetcher.context.storage_state(path=cfg.cookie_path)
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

        storage_state_path = cfg.cookie_path if (cfg.cookie_path and os.path.exists(cfg.cookie_path)) else None
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
            while not self._stop_event.is_set():
                if first_poll:
                    break
                interval_sec = self._effective_interval_sec(cfg)
                last = self._last_fetch_time
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
                storage_state_path = cfg.cookie_path if (cfg.cookie_path and os.path.exists(cfg.cookie_path)) else None
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
                        _ensure_data_dir()
                        await fetcher.context.storage_state(path=cfg.cookie_path)
                except Exception as e:
                    self._log(f"[Cookie] 周期重启前保存失败（仍继续重启）：{e}")
                try:
                    await fetcher.close()
                except Exception:
                    pass
                storage_state_path = cfg.cookie_path if (cfg.cookie_path and os.path.exists(cfg.cookie_path)) else None
                fetcher = QianChuanFetcher(headless=new_headless, storage_state=storage_state_path)
                await fetcher._init_browser()
                self._fetcher = fetcher
                poll_browser_started_at = time.time()
                self._log("[服务] 轮询浏览器已按周期重启，继续抓取")

            try:
                targets = list_promotion_targets(enabled=True, db=db)
                if not targets:
                    self._log("[抓取] 没有启用的监控计划，本轮跳过")
                    self._last_fetch_time = time.time()
                    continue
                self._log(f"[抓取] 开始一轮抓取，共 {len(targets)} 条监控计划")
                fa, fp, ft = snapshot_feishu_bitable_for_fetch()
                fs_panel = load_feishu_bitable_panel_config()
                fpm = fs_panel.get("push_mode") or "each_crawl"
                for target_index, current_target in enumerate(targets, start=1):
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
                        update_target_sync_state(
                            current_uid,
                            status="error",
                            error=f"构建抓取地址失败：{e}",
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
                    try:
                        async with exclusive_browser_operation(
                            f"采集:{current_uid}",
                            timeout_seconds=max(60, int(cfg.round_timeout)),
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
                        delivery_gate = result.get("delivery_gate") or {}
                        delivery_reason = str(
                            delivery_gate.get("reason") or ""
                        ).strip()
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
                            existing_capability = current_target.get("capability")
                            if not isinstance(existing_capability, dict):
                                existing_capability = {}
                            update_target_sync_state(
                                current_uid,
                                status=target_status,
                                error=(
                                    f"计划当前状态：{delivery_name or '非投放中'}；"
                                    "已禁止追投，未执行任何写操作"
                                ),
                                capability={
                                    "material_read": bool(
                                        existing_capability.get("material_read")
                                    ),
                                    "product_relation": (
                                        current_scene == "live"
                                        or product_link_count > 0
                                    ),
                                    "retarget_execute": False,
                                    "regulation_execute": False,
                                },
                                db=db,
                            )
                            self._log(
                                f"[抓取 {target_index}/{len(targets)}] "
                                f"计划当前为{delivery_name or '非投放中'}，已安全跳过"
                            )
                        elif current_scene == "product" and material_count <= 0:
                            update_target_sync_state(
                                current_uid,
                                status="capability_mismatch",
                                error="商品全域页面未识别到素材接口，本轮未执行任何写操作",
                                capability={
                                    "material_read": False,
                                    "product_relation": product_link_count > 0,
                                    "retarget_execute": False,
                                    "regulation_execute": False,
                                },
                                db=db,
                            )
                            self._log(
                                f"[抓取 {target_index}/{len(targets)}] 商品页面能力未识别，已安全跳过"
                            )
                        else:
                            existing_capability = current_target.get("capability")
                            if not isinstance(existing_capability, dict):
                                existing_capability = {}
                            update_target_sync_state(
                                current_uid,
                                status="ok",
                                synced=True,
                                capability={
                                    "material_read": True,
                                    "product_relation": (
                                        current_scene == "live" or product_link_count > 0
                                    ),
                                    # 商品追投表单经独立只读探测确认后保留能力标记；
                                    # 常规素材轮询不能替代该探测，也不能擅自打开能力。
                                    "retarget_execute": bool(
                                        existing_capability.get("retarget_execute")
                                    ),
                                    # 商品任务列表/结束入口须在真实受控任务创建后
                                    # 另行探测；未确认前自动停投保持关闭。
                                    "regulation_execute": bool(
                                        existing_capability.get("regulation_execute")
                                    ),
                                },
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
                        update_target_sync_state(
                            current_uid,
                            status="error",
                            error=str(e),
                            db=db,
                        )
                        self._log(
                            f"[抓取 {target_index}/{len(targets)}] 异常：{e}"
                        )
                    finally:
                        try:
                            fetcher._material_total_count = 0
                            fetcher._material_current_count = 0
                            fetcher._reset_assist_fetch_state()
                        except Exception:
                            pass

                # 整轮结束后保存一次 Cookie。
                try:
                    if fetcher.context:
                        _ensure_data_dir()
                        await fetcher.context.storage_state(path=cfg.cookie_path)
                except Exception:
                    pass
                self._last_fetch_time = time.time()
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
