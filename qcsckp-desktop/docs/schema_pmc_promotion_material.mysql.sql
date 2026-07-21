-- =============================================================================
-- 千川素材看盘 - 与本地 SQLite 表 pmc_promotion_material 对齐的 MySQL 建表脚本
-- 依据：utils/sqlite_store.py TABLE_SCHEMAS、services/fetcher.py 写入逻辑
-- 说明：
--   1) 时区：SQLite 使用 datetime('now', '+8 hours')；MySQL 建议在会话或服务器层设为东八区，
--      或应用层写入 created_at/updated_at 时显式使用北京时间。
--   2) stat_date 由程序写入当日 YYYY-MM-DD；stat_cost 等为接口浮点，与 SQLite REAL 一致用 DOUBLE。
-- =============================================================================

SET NAMES utf8mb4;

CREATE TABLE IF NOT EXISTS `pmc_promotion_material` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '本地自增主键',
  `aadvid` VARCHAR(64) NOT NULL COMMENT '广告主ID，来自 URL aavid',
  `material_id` VARCHAR(64) NOT NULL COMMENT '素材ID，dimensions.materialId',
  `video_name` VARCHAR(1024) DEFAULT NULL COMMENT 'roi2MaterialVideoName',
  `material_status` INT DEFAULT NULL COMMENT 'roi2MaterialStatus',
  `show_status` INT DEFAULT NULL COMMENT 'roi2MaterialShowStatus',
  `show_status_reason` VARCHAR(512) DEFAULT NULL COMMENT 'roi2MaterialShowStatusReason',
  `upload_time` VARCHAR(64) DEFAULT NULL COMMENT 'roi2MaterialUploadTime',
  `video_type` INT DEFAULT NULL COMMENT 'roi2MaterialVideoType',
  `video_id` VARCHAR(128) DEFAULT NULL COMMENT 'VideoPlayInfo.VideoId',
  `aweme_item_id` BIGINT DEFAULT NULL COMMENT 'VideoPlayInfo.AwemeItemId',
  `cover_url` VARCHAR(2048) DEFAULT NULL COMMENT 'CoverImage.WebUrl',
  `cover_width` INT DEFAULT NULL,
  `cover_height` INT DEFAULT NULL,
  `video_duration` INT DEFAULT NULL,
  `video_title` VARCHAR(1024) DEFAULT NULL,
  `lego_source` INT DEFAULT NULL,
  `video_create_time` VARCHAR(64) DEFAULT NULL COMMENT 'VideoPlayInfo.CreateTime',
  `tag_list` TEXT COMMENT 'materialTagList 解析后逗号拼接',
  `stat_cost` DOUBLE DEFAULT NULL COMMENT 'statCostForRoi2',
  `order_settle_count_1h` INT DEFAULT NULL COMMENT 'totalOrderSettleCountForRoi21H',
  `order_settle_amount_1h` DOUBLE DEFAULT NULL,
  `order_settle_rate_1h` DOUBLE DEFAULT NULL,
  `prepay_pay_order_count` DOUBLE DEFAULT NULL COMMENT 'totalPrepayAndPayOrderRoi2',
  `pay_gmv_include_coupon` DOUBLE DEFAULT NULL,
  `prepay_pay_settle_1h` DOUBLE DEFAULT NULL,
  `refund_rate_1h` DOUBLE DEFAULT NULL,
   `overall_show_count` bigint(20) DEFAULT NULL COMMENT '整体展现次数',
  `overall_click_count` bigint(20) DEFAULT NULL COMMENT '整体点击次数',
  `overall_ctr` double DEFAULT NULL COMMENT '整体点击率',
  `overall_conversion_rate` double DEFAULT NULL COMMENT '整体转化率',
  `stat_date` DATE NOT NULL COMMENT '入库统计日 YYYY-MM-DD（程序写入）',
  `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '入库时间（对齐 SQLite +8 时请用会话时区或应用写入）',
  `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_material_aadvid` (`aadvid`),
  KEY `idx_material_stat_date` (`stat_date`),
  KEY `idx_material_id` (`material_id`),
  KEY `idx_material_video_type` (`video_type`),
  KEY `idx_material_status` (`material_status`),
  KEY `idx_material_created_at` (`created_at`),
  KEY `idx_material_perf_lead` (`created_at`, `material_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
COMMENT='千川 uni-promotion 素材列表拦截入库（与本地 qianchuan.db 一致）';
