<?php
declare(strict_types=1);

/**
 * 桌面端广告详情基础信息 → 云端 MySQL（表 pmc_ad_detail_basic）
 * POST JSON：username、password、rows（对象数组，字段与表一致；aadvid 唯一，存在则更新）
 * 表须已在数据库中建好（本接口不再自动 CREATE TABLE）。
 */

header('Content-Type: application/json; charset=utf-8');

require_once dirname(__DIR__) . '/includes/bootstrap.php';
require_once dirname(__DIR__) . '/includes/helpers.php';

const PAD_BACKUP_MAX_ROWS = 500;

function pad_table_exists(PDO $pdo): bool
{
    $check = $pdo->query("SHOW TABLES LIKE 'pmc_ad_detail_basic'");

    return $check !== false && $check->fetch() !== false;
}

/**
 * @param mixed $v
 */
function pad_opt_int($v): ?int
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
function pad_opt_float($v): ?float
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
function pad_opt_string($v): ?string
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

    return $t;
}

/**
 * @param array<string,mixed> $row
 * @return array<string,mixed>
 */
function pad_normalize_row(array $row): array
{
    return [
        'aadvid' => pad_opt_string($row['aadvid'] ?? null) ?? '',
        'ad_id' => pad_opt_string($row['ad_id'] ?? null) ?? '',
        'budget' => pad_opt_string($row['budget'] ?? null),
        'audience_coverage_count' => pad_opt_string($row['audience_coverage_count'] ?? null),
        'compensation_convert' => pad_opt_string($row['compensation_convert'] ?? null),
        'ecp_roi2_goal' => pad_opt_float($row['ecp_roi2_goal'] ?? null),
        'creative_type' => pad_opt_int($row['creative_type'] ?? null),
        'user_info_id' => pad_opt_string($row['user_info_id'] ?? null),
        'user_info_name' => pad_opt_string($row['user_info_name'] ?? null),
        'user_info_unique_id' => pad_opt_string($row['user_info_unique_id'] ?? null),
        'created_at' => array_key_exists('created_at', $row)
            ? dt_from_input(is_string($row['created_at']) ? $row['created_at'] : null)
            : null,
        'updated_at' => array_key_exists('updated_at', $row)
            ? dt_from_input(is_string($row['updated_at']) ? $row['updated_at'] : null)
            : null,
    ];
}

/**
 * 若存在 pmc_promotion_material，则要求该用户已同步过该 aadvid，避免越权写入他人广告主。
 */
function pad_user_may_write_aadvid(PDO $pdo, int $userId, string $aadvid): bool
{
    $check = $pdo->query("SHOW TABLES LIKE 'pmc_promotion_material'");
    if ($check === false || $check->fetch() === false) {
        return true;
    }
    $st = $pdo->prepare('SELECT 1 FROM pmc_promotion_material WHERE user_id = ? AND aadvid = ? LIMIT 1');
    $st->execute([$userId, $aadvid]);

    return (bool) $st->fetchColumn();
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
    $input = is_array($decoded) ? $decoded : [];
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
        echo json_encode(['success' => false, 'message' => '代理已禁用，无法同步'], JSON_UNESCAPED_UNICODE);
        exit;
    }
}

$userId = (int) $acc['id'];

if (!pad_table_exists($pdo)) {
    http_response_code(500);
    echo json_encode(['success' => false, 'message' => '数据库中不存在表 pmc_ad_detail_basic，请先在库中创建该表'], JSON_UNESCAPED_UNICODE);
    exit;
}

if (count($rows) === 0) {
    echo json_encode([
        'success' => true,
        'data' => ['upserted' => 0, 'user_id' => $userId],
    ], JSON_UNESCAPED_UNICODE);
    exit;
}

if (count($rows) > PAD_BACKUP_MAX_ROWS) {
    echo json_encode([
        'success' => false,
        'message' => '单次最多提交 ' . PAD_BACKUP_MAX_ROWS . ' 条，请分批上传',
    ], JSON_UNESCAPED_UNICODE);
    exit;
}

$sqlUpsert = <<<'SQL'
INSERT INTO `pmc_ad_detail_basic` (
  `aadvid`, `ad_id`, `budget`, `audience_coverage_count`, `compensation_convert`,
  `ecp_roi2_goal`, `creative_type`, `user_info_id`, `user_info_name`, `user_info_unique_id`,
  `created_at`, `updated_at`
) VALUES (
  :aadvid, :ad_id, :budget, :audience_coverage_count, :compensation_convert,
  :ecp_roi2_goal, :creative_type, :user_info_id, :user_info_name, :user_info_unique_id,
  :created_at, :updated_at
)
ON DUPLICATE KEY UPDATE
  `ad_id` = VALUES(`ad_id`),
  `budget` = VALUES(`budget`),
  `audience_coverage_count` = VALUES(`audience_coverage_count`),
  `compensation_convert` = VALUES(`compensation_convert`),
  `ecp_roi2_goal` = VALUES(`ecp_roi2_goal`),
  `creative_type` = VALUES(`creative_type`),
  `user_info_id` = VALUES(`user_info_id`),
  `user_info_name` = VALUES(`user_info_name`),
  `user_info_unique_id` = VALUES(`user_info_unique_id`),
  `updated_at` = VALUES(`updated_at`)
