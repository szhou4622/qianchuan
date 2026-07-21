-- 规则追投限频状态（每素材一行，material_id 唯一）
-- use_count：当前 limit_started_at 起算窗口内「追投成功」次数（失败不占额）。
-- 窗口长度、最大次数由 data/rule_retargeting.json 中 retargeting.interval 提供，本表不存。
-- 时间：created_at / updated_at / limit_started_at 为北京时间字符串（与业务侧一致）。
-- material_id 通过 UNIQUE 索引查询（与 uk_pmc_retargeting_rate_limit_material_id 一致）。

CREATE TABLE IF NOT EXISTS pmc_retargeting_rate_limit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  material_id TEXT NOT NULL,
  limit_started_at TEXT NOT NULL,
  use_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now', '+8 hours')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))
);

CREATE UNIQUE INDEX IF NOT EXISTS uk_pmc_retargeting_rate_limit_material_id
  ON pmc_retargeting_rate_limit (material_id);
