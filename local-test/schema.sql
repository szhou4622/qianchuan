CREATE DATABASE IF NOT EXISTS `{{DB_NAME}}`
  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE USER IF NOT EXISTS 'qcsckp_local'@'127.0.0.1'
  IDENTIFIED BY '{{DB_PASSWORD}}';
ALTER USER 'qcsckp_local'@'127.0.0.1'
  IDENTIFIED BY '{{DB_PASSWORD}}';
GRANT ALL PRIVILEGES ON `{{DB_NAME}}`.* TO 'qcsckp_local'@'127.0.0.1';
FLUSH PRIVILEGES;

USE `{{DB_NAME}}`;

CREATE TABLE IF NOT EXISTS `accounts` (
  `id` int unsigned NOT NULL AUTO_INCREMENT,
  `username` varchar(100) NOT NULL,
  `password_hash` varchar(255) NOT NULL,
  `role` varchar(32) NOT NULL DEFAULT 'user',
  `parent_id` int unsigned DEFAULT NULL,
  `valid_from` datetime DEFAULT NULL,
  `valid_until` datetime DEFAULT NULL,
  `is_disabled` tinyint(1) NOT NULL DEFAULT 0,
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_accounts_username` (`username`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `desktop_releases` (
  `id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `platform` varchar(32) NOT NULL,
  `kind` varchar(32) NOT NULL,
  `version` varchar(64) NOT NULL,
  `storage_name` varchar(255) NOT NULL,
  `original_filename` varchar(255) NOT NULL,
  `file_size` bigint unsigned NOT NULL DEFAULT 0,
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_desktop_release_platform_kind` (`platform`,`kind`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `pmc_ad_detail_basic` (
  `id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `aadvid` varchar(64) NOT NULL,
  `ad_id` varchar(64) NOT NULL,
  `budget` varchar(64) DEFAULT NULL,
  `audience_coverage_count` varchar(64) DEFAULT NULL,
  `compensation_convert` varchar(64) DEFAULT NULL,
  `ecp_roi2_goal` decimal(18,6) DEFAULT NULL,
  `creative_type` int DEFAULT NULL,
  `user_info_id` varchar(64) DEFAULT NULL,
  `user_info_name` varchar(255) DEFAULT NULL,
  `user_info_unique_id` varchar(255) DEFAULT NULL,
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_pmc_ad_detail_basic_account_plan` (`aadvid`,`ad_id`),
  KEY `idx_pmc_ad_detail_basic_aadvid` (`aadvid`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

INSERT INTO `accounts`
  (`username`,`password_hash`,`role`,`parent_id`,`valid_from`,`valid_until`,`is_disabled`)
VALUES
  ('local_test','{{ACCOUNT_PASSWORD_HASH}}','user',NULL,NOW(),DATE_ADD(NOW(),INTERVAL 365 DAY),0)
ON DUPLICATE KEY UPDATE
  `password_hash`=VALUES(`password_hash`),
  `role`='user',
  `parent_id`=NULL,
  `valid_from`=NOW(),
  `valid_until`=DATE_ADD(NOW(),INTERVAL 365 DAY),
  `is_disabled`=0;
