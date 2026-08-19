<?php
declare(strict_types=1);

/**
 * 桌面端本地采集数据 → 云端 MySQL 备份（表 pmc_promotion_material，字段见 doc/DATABASE_FIELDS.md）
 * POST JSON：username、password、rows（行对象数组，字段名与文档一致；服务端写入 user_id）
 * 首次请求若表不存在则自动 CREATE TABLE；若表已存在但缺少「整体展现/点击/点击率/转化率」列则自动 ALTER 补齐，再执行写入。
 */

header('Content-Type: application/json; charset=utf-8');

require_once dirname(__DIR__) . '/includes/bootstrap.php';
require_once dirname(__DIR__) . '/includes/helpers.php';

const PMC_BACKUP_MAX_ROWS = 2000;

/**
 * 与 database.sql / doc/schema_pmc_promotion_material.mysql.sql 保持一致。
 */
function pmc_promotion_material_ddl(): string
{
    return <<<'SQL'
CREATE TABLE `pmc_promotion_material` (
  `id` bigint unsigned NOT NULL AUTO_INCREMENT COMMENT '云端自增主键',
  `user_id` int unsigned NOT NULL COMMENT '备份所属普通用户 accounts.id',
  `aadvid` varchar(64) NOT NULL COMMENT '广告主ID',
  `material_id` varchar(64) NOT NULL COMMENT '素材ID',
  `video_name` varchar(1024) DEFAULT NULL,
  `material_status` int DEFAULT NULL,
  `show_status` int DEFAULT NULL,
  `show_status_reason` varchar(512) DEFAULT NULL,
  `upload_time` varchar(64) DEFAULT NULL,
  `video_type` int DEFAULT NULL,
  `video_id` varchar(128) DEFAULT NULL,
  `aweme_item_id` bigint DEFAULT NULL,
  `cover_url` varchar(2048) DEFAULT NULL,
  `cover_width` int DEFAULT NULL,
  `cover_height` int DEFAULT NULL,
  `video_duration` int DEFAULT NULL,
  `video_title` varchar(1024) DEFAULT NULL,
  `lego_source` int DEFAULT NULL,
  `video_create_time` varchar(64) DEFAULT NULL,
  `tag_list` text COMMENT '逗号拼接',
  `stat_cost` double DEFAULT NULL,
  `order_settle_count_1h` int DEFAULT NULL,
  `order_settle_amount_1h` double DEFAULT NULL,
  `order_settle_rate_1h` double DEFAULT NULL,
  `prepay_pay_order_count` double DEFAULT NULL,
  `pay_gmv_include_coupon` double DEFAULT NULL,
  `prepay_pay_settle_1h` double DEFAULT NULL,
  `refund_rate_1h` double DEFAULT NULL,
  `overall_order_count` int DEFAULT NULL COMMENT '整体成交订单数',
  `overall_show_count` bigint DEFAULT NULL COMMENT '整体展现次数',
  `overall_click_count` bigint DEFAULT NULL COMMENT '整体点击次数',
  `overall_ctr` double DEFAULT NULL COMMENT '整体点击率',
  `overall_conversion_rate` double DEFAULT NULL COMMENT '整体转化率',
  `stat_date` date NOT NULL COMMENT '统计日 YYYY-MM-DD',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_pmc_user` (`user_id`),
  KEY `idx_pmc_aadvid` (`aadvid`),
  KEY `idx_pmc_stat_date` (`stat_date`),
  KEY `idx_pmc_material_id` (`material_id`),
  KEY `idx_pmc_video_type` (`video_type`),
  KEY `idx_pmc_material_status` (`material_status`),
  KEY `idx_pmc_created_at` (`created_at`),
  KEY `idx_pmc_created_material` (`created_at`,`material_id`),
  KEY `idx_pmc_user_stat` (`user_id`,`stat_date`),
  UNIQUE KEY `uk_pmc_backup_row` (`user_id`,`aadvid`,`material_id`,`stat_date`,`created_at`),
  CONSTRAINT `fk_pmc_user` FOREIGN KEY (`user_id`) REFERENCES `accounts` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
COMMENT='千川素材数据云端备份'
SQL;
}

function ensure_pmc_promotion_material_table(PDO $pdo): void
{
    $check = $pdo->query("SHOW TABLES LIKE 'pmc_promotion_material'");
    if ($check !== false && $check->fetch() !== false) {
        return;
    }
    $pdo->exec(pmc_promotion_material_ddl());
}

function pmc_column_exists(PDO $pdo, string $table, string $column): bool
{
    $dbName = $pdo->query('SELECT DATABASE()')->fetchColumn();
    if (!is_string($dbName) || $dbName === '') {
        return false;
    }
    $st = $pdo->prepare(
        'SELECT COUNT(*) FROM information_schema.COLUMNS
         WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ? AND COLUMN_NAME = ?'
    );
    $st->execute([$dbName, $table, $column]);
    return (int) $st->fetchColumn() > 0;
}

/**
 * 旧库仅有早期列时，增量补齐「整体成交订单数」及「整体展现/点击/点击率/转化率」列（可空）。
 */
function pmc_ensure_overall_metric_columns(PDO $pdo): void
{
    $table = 'pmc_promotion_material';
    $defs = [
        'overall_order_count' => 'int DEFAULT NULL COMMENT \'整体成交订单数\'',
        'overall_show_count' => 'bigint DEFAULT NULL COMMENT \'整体展现次数\'',
        'overall_click_count' => 'bigint DEFAULT NULL COMMENT \'整体点击次数\'',
        'overall_ctr' => 'double DEFAULT NULL COMMENT \'整体点击率\'',
        'overall_conversion_rate' => 'double DEFAULT NULL COMMENT \'整体转化率\'',
    ];
    foreach ($defs as $col => $typeSql) {
        if (!pmc_column_exists($pdo, $table, $col)) {
            $pdo->exec("ALTER TABLE `$table` ADD COLUMN `$col` $typeSql");
        }
    }
}

function ensure_pmc_promotion_material_schema(PDO $pdo): void
{
    ensure_pmc_promotion_material_table($pdo);
    pmc_ensure_overall_metric_columns($pdo);
    $dbName = $pdo->query('SELECT DATABASE()')->fetchColumn();
    if (!is_string($dbName) || $dbName === '') {
        throw new RuntimeException('无法识别当前数据库');
    }
    $st = $pdo->prepare(
        'SELECT COUNT(*) FROM information_schema.STATISTICS '
        . 'WHERE TABLE_SCHEMA=? AND TABLE_NAME=? AND INDEX_NAME=?'
    );
    $st->execute([$dbName, 'pmc_promotion_material', 'uk_pmc_backup_row']);
    if ((int) $st->fetchColumn() === 0) {
        $pdo->exec(
            'DELETE older FROM pmc_promotion_material older '
            . 'INNER JOIN pmc_promotion_material newer ON '
            . 'newer.user_id=older.user_id AND newer.aadvid=older.aadvid '
            . 'AND newer.material_id=older.material_id AND newer.stat_date=older.stat_date '
            . 'AND newer.created_at=older.created_at AND newer.id>older.id'
        );
        $pdo->exec(
            'ALTER TABLE `pmc_promotion_material` ADD UNIQUE KEY `uk_pmc_backup_row` '
            . '(`user_id`,`aadvid`,`material_id`,`stat_date`,`created_at`)'
        );
    }
}

/**
 * @param mixed $v
 */
function pmc_opt_int($v): ?int
{
    if ($v === null) {
        return null;
    }
    if (is_string($v)) {
        $t = trim($v);
        if ($t === '' || strcasecmp($t, 'null') === 0) {
            return null;
        }
        if (is_numeric($t)) {
            return (int) $t;
        }
        return null;
    }
    if (is_int($v)) {
        return $v;
    }
    if (is_float($v)) {
        return (int) $v;
    }
    return null;
}

/**
 * @param mixed $v
 */
function pmc_opt_float($v): ?float
{
    if ($v === null) {
        return null;
    }
    if (is_string($v)) {
        $t = trim($v);
        if ($t === '' || strcasecmp($t, 'null') === 0) {
            return null;
        }
    }
    if (is_numeric($v)) {
        return (float) $v;
    }
    return null;
}

/**
 * @param mixed $v
 */
function pmc_opt_string($v): ?string
{
    if ($v === null) {
        return null;
    }
    if (!is_string($v)) {
        $v = (string) $v;
    }
    $t = trim($v);
    if ($t === '' || strcasecmp($t, 'null') === 0) {
        return null;
    }
    return $v;
}

/**
 * @param array<string,mixed> $row
 * @return array<string,mixed>
 */
function pmc_normalize_row(array $row): array
{
    $upload = $row['upload_time'] ?? null;
    if (is_string($upload) && ($upload === '-' || trim($upload) === '-')) {
        $upload = null;
    } elseif ($upload !== null && !is_string($upload)) {
        $upload = (string) $upload;
    }

    return [
        'aadvid' => pmc_opt_string($row['aadvid'] ?? null) ?? '',
        'material_id' => pmc_opt_string($row['material_id'] ?? null) ?? '',
        'video_name' => pmc_opt_string($row['video_name'] ?? null),
        'material_status' => pmc_opt_int($row['material_status'] ?? null),
        'show_status' => pmc_opt_int($row['show_status'] ?? null),
        'show_status_reason' => pmc_opt_string($row['show_status_reason'] ?? null),
        'upload_time' => $upload !== null ? (is_string($upload) ? $upload : null) : null,
        'video_type' => pmc_opt_int($row['video_type'] ?? null),
        'video_id' => pmc_opt_string($row['video_id'] ?? null),
        'aweme_item_id' => pmc_opt_int($row['aweme_item_id'] ?? null),
        'cover_url' => pmc_opt_string($row['cover_url'] ?? null),
        'cover_width' => pmc_opt_int($row['cover_width'] ?? null),
        'cover_height' => pmc_opt_int($row['cover_height'] ?? null),
        'video_duration' => pmc_opt_int($row['video_duration'] ?? null),
        'video_title' => pmc_opt_string($row['video_title'] ?? null),
        'lego_source' => pmc_opt_int($row['lego_source'] ?? null),
        'video_create_time' => pmc_opt_string($row['video_create_time'] ?? null),
        'tag_list' => isset($row['tag_list']) && $row['tag_list'] !== null ? (string) $row['tag_list'] : null,
        'stat_cost' => pmc_opt_float($row['stat_cost'] ?? null),
        'order_settle_count_1h' => pmc_opt_int($row['order_settle_count_1h'] ?? null),
        'order_settle_amount_1h' => pmc_opt_float($row['order_settle_amount_1h'] ?? null),
        'order_settle_rate_1h' => pmc_opt_float($row['order_settle_rate_1h'] ?? null),
        'prepay_pay_order_count' => pmc_opt_float($row['prepay_pay_order_count'] ?? null),
        'pay_gmv_include_coupon' => pmc_opt_float($row['pay_gmv_include_coupon'] ?? null),
        'prepay_pay_settle_1h' => pmc_opt_float($row['prepay_pay_settle_1h'] ?? null),
        'refund_rate_1h' => pmc_opt_float($row['refund_rate_1h'] ?? null),
        'overall_order_count' => pmc_opt_int($row['overall_order_count'] ?? $row['over_order_count'] ?? null),
        'overall_show_count' => pmc_opt_int($row['overall_show_count'] ?? null),
        'overall_click_count' => pmc_opt_int($row['overall_click_count'] ?? null),
        'overall_ctr' => pmc_opt_float($row['overall_ctr'] ?? null),
        'overall_conversion_rate' => pmc_opt_float($row['overall_conversion_rate'] ?? null),
        'stat_date' => pmc_opt_string($row['stat_date'] ?? null) ?? '',
        'created_at' => array_key_exists('created_at', $row)
            ? dt_from_input(is_string($row['created_at']) ? $row['created_at'] : null)
            : null,
        'updated_at' => array_key_exists('updated_at', $row)
            ? dt_from_input(is_string($row['updated_at']) ? $row['updated_at'] : null)
            : null,
    ];
}

if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
    http_response_code(405);
    echo json_encode(['success' => false, 'message' => '请使用 POST'], JSON_UNESCAPED_UNICODE);
    exit;
}

$raw = file_get_contents('php://input');
$input = [];
if ($raw !== '' && $raw !== false) {
    $decoded = json_decode($raw, true);
    if (!is_array($decoded) || json_last_error() !== JSON_ERROR_NONE) {
        http_response_code(400);
        echo json_encode(['success' => false, 'message' => '请求 JSON 无效或已被服务器截断'], JSON_UNESCAPED_UNICODE);
        exit;
    }
    $input = $decoded;
}

$username = trim((string) ($input['username'] ?? $_POST['username'] ?? ''));
$password = (string) ($input['password'] ?? $_POST['password'] ?? '');
$rows = $input['rows'] ?? null;
if (!is_array($rows)) {
    $rows = [];
}

if ($username === '' || $password === '') {
    echo json_encode(['success' => false, 'message' => '请提供账号和密码'], JSON_UNESCAPED_UNICODE);
    exit;
}

$st = $pdo->prepare(
    'SELECT id, password_hash, role, parent_id, valid_from, valid_until, is_disabled FROM accounts WHERE username = ? LIMIT 1'
);
$st->execute([$username]);
$acc = $st->fetch();

if (!$acc || $acc['role'] !== 'user') {
    echo json_encode(['success' => false, 'message' => '账号或密码错误'], JSON_UNESCAPED_UNICODE);
    exit;
}

if (!password_verify($password, $acc['password_hash'])) {
    echo json_encode(['success' => false, 'message' => '账号或密码错误'], JSON_UNESCAPED_UNICODE);
    exit;
}

if ((int) $acc['is_disabled'] === 1) {
    echo json_encode(['success' => false, 'message' => '账号已禁用'], JSON_UNESCAPED_UNICODE);
    exit;
}

if (!empty($acc['parent_id'])) {
    $ag = $pdo->prepare('SELECT is_disabled FROM accounts WHERE id = ? AND role = ?');
    $ag->execute([(int) $acc['parent_id'], 'agent']);
    $agent = $ag->fetch();
    if ($agent && (int) $agent['is_disabled'] === 1) {
        echo json_encode(['success' => false, 'message' => '代理已禁用，无法备份'], JSON_UNESCAPED_UNICODE);
        exit;
    }
}

$userId = (int) $acc['id'];

try {
    ensure_pmc_promotion_material_schema($pdo);
} catch (Throwable $e) {
    http_response_code(500);
    echo json_encode(['success' => false, 'message' => '数据表初始化失败'], JSON_UNESCAPED_UNICODE);
    exit;
}

if (count($rows) === 0) {
    echo json_encode([
        'success' => true,
        'data' => ['inserted' => 0, 'user_id' => $userId],
    ], JSON_UNESCAPED_UNICODE);
    exit;
}

if (count($rows) > PMC_BACKUP_MAX_ROWS) {
    echo json_encode([
        'success' => false,
        'message' => '单次最多提交 ' . PMC_BACKUP_MAX_ROWS . ' 条，请分批上传',
    ], JSON_UNESCAPED_UNICODE);
    exit;
}

$sqlDefaultTs = <<<'SQL'
INSERT INTO `pmc_promotion_material` (
  `user_id`, `aadvid`, `material_id`, `video_name`, `material_status`, `show_status`, `show_status_reason`,
  `upload_time`, `video_type`, `video_id`, `aweme_item_id`, `cover_url`, `cover_width`, `cover_height`,
  `video_duration`, `video_title`, `lego_source`, `video_create_time`, `tag_list`,
  `stat_cost`, `order_settle_count_1h`, `order_settle_amount_1h`, `order_settle_rate_1h`,
  `prepay_pay_order_count`, `pay_gmv_include_coupon`, `prepay_pay_settle_1h`, `refund_rate_1h`,
  `overall_order_count`,
  `overall_show_count`, `overall_click_count`, `overall_ctr`, `overall_conversion_rate`,
  `stat_date`
) VALUES (
  :user_id, :aadvid, :material_id, :video_name, :material_status, :show_status, :show_status_reason,
  :upload_time, :video_type, :video_id, :aweme_item_id, :cover_url, :cover_width, :cover_height,
  :video_duration, :video_title, :lego_source, :video_create_time, :tag_list,
  :stat_cost, :order_settle_count_1h, :order_settle_amount_1h, :order_settle_rate_1h,
  :prepay_pay_order_count, :pay_gmv_include_coupon, :prepay_pay_settle_1h, :refund_rate_1h,
  :overall_order_count,
  :overall_show_count, :overall_click_count, :overall_ctr, :overall_conversion_rate,
  :stat_date
)
SQL;

$sqlWithTs = <<<'SQL'
INSERT INTO `pmc_promotion_material` (
  `user_id`, `aadvid`, `material_id`, `video_name`, `material_status`, `show_status`, `show_status_reason`,
  `upload_time`, `video_type`, `video_id`, `aweme_item_id`, `cover_url`, `cover_width`, `cover_height`,
  `video_duration`, `video_title`, `lego_source`, `video_create_time`, `tag_list`,
  `stat_cost`, `order_settle_count_1h`, `order_settle_amount_1h`, `order_settle_rate_1h`,
  `prepay_pay_order_count`, `pay_gmv_include_coupon`, `prepay_pay_settle_1h`, `refund_rate_1h`,
  `overall_order_count`,
  `overall_show_count`, `overall_click_count`, `overall_ctr`, `overall_conversion_rate`,
  `stat_date`, `created_at`, `updated_at`
) VALUES (
  :user_id, :aadvid, :material_id, :video_name, :material_status, :show_status, :show_status_reason,
  :upload_time, :video_type, :video_id, :aweme_item_id, :cover_url, :cover_width, :cover_height,
  :video_duration, :video_title, :lego_source, :video_create_time, :tag_list,
  :stat_cost, :order_settle_count_1h, :order_settle_amount_1h, :order_settle_rate_1h,
  :prepay_pay_order_count, :pay_gmv_include_coupon, :prepay_pay_settle_1h, :refund_rate_1h,
  :overall_order_count,
  :overall_show_count, :overall_click_count, :overall_ctr, :overall_conversion_rate,
  :stat_date, :created_at, :updated_at
)
SQL;

$upsertAssignments = [
    'video_name', 'material_status', 'show_status', 'show_status_reason',
    'upload_time', 'video_type', 'video_id', 'aweme_item_id', 'cover_url',
    'cover_width', 'cover_height', 'video_duration', 'video_title',
    'lego_source', 'video_create_time', 'tag_list', 'stat_cost',
    'order_settle_count_1h', 'order_settle_amount_1h', 'order_settle_rate_1h',
    'prepay_pay_order_count', 'pay_gmv_include_coupon',
    'prepay_pay_settle_1h', 'refund_rate_1h', 'overall_order_count',
    'overall_show_count', 'overall_click_count', 'overall_ctr',
    'overall_conversion_rate', 'updated_at',
];
$upsertSql = implode(', ', array_map(
    static fn(string $column): string => "`$column`=VALUES(`$column`)",
    $upsertAssignments
));
$sqlDefaultTs .= "\nON DUPLICATE KEY UPDATE " . $upsertSql;
$sqlWithTs .= "\nON DUPLICATE KEY UPDATE " . $upsertSql;

/**
 * @param \PDOStatement $ins
 * @param array<string,mixed> $n
 */
function pmc_bind_row_params(\PDOStatement $ins, int $userId, array $n): void
{
    $ins->bindValue(':user_id', $userId, PDO::PARAM_INT);
    $ins->bindValue(':aadvid', $n['aadvid']);
    $ins->bindValue(':material_id', $n['material_id']);
    $ins->bindValue(':video_name', $n['video_name'], $n['video_name'] === null ? PDO::PARAM_NULL : PDO::PARAM_STR);
    $ins->bindValue(':material_status', $n['material_status'], $n['material_status'] === null ? PDO::PARAM_NULL : PDO::PARAM_INT);
    $ins->bindValue(':show_status', $n['show_status'], $n['show_status'] === null ? PDO::PARAM_NULL : PDO::PARAM_INT);
    $ins->bindValue(':show_status_reason', $n['show_status_reason'], $n['show_status_reason'] === null ? PDO::PARAM_NULL : PDO::PARAM_STR);
    $ins->bindValue(':upload_time', $n['upload_time'], $n['upload_time'] === null ? PDO::PARAM_NULL : PDO::PARAM_STR);
    $ins->bindValue(':video_type', $n['video_type'], $n['video_type'] === null ? PDO::PARAM_NULL : PDO::PARAM_INT);
    $ins->bindValue(':video_id', $n['video_id'], $n['video_id'] === null ? PDO::PARAM_NULL : PDO::PARAM_STR);
    $ins->bindValue(':aweme_item_id', $n['aweme_item_id'], $n['aweme_item_id'] === null ? PDO::PARAM_NULL : PDO::PARAM_INT);
    $ins->bindValue(':cover_url', $n['cover_url'], $n['cover_url'] === null ? PDO::PARAM_NULL : PDO::PARAM_STR);
    $ins->bindValue(':cover_width', $n['cover_width'], $n['cover_width'] === null ? PDO::PARAM_NULL : PDO::PARAM_INT);
    $ins->bindValue(':cover_height', $n['cover_height'], $n['cover_height'] === null ? PDO::PARAM_NULL : PDO::PARAM_INT);
    $ins->bindValue(':video_duration', $n['video_duration'], $n['video_duration'] === null ? PDO::PARAM_NULL : PDO::PARAM_INT);
    $ins->bindValue(':video_title', $n['video_title'], $n['video_title'] === null ? PDO::PARAM_NULL : PDO::PARAM_STR);
    $ins->bindValue(':lego_source', $n['lego_source'], $n['lego_source'] === null ? PDO::PARAM_NULL : PDO::PARAM_INT);
    $ins->bindValue(':video_create_time', $n['video_create_time'], $n['video_create_time'] === null ? PDO::PARAM_NULL : PDO::PARAM_STR);
    $ins->bindValue(':tag_list', $n['tag_list'], $n['tag_list'] === null ? PDO::PARAM_NULL : PDO::PARAM_STR);
    $ins->bindValue(':stat_cost', $n['stat_cost'], $n['stat_cost'] === null ? PDO::PARAM_NULL : PDO::PARAM_STR);
    $ins->bindValue(':order_settle_count_1h', $n['order_settle_count_1h'], $n['order_settle_count_1h'] === null ? PDO::PARAM_NULL : PDO::PARAM_INT);
    $ins->bindValue(':order_settle_amount_1h', $n['order_settle_amount_1h'], $n['order_settle_amount_1h'] === null ? PDO::PARAM_NULL : PDO::PARAM_STR);
    $ins->bindValue(':order_settle_rate_1h', $n['order_settle_rate_1h'], $n['order_settle_rate_1h'] === null ? PDO::PARAM_NULL : PDO::PARAM_STR);
    $ins->bindValue(':prepay_pay_order_count', $n['prepay_pay_order_count'], $n['prepay_pay_order_count'] === null ? PDO::PARAM_NULL : PDO::PARAM_STR);
    $ins->bindValue(':pay_gmv_include_coupon', $n['pay_gmv_include_coupon'], $n['pay_gmv_include_coupon'] === null ? PDO::PARAM_NULL : PDO::PARAM_STR);
    $ins->bindValue(':prepay_pay_settle_1h', $n['prepay_pay_settle_1h'], $n['prepay_pay_settle_1h'] === null ? PDO::PARAM_NULL : PDO::PARAM_STR);
    $ins->bindValue(':refund_rate_1h', $n['refund_rate_1h'], $n['refund_rate_1h'] === null ? PDO::PARAM_NULL : PDO::PARAM_STR);
    $ins->bindValue(':overall_order_count', $n['overall_order_count'], $n['overall_order_count'] === null ? PDO::PARAM_NULL : PDO::PARAM_INT);
    $ins->bindValue(':overall_show_count', $n['overall_show_count'], $n['overall_show_count'] === null ? PDO::PARAM_NULL : PDO::PARAM_STR);
    $ins->bindValue(':overall_click_count', $n['overall_click_count'], $n['overall_click_count'] === null ? PDO::PARAM_NULL : PDO::PARAM_STR);
    $ins->bindValue(':overall_ctr', $n['overall_ctr'], $n['overall_ctr'] === null ? PDO::PARAM_NULL : PDO::PARAM_STR);
    $ins->bindValue(':overall_conversion_rate', $n['overall_conversion_rate'], $n['overall_conversion_rate'] === null ? PDO::PARAM_NULL : PDO::PARAM_STR);
    $ins->bindValue(':stat_date', $n['stat_date']);
}

$inserted = 0;
$rejected = [];
try {
    $pdo->beginTransaction();
    $insDefault = $pdo->prepare($sqlDefaultTs);
    $insWithTs = $pdo->prepare($sqlWithTs);
    foreach ($rows as $idx => $item) {
        try {
            if (!is_array($item)) {
                throw new InvalidArgumentException('rows[' . $idx . '] 不是对象');
            }
            $n = pmc_normalize_row($item);
            if ($n['aadvid'] === '' || $n['material_id'] === '' || $n['stat_date'] === '') {
                throw new InvalidArgumentException('rows[' . $idx . '] 缺少 aadvid、material_id 或 stat_date');
            }
            if (!preg_match('/^\d{4}-\d{2}-\d{2}$/', $n['stat_date'])) {
                throw new InvalidArgumentException('rows[' . $idx . '] stat_date 须为 YYYY-MM-DD');
            }
        } catch (InvalidArgumentException $e) {
            $rejected[] = ['index' => $idx, 'message' => $e->getMessage()];
            continue;
        }

        $wantTs = $n['created_at'] !== null || $n['updated_at'] !== null;
        if ($wantTs) {
            $ca = $n['created_at'] ?? date('Y-m-d H:i:s');
            $ua = $n['updated_at'] ?? $ca;
            pmc_bind_row_params($insWithTs, $userId, $n);
            $insWithTs->bindValue(':created_at', $ca);
            $insWithTs->bindValue(':updated_at', $ua);
            $insWithTs->execute();
        } else {
            pmc_bind_row_params($insDefault, $userId, $n);
            $insDefault->execute();
        }
        $inserted++;
    }
    $pdo->commit();
} catch (InvalidArgumentException $e) {
    if ($pdo->inTransaction()) {
        $pdo->rollBack();
    }
    echo json_encode(['success' => false, 'message' => $e->getMessage()], JSON_UNESCAPED_UNICODE);
    exit;
} catch (Throwable $e) {
    if ($pdo->inTransaction()) {
        $pdo->rollBack();
    }
    http_response_code(500);
    echo json_encode(['success' => false, 'message' => '写入失败'], JSON_UNESCAPED_UNICODE);
    exit;
}

echo json_encode([
    'success' => true,
    'data' => [
        'inserted' => $inserted,
        'rejected' => count($rejected),
        'rejected_rows' => array_slice($rejected, 0, 50),
        'user_id' => $userId,
    ],
], JSON_UNESCAPED_UNICODE);
