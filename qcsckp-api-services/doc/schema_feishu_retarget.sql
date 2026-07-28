-- 测试1版：飞书确认追投云端表。
-- 正式发布时先备份数据库，再在现有业务库中执行；不包含任何账号或飞书凭据。

CREATE TABLE IF NOT EXISTS `desktop_device_sessions` (
  `id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `user_id` int unsigned NOT NULL,
  `token_hash` char(64) NOT NULL,
  `device_name` varchar(120) NOT NULL DEFAULT '',
  `expires_at` datetime NOT NULL,
  `revoked_at` datetime DEFAULT NULL,
  `last_seen_at` datetime DEFAULT NULL,
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_device_token_hash` (`token_hash`),
  KEY `idx_device_user` (`user_id`),
  KEY `idx_device_expiry` (`expires_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `retarget_tasks` (
  `id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `task_uid` char(36) NOT NULL,
  `user_id` int unsigned NOT NULL,
  `active_dedupe_key` char(64) DEFAULT NULL,
  `aavid` varchar(64) NOT NULL,
  `account_name` varchar(200) NOT NULL DEFAULT '',
  `ad_id` varchar(64) NOT NULL,
  `target_uid` varchar(64) NOT NULL DEFAULT 'legacy_unscoped',
  `plan_name` varchar(256) NOT NULL DEFAULT '',
  `promotion_scene` varchar(32) NOT NULL DEFAULT 'live',
  `plan_system` varchar(32) NOT NULL DEFAULT 'unknown',
  `trigger_level` varchar(32) NOT NULL DEFAULT 'material',
  `product_id` varchar(128) NOT NULL DEFAULT '',
  `product_name` varchar(512) NOT NULL DEFAULT '',
  `material_id` varchar(128) NOT NULL,
  `material_name` varchar(512) NOT NULL DEFAULT '',
  `strategy_id` varchar(128) NOT NULL,
  `strategy_name` varchar(128) NOT NULL DEFAULT '',
  `strategy_hash` char(64) NOT NULL,
  `status` varchar(32) NOT NULL,
  `action_nonce` char(64) NOT NULL,
  `trigger_snapshot_json` longtext,
  `query_snapshot_json` longtext,
  `retargeting_json` longtext,
  `rule_snapshot_json` longtext,
  `clicker_open_id` varchar(128) DEFAULT NULL,
  `claimed_device` varchar(120) DEFAULT NULL,
  `claim_token` char(64) DEFAULT NULL,
  `lease_expires_at` datetime DEFAULT NULL,
  `regulate_task_id` varchar(128) DEFAULT NULL,
  `result_message` text,
  `result_detail` longtext,
  `result_json` longtext,
  `approved_at` datetime DEFAULT NULL,
  `started_at` datetime DEFAULT NULL,
  `finished_at` datetime DEFAULT NULL,
  `expires_at` datetime NOT NULL,
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_retarget_task_uid` (`task_uid`),
  UNIQUE KEY `uk_retarget_active_dedupe` (`active_dedupe_key`),
  KEY `idx_retarget_user_status` (`user_id`,`status`,`expires_at`),
  KEY `idx_retarget_target_time` (`user_id`,`target_uid`,`created_at`),
  KEY `idx_retarget_account_time` (`user_id`,`aavid`,`created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `retarget_task_messages` (
  `id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `task_id` bigint unsigned NOT NULL,
  `receive_id_type` varchar(32) NOT NULL,
  `receive_id` varchar(128) NOT NULL,
  `message_id` varchar(128) NOT NULL,
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_retarget_task_message` (`task_id`,`message_id`),
  KEY `idx_retarget_message_task` (`task_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `retarget_card_update_jobs` (
  `id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `task_id` bigint unsigned NOT NULL,
  `expanded` tinyint(1) NOT NULL DEFAULT 0,
  `attempts` int unsigned NOT NULL DEFAULT 0,
  `available_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `locked_at` datetime DEFAULT NULL,
  `last_error` varchar(1000) DEFAULT NULL,
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_retarget_card_update_task` (`task_id`),
  KEY `idx_retarget_card_update_due` (`available_at`,`locked_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
