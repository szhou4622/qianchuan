-- 千川规则化停投执行流水（SQLite）
-- 与 pmc_roi2_assist_task 对齐：assist_task_id、task_name；素材明细见 query_snapshot_json。
-- 自动建表以 utils/sqlite_store.TABLE_SCHEMAS['pmc_regulation_run'] 为准。

CREATE TABLE IF NOT EXISTS pmc_regulation_run (
  id INTEGER PRIMARY KEY AUTOINCREMENT,

  aavid TEXT NOT NULL,
  ad_id TEXT NOT NULL,

  assist_task_id TEXT,
  /* 与 pmc_roi2_assist_task.assist_task_id 一致，停投操作针对的调控任务 id */

  task_name TEXT,
  /* 与 pmc_roi2_assist_task.task_name 一致，任务展示名 */

  strategy_name TEXT,
  /* 本工具 rule_regulation.json 中的策略标题 */

  stop_action TEXT,
  /* pause | delete，与 rule_regulation 策略 regulation_stop_action 一致 */

  started_at TEXT NOT NULL,
  ended_at TEXT NOT NULL,
  duration_ms INTEGER,

  /* status: 1 成功 -1 失败 2 跳过（如 done_already_paused，无需再暂停/删除） */
  status INTEGER NOT NULL CHECK (status IN (-1, 1, 2)),
  step TEXT,
  message TEXT,
  detail TEXT,

  rule_full_json TEXT,
  trigger_snapshot_json TEXT,
  query_snapshot_json TEXT,

  headless INTEGER NOT NULL DEFAULT 0 CHECK (headless IN (0, 1)),
  browser_headless_rule INTEGER CHECK (browser_headless_rule IN (0, 1)),

  trigger_source TEXT,
  app_version TEXT,

  created_at TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))
);

CREATE INDEX IF NOT EXISTS idx_pmc_regulation_run_started
  ON pmc_regulation_run (started_at DESC);

CREATE INDEX IF NOT EXISTS idx_pmc_regulation_run_aavid_time
  ON pmc_regulation_run (aavid, started_at DESC);

CREATE INDEX IF NOT EXISTS idx_pmc_regulation_run_status_time
  ON pmc_regulation_run (status, started_at DESC);
