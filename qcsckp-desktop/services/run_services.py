"""
服务运行模块（GUI 调用）

职责：
- 以线程方式启动/停止抓取服务（避免阻塞 GUI）
- Headful 浏览器，允许用户手动登录千川
- 监控当前页面 URL，当识别到详情页并解析到 aavid/adId 后才开始抓取
- 轮询抓取并入库 SQLite
- 服务管理配置见 data/control_panel.json（crawl / feishu_table / robot）
- 日志写入 data/service.log，并提供读取末尾 N 行的方法给前端展示
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Tuple, List
from urllib.parse import urlparse, parse_qs

from utils.log import logger as app_logger
from utils.sqlite_store import SQLiteStore
from services.fetcher import QianChuanFetcher, build_qianchuan_url_by_params, GlobalAuthExpiredError
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


def _ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


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
    rows = db.select_pmc_latest_per_material_in_last_hour_utc8(aadvid=aid)
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
    cookie_path: str = "data/qcookie.json"
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
    ) -> None:
        cfg = load_feishu_bitable_panel_config()
        if cfg.get("push_mode") != "hourly_latest":
            return
        if not (fa and fp and ft):
            return

        def _run():
            return _feishu_hourly_push_window_sync(
                db, fa, fp, ft, aadvid, self._feishu_hourly_last_window_end, self._log
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
        try:
            await fetcher.page.goto(cfg.open_url, wait_until="domcontentloaded", timeout=60000)
        except Exception:
            # 即便打开失败也继续等待用户操作
            pass

        self._log("[服务] 等待用户登录并进入投放详情页...")
        with self._lock:
            self._phase = "waiting_login"
            self._message = "等待识别 URL（进入投放详情页后自动开始抓取）"

        target_url = await self._wait_for_target_url(fetcher, cfg)
        if not target_url:
            self._log("[服务] 已停止（未进入详情页）")
            with self._lock:
                self._phase = "stopped"
                self._message = "已停止"
            await fetcher.close()
            return

        aavid, ad_id = _extract_aavid_adid(target_url)
        with self._lock:
            self._last_target = {"aavid": aavid, "adId": ad_id, "url": target_url}
            self._phase = "starting"
            self._message = f"已识别目标（aavid={aavid}, adId={ad_id}），保存 cookies 并重启浏览器..."
        self._log(f"[服务] 识别到目标：aavid={aavid}, adId={ad_id}，准备保存 cookies 并重启")

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

            # 每次抓取前重新构建 URL（日期可能已变化）
            current_aavid = self._last_target.get("aavid")
            current_ad_id = self._last_target.get("adId")
            try:
                if current_aavid and current_ad_id:
                    fetch_url = build_qianchuan_url_by_params(
                        base_url=cfg.base_url,
                        aavid=int(current_aavid),
                        ad_id=int(current_ad_id),
                    )
            except Exception as e:
                self._log(f"[URL] 重新构建抓取URL失败：{e}")
                continue

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
                self._log("[抓取] 开始一轮抓取")
                fa, fp, ft = snapshot_feishu_bitable_for_fetch()
                fs_panel = load_feishu_bitable_panel_config()
                fpm = fs_panel.get("push_mode") or "each_crawl"
                await fetcher.fetch(
                    fetch_url,
                    db=db,
                    timeout=int(cfg.round_timeout),
                    feishu_app_token=fa,
                    feishu_personal_base_token=fp,
                    feishu_table_id=ft,
                    feishu_push_mode=fpm,
                    cloud_backup_username=self._cloud_backup_username,
                    cloud_backup_password=self._cloud_backup_password,
                )
                # 保存 cookie
                try:
                    if fetcher.context:
                        _ensure_data_dir()
                        await fetcher.context.storage_state(path=cfg.cookie_path)
                except Exception:
                    pass
                self._last_fetch_time = time.time()
                self._log(f"[抓取] 完成")
                try:
                    await self._maybe_feishu_hourly_push_after_fetch(
                        db, fa, fp, ft, str(current_aavid or "").strip() or None
                    )
                except Exception as e:
                    self._log(f"[飞书·整点] 检查/同步异常（已忽略）：{e}")
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

    async def _wait_for_target_url(self, fetcher: QianChuanFetcher, cfg: ServiceConfig) -> Optional[str]:
        prefix = (cfg.wait_url_prefix or "").strip()
        while not self._stop_event.is_set():
            try:
                # 注意：fetcher.page 可能被 “新标签页” 回调更新
                cur = (fetcher.page.url or "").strip()
            except Exception:
                cur = ""

            if cur and (not prefix or cur.startswith(prefix)):
                aavid, ad_id = _extract_aavid_adid(cur)
                if aavid and ad_id:
                    return cur

            await asyncio.sleep(0.5)
        return None


_GLOBAL_CONTROLLER: Optional[ServiceController] = None


def get_service_controller() -> ServiceController:
    global _GLOBAL_CONTROLLER
    if _GLOBAL_CONTROLLER is None:
        _GLOBAL_CONTROLLER = ServiceController()
    return _GLOBAL_CONTROLLER

