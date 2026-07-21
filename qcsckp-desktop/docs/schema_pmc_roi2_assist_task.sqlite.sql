-- =============================================================================
-- 千川全域 · 素材追投/调控任务
-- 依据：dev_files/ad_tiaokong.json、dev_files/mapping.json
-- 说明：
--   1) assist_task_id = adInfos.id；aadvid / ad_id = 采集上下文与 adInfos.advId
--   2) start_time / end_time / modify_time / create_time：入库为东八区 YYYY-MM-DD HH:mm:ss（TEXT），由 Unix 秒转换
--   3) 调控指标列与 mapping Name（snake_case）一致，存 adStatsMap metrics.*.value
--   4) assist_materials_json = [{"material_id","title"},...]
--   5) created_at/updated_at：本地入库时间，东八区
-- =============================================================================

CREATE TABLE IF NOT EXISTS pmc_roi2_assist_task (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    assist_task_id TEXT NOT NULL,
    aadvid TEXT NOT NULL,
    ad_id TEXT NOT NULL,

    -- adInfos（与接口字段对应）
    task_name TEXT,
    budget TEXT,
    bid TEXT,
    start_time TEXT,
    end_time TEXT,
    modify_time TEXT,
    create_time TEXT,
    order_id TEXT,
    aggregate_cid TEXT,
    ecp_roi2_goal REAL,
    creator_user_id TEXT,
    mar_goal INTEGER,
    prom_way INTEGER,
    external_action INTEGER,
    external_action_name TEXT,
    smart_bid_type INTEGER,
    budget_mode INTEGER,
    ad_cost_protect_status INTEGER,
    deep_external_action INTEGER,
    lab_ad_type INTEGER,
    deep_external_action_name TEXT,
    deep_bid_type INTEGER,
    qcpx_mode INTEGER,
    adlab_mode INTEGER,
    ad_delivery_type INTEGER,
    ad_delivery_name TEXT,
    ad_opt_type INTEGER,
    hint_type INTEGER,
    learning_phase INTEGER,
    daily_delivery_seconds INTEGER,

    show_cnt_for_roi2_assist REAL,
    click_cnt_for_roi2_assist REAL,
    ctr_for_roi2_assist REAL,
    convert_rate_for_roi2_assist REAL,
    stat_cost_for_roi2_assist REAL,
    total_pay_order_count_for_roi2_assist REAL,
    total_pay_order_gmv_include_coupon_for_roi2_assist REAL,
    total_prepay_and_pay_order_roi2_assist REAL,
    total_cost_per_pay_order_for_roi2_assist REAL,
    pay_convert_cost_for_roi2_assist REAL,
    pay_convert_cnt_for_roi2_assist REAL,
    total_order_settle_amount_for_roi2_1h_assist REAL,
    total_refund_order_gmv_for_roi2_1h_rate_assist REAL,
    total_prepay_and_pay_settle_roi2_1h_assist REAL,
    total_pay_order_gmv_for_roi2_assist REAL,
    total_pay_order_coupon_amount_for_roi2_assist REAL,

    assist_materials_json TEXT,

    created_at TEXT NOT NULL DEFAULT (datetime('now', '+8 hours')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now', '+8 hours')),
    CONSTRAINT uk_pmc_roi2_assist_task_assist_id UNIQUE (assist_task_id)
);

CREATE INDEX IF NOT EXISTS idx_pmc_roi2_assist_aadvid ON pmc_roi2_assist_task (aadvid);
CREATE INDEX IF NOT EXISTS idx_pmc_roi2_assist_created ON pmc_roi2_assist_task (created_at);