SQL;

$sqlUpsertDefaultTs = <<<'SQL'
INSERT INTO `pmc_ad_detail_basic` (
  `aadvid`, `ad_id`, `budget`, `audience_coverage_count`, `compensation_convert`,
  `ecp_roi2_goal`, `creative_type`, `user_info_id`, `user_info_name`, `user_info_unique_id`
) VALUES (
  :aadvid, :ad_id, :budget, :audience_coverage_count, :compensation_convert,
  :ecp_roi2_goal, :creative_type, :user_info_id, :user_info_name, :user_info_unique_id
)
ON DUPLICATE KEY UPDATE
  `ad_id` = VALUES(`ad_id`),
  `budget` = VALUES(`budget`),
  `audience_coverage_count` = VALUES(`audience_coverage_count`),
  `compensation_convert` = VALUES(`compensation_convert`),
  `ecp_roi2_goal` = VALUES(`ecp_roi2_goal`),
  `creative_type` = VALUES(`creative_type`),
  `user_info_id` = VALUES(`user_info_id`),
  `user_info_name` = VALUES(`user_info_name`),
  `user_info_unique_id` = VALUES(`user_info_unique_id`),
  `updated_at` = CURRENT_TIMESTAMP
SQL;

/**
 * @param array<string,mixed> $n
 */
function pad_bind_params(PDOStatement $st, array $n, bool $withTimestamps): void
{
    $st->bindValue(':aadvid', $n['aadvid']);
    $st->bindValue(':ad_id', $n['ad_id']);
    $st->bindValue(':budget', $n['budget'], $n['budget'] === null ? PDO::PARAM_NULL : PDO::PARAM_STR);
    $st->bindValue(':audience_coverage_count', $n['audience_coverage_count'], $n['audience_coverage_count'] === null ? PDO::PARAM_NULL : PDO::PARAM_STR);
    $st->bindValue(':compensation_convert', $n['compensation_convert'], $n['compensation_convert'] === null ? PDO::PARAM_NULL : PDO::PARAM_STR);
    $st->bindValue(':ecp_roi2_goal', $n['ecp_roi2_goal'], $n['ecp_roi2_goal'] === null ? PDO::PARAM_NULL : PDO::PARAM_STR);
    $st->bindValue(':creative_type', $n['creative_type'], $n['creative_type'] === null ? PDO::PARAM_NULL : PDO::PARAM_INT);
    $st->bindValue(':user_info_id', $n['user_info_id'], $n['user_info_id'] === null ? PDO::PARAM_NULL : PDO::PARAM_STR);
    $st->bindValue(':user_info_name', $n['user_info_name'], $n['user_info_name'] === null ? PDO::PARAM_NULL : PDO::PARAM_STR);
    $st->bindValue(':user_info_unique_id', $n['user_info_unique_id'], $n['user_info_unique_id'] === null ? PDO::PARAM_NULL : PDO::PARAM_STR);
    if ($withTimestamps) {
        $ca = $n['created_at'] ?? date('Y-m-d H:i:s');
        $ua = $n['updated_at'] ?? $ca;
        $st->bindValue(':created_at', $ca);
        $st->bindValue(':updated_at', $ua);
    }
}

$upserted = 0;
try {
    $pdo->beginTransaction();
    $prepDefault = $pdo->prepare($sqlUpsertDefaultTs);
    $prepTs = $pdo->prepare($sqlUpsert);
    foreach ($rows as $idx => $item) {
        if (!is_array($item)) {
            throw new InvalidArgumentException('rows[' . $idx . '] 不是对象');
        }
        $n = pad_normalize_row($item);
        if ($n['aadvid'] === '' || $n['ad_id'] === '') {
            throw new InvalidArgumentException('rows[' . $idx . '] 缺少 aadvid 或 ad_id');
        }
        if (!pad_user_may_write_aadvid($pdo, $userId, $n['aadvid'])) {
            throw new InvalidArgumentException(
                'rows[' . $idx . '] 广告主 ' . $n['aadvid'] . ' 与当前账号素材备份不一致，请先同步该广告主的素材数据'
            );
        }

        $wantTs = $n['created_at'] !== null || $n['updated_at'] !== null;
        if ($wantTs) {
            pad_bind_params($prepTs, $n, true);
            $prepTs->execute();
        } else {
            pad_bind_params($prepDefault, $n, false);
            $prepDefault->execute();
        }
        $upserted++;
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
        'upserted' => $upserted,
        'user_id' => $userId,
    ],
], JSON_UNESCAPED_UNICODE);
