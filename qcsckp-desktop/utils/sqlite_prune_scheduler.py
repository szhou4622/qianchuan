"""
后台裁剪 SQLite：按主键 id 控制 pmc_promotion_material 行数。
由 gui_app 启动守护线程；策略见 config.py（北京时间、触发阈值、每日夜间时段可跨午夜、限流删除）。
启动时若行数已超过触发线，会先按「每轮限流 + 轮间隔」同步裁剪至保留目标行数，再进入定时循环。
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timedelta

from config import (
    SQLITE_PRUNE_ENABLED,
    SQLITE_PRUNE_MAX_ROWS,
    SQLITE_PRUNE_TRIGGER_ROWS,
    SQLITE_PRUNE_DAILY_START_HOUR,
    SQLITE_PRUNE_DAILY_END_HOUR,
    SQLITE_PRUNE_MAX_ROWS_PER_CYCLE,
    SQLITE_PRUNE_CYCLE_INTERVAL_SEC,
    SQLITE_PRUNE_DELETE_BATCH_SIZE,
    SQLITE_PRUNE_BATCH_SLEEP_SEC,
    SQLITE_PRUNE_LOOP_IDLE_SEC,
    SQLITE_PRUNE_START_DELAY_SEC,
    DB_FILE,
)
from utils.log import logger
from utils.sqlite_store import SQLiteStore


def _beijing_now() -> datetime:
    """与库表 created_at 一致：UTC+8（无时区对象，仅本地算术）。"""
    return datetime.utcnow() + timedelta(hours=8)


def _in_daily_prune_window(now_bj: datetime) -> bool:
    """北京时间是否在 [start, end) 内；若 start > end 则视为跨午夜（如 22–次日 6）。"""
    h = now_bj.hour
    sh, eh = SQLITE_PRUNE_DAILY_START_HOUR, SQLITE_PRUNE_DAILY_END_HOUR
    if sh < eh:
        return sh <= h < eh
    if sh > eh:
        return h >= sh or h < eh
    return False


def run_startup_prune_if_over_trigger() -> None:
    """
    进程启动时调用：若行数已超过触发线，按与夜间相同的限流多轮裁剪至保留 SQLITE_PRUNE_MAX_ROWS 行：
    每轮最多删 SQLITE_PRUNE_MAX_ROWS_PER_CYCLE，轮间隔 SQLITE_PRUNE_CYCLE_INTERVAL_SEC。
    """
    if not SQLITE_PRUNE_ENABLED:
        return
    if not os.path.exists(DB_FILE):
        return
    try:
        store = SQLiteStore(database=DB_FILE)
        cnt0 = store.count("pmc_promotion_material")
        if cnt0 <= SQLITE_PRUNE_TRIGGER_ROWS:
            return
        logger.info(
            f"[SQLite 裁剪] 启动检查：当前 {cnt0} 行 > 触发线 {SQLITE_PRUNE_TRIGGER_ROWS}，"
            f"限流裁剪至保留 {SQLITE_PRUNE_MAX_ROWS} 行（每轮≤{SQLITE_PRUNE_MAX_ROWS_PER_CYCLE}，"
            f"间隔 {SQLITE_PRUNE_CYCLE_INTERVAL_SEC}s）…"
        )
        total_deleted = 0
        round_no = 0
        while True:
            store = SQLiteStore(database=DB_FILE)
            cnt = store.count("pmc_promotion_material")
            if cnt <= SQLITE_PRUNE_MAX_ROWS:
                break
            round_no += 1
            deleted = store.prune_table_keep_latest_by_id_limited(
                "pmc_promotion_material",
                max_rows=SQLITE_PRUNE_MAX_ROWS,
                max_delete=SQLITE_PRUNE_MAX_ROWS_PER_CYCLE,
                batch_size=SQLITE_PRUNE_DELETE_BATCH_SIZE,
                sleep_between_batches_sec=float(SQLITE_PRUNE_BATCH_SLEEP_SEC),
            )
            total_deleted += deleted
            cnt_after = store.count("pmc_promotion_material")
            if deleted:
                logger.info(
                    f"[SQLite 裁剪] 启动裁剪 第{round_no}轮 删除 {deleted} 行，累计删 {total_deleted}，"
                    f"当前约 {cnt_after} 行"
                )
            if deleted == 0:
                logger.warning("[SQLite 裁剪] 启动裁剪本轮未删行，停止")
                break
            if cnt_after <= SQLITE_PRUNE_MAX_ROWS:
                break
            time.sleep(SQLITE_PRUNE_CYCLE_INTERVAL_SEC)
        if total_deleted:
            store = SQLiteStore(database=DB_FILE)
            after = store.count("pmc_promotion_material")
            logger.info(
                f"[SQLite 裁剪] 启动裁剪结束，共删 {total_deleted} 行，当前约 {after} 行"
            )
    except Exception as e:
        logger.warning(f"[SQLite 裁剪] 启动裁剪失败: {e}")


def _prune_loop() -> None:
    time.sleep(max(0, SQLITE_PRUNE_START_DELAY_SEC))

    while True:
        idle_sleep = SQLITE_PRUNE_LOOP_IDLE_SEC
        try:
            if not os.path.exists(DB_FILE):
                time.sleep(idle_sleep)
                continue

            now_bj = _beijing_now()

            if not _in_daily_prune_window(now_bj):
                time.sleep(idle_sleep)
                continue

            store = SQLiteStore(database=DB_FILE)
            cnt = store.count("pmc_promotion_material")

            if cnt <= SQLITE_PRUNE_TRIGGER_ROWS:
                time.sleep(idle_sleep)
                continue

            deleted = store.prune_table_keep_latest_by_id_limited(
                "pmc_promotion_material",
                max_rows=SQLITE_PRUNE_MAX_ROWS,
                max_delete=SQLITE_PRUNE_MAX_ROWS_PER_CYCLE,
                batch_size=SQLITE_PRUNE_DELETE_BATCH_SIZE,
                sleep_between_batches_sec=float(SQLITE_PRUNE_BATCH_SLEEP_SEC),
            )
            if deleted:
                logger.info(
                    f"[SQLite 裁剪] 本轮删除 {deleted} 行，裁剪前约 {cnt} 行，"
                    f"目标保留 {SQLITE_PRUNE_MAX_ROWS} 行（触发线 {SQLITE_PRUNE_TRIGGER_ROWS}）"
                )
        except Exception as e:
            logger.warning(f"[SQLite 裁剪] 失败: {e}")

        time.sleep(SQLITE_PRUNE_CYCLE_INTERVAL_SEC)


def _prune_worker() -> None:
    """先执行启动时超限裁剪（不阻塞主线程 / GUI），再进入每日定时限流循环。"""
    run_startup_prune_if_over_trigger()
    _prune_loop()


def start_sqlite_prune_background_thread() -> threading.Thread | None:
    """
    守护线程：每日北京时间窗口内（默认 22:00–次日 6:00，见 config）且行数 > 触发线时按限流删除；
    每轮最多 SQLITE_PRUNE_MAX_ROWS_PER_CYCLE，轮间隔 SQLITE_PRUNE_CYCLE_INTERVAL_SEC。

    启动时若需裁剪，在后台线程执行，避免阻塞 webview 窗口显示。
    """
    if not SQLITE_PRUNE_ENABLED:
        logger.info("SQLite 定期裁剪已关闭（SQLITE_PRUNE_ENABLED=false）")
        return None
    t = threading.Thread(target=_prune_worker, name="sqlite-prune", daemon=True)
    t.start()
    _wh = (
        f"{SQLITE_PRUNE_DAILY_START_HOUR}:00–次日{SQLITE_PRUNE_DAILY_END_HOUR}:00"
        if SQLITE_PRUNE_DAILY_START_HOUR > SQLITE_PRUNE_DAILY_END_HOUR
        else f"{SQLITE_PRUNE_DAILY_START_HOUR}:00–{SQLITE_PRUNE_DAILY_END_HOUR}:00"
    )
    logger.info(
        f"[SQLite 裁剪] 已启动：北京 {_wh}，"
        f"保留 {SQLITE_PRUNE_MAX_ROWS} 行、触发 {SQLITE_PRUNE_TRIGGER_ROWS} 行，"
        f"每轮≤{SQLITE_PRUNE_MAX_ROWS_PER_CYCLE} 行、间隔 {SQLITE_PRUNE_CYCLE_INTERVAL_SEC}s，"
        f"批 {SQLITE_PRUNE_DELETE_BATCH_SIZE}/间隔 {SQLITE_PRUNE_BATCH_SLEEP_SEC}s"
    )
    return t
