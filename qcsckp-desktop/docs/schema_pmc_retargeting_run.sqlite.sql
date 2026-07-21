-- 千川素材追投执行流水（SQLite）
-- 与 services/retargeting_service.RetargetingRunResult / to_log_dict() 对齐，并补充耗时、来源等审计字段。
-- 自动建表以 utils/sqlite_store.TABLE_SCHEMAS['pmc_retargeting_run'] 为准；本文件便于审阅与手工执行。
--
-- 状态 status：1 = 成功，-1 = 失败（与业务约定一致）
-- 时间：业务侧 started_at / ended_at 建议统一用北京时间字符串；created_at 默认用 UTC+8（不依赖操作系统时区）

CREATE TABLE IF NOT EXISTS pmc_retargeting_run (
  id INTEGER PRIMARY KEY AUTOINCREMENT,

  aavid TEXT NOT NULL,
  ad_id TEXT NOT NULL,
  material_id TEXT NOT NULL,

  material_name TEXT,
  /* 素材名称：规则追投时来自大屏素材行 title（video_name）；其它入口可为空 */

  strategy_name TEXT,
  /* 策略展示名：与规则页策略 title 一致；旧数据或空时前端默认「策略 1」 */

  regulate_task_id TEXT,
  /* 调控任务 id：来自提交后 create-uni-prom-assist-task 接口 data.id；失败或未返回时为空 */

  -- ---------- 时间 / 耗时 ----------
  started_at TEXT NOT NULL,
  ended_at TEXT NOT NULL,
  duration_ms INTEGER,
  /* duration_ms = 从 started_at 到 ended_at 的毫秒数，入库时由程序计算写入，便于按耗时筛选 */

  -- ---------- 结果（与 to_log_dict 对应） ----------
  status INTEGER NOT NULL CHECK (status IN (-1, 1)),
  /* 1 成功，-1 失败 */

  step TEXT,
  /* 阶段：validate | build_url | browser | search_material | fill_or_submit | submit_api | done | exception */

  message TEXT,
  /* 一行摘要：成功说明或失败原因（人可读） */

  detail TEXT,
  /* 失败时的完整信息：页面校验文案、异常堆栈、补充 JSON 等；成功时可为空或写平台提示 */

  retargeting_method TEXT,
  /* volume | cost_control，与 rule 中 retargeting.method 一致 */

  optimization_goal TEXT,
  /* cost_control 时：net_roi | live_room；放量或其它可为空 */

  retargeting_json TEXT,
  /* 本次生效的 retargeting 对象 JSON 快照（与入参一致，便于复盘） */

  rule_full_json TEXT,
  /* 可选：整条 rule_retargeting.json 快照（含 trigger、interval 等），自动追投接入后便于审计 */

  trigger_snapshot_json TEXT,
  /* 触发条件配置 + 当次求值明细（各条件 actual/threshold 等），JSON 字符串 */

  query_snapshot_json TEXT,
  /* 当次大屏查询上下文：周期、query_at、period 文案、素材行快照等，JSON 字符串 */

  headless INTEGER NOT NULL DEFAULT 0 CHECK (headless IN (0, 1)),
  /* 实际 Playwright 是否无头 */

  browser_headless_rule INTEGER CHECK (browser_headless_rule IN (0, 1)),
  /* 可选：规则文件里的 browser_headless，便于对比「配置无头 vs 实际无头」 */

  -- ---------- 来源 / 环境 ----------
  trigger_source TEXT,
  /* manual | api | scheduler | rule_engine | test 等，由调用方写入 */

  app_version TEXT,
  /* 应用版本号，可选 */

  -- ---------- 行级时间戳 ----------
  created_at TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))
  /* 入库时间：SQLite now 为 UTC，+8 hours 为北京时间（CST），与 datetime('now','localtime') 解耦 */
);

-- 按时间查最近流水
CREATE INDEX IF NOT EXISTS idx_pmc_retargeting_run_started
  ON pmc_retargeting_run (started_at DESC);

-- 按账号 + 素材排查
CREATE INDEX IF NOT EXISTS idx_pmc_retargeting_run_account_material
  ON pmc_retargeting_run (aavid, material_id, started_at DESC);

-- 按成败统计
CREATE INDEX IF NOT EXISTS idx_pmc_retargeting_run_status_time
  ON pmc_retargeting_run (status, started_at DESC);
