<?php
declare(strict_types=1);

header('Content-Type: application/json; charset=utf-8');

require_once dirname(__DIR__) . '/includes/bootstrap.php';
require_once dirname(__DIR__) . '/includes/portal_api_login_log.php';

if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
    http_response_code(405);
    echo json_encode(['success' => false, 'message' => '请使用 POST'], JSON_UNESCAPED_UNICODE);
    exit;
}

$raw = file_get_contents('php://input');
$input = [];
if ($raw !== '' && $raw !== false) {
    $input = json_decode($raw, true);
    if (!is_array($input)) {
        $input = [];
    }
}
$username = trim((string) ($input['username'] ?? $_POST['username'] ?? ''));
$password = (string) ($input['password'] ?? $_POST['password'] ?? '');

if ($username === '' || $password === '') {
    echo json_encode(['success' => false, 'message' => '请提供账号和密码'], JSON_UNESCAPED_UNICODE);
    exit;
}

$st = $pdo->prepare(
    'SELECT id, password_hash, role, parent_id, valid_from, valid_until, is_disabled FROM accounts WHERE username = ? LIMIT 1'
);
$st->execute([$username]);
$row = $st->fetch();

if (!$row) {
    echo json_encode(['success' => false, 'message' => '账号或密码错误'], JSON_UNESCAPED_UNICODE);
    exit;
}

if ($row['role'] !== 'user') {
    echo json_encode(['success' => false, 'message' => '账号或密码错误'], JSON_UNESCAPED_UNICODE);
    exit;
}

if (!password_verify($password, $row['password_hash'])) {
    echo json_encode(['success' => false, 'message' => '账号或密码错误'], JSON_UNESCAPED_UNICODE);
    exit;
}

$parentId = !empty($row['parent_id']) ? (int) $row['parent_id'] : null;
$vf = $row['valid_from'] ?? null;
$vu = $row['valid_until'] ?? null;
$dis = (int) $row['is_disabled'];

// 检查代理是否被禁用（用户不可用）
if ($parentId !== null) {
    $ag = $pdo->prepare('SELECT is_disabled FROM accounts WHERE id = ? AND role = ?');
    $ag->execute([$parentId, 'agent']);
    $agent = $ag->fetch();
    if ($agent && (int) $agent['is_disabled'] === 1) {
        portal_api_login_log_write($pdo, [
            'account_id' => (int) $row['id'],
            'username' => $username,
            'result_code' => 'agent_disabled',
            'login_success' => true,
            'parent_id' => $parentId,
            'valid_from_snapshot' => $vf !== null ? (string) $vf : null,
            'valid_until_snapshot' => $vu !== null ? (string) $vu : null,
            'account_disabled_snapshot' => $dis,
        ]);
        echo json_encode([
            'success' => true,
            'data' => [
                'valid_from' => $row['valid_from'],
                'valid_until' => $row['valid_until'],
                'is_disabled' => 1,
            ],
        ], JSON_UNESCAPED_UNICODE);
        exit;
    }
}

portal_api_login_log_write($pdo, [
    'account_id' => (int) $row['id'],
    'username' => $username,
    'result_code' => 'success',
    'login_success' => true,
    'parent_id' => $parentId,
    'valid_from_snapshot' => $vf !== null ? (string) $vf : null,
    'valid_until_snapshot' => $vu !== null ? (string) $vu : null,
    'account_disabled_snapshot' => $dis,
]);

echo json_encode([
    'success' => true,
    'data' => [
        'valid_from' => $row['valid_from'],
        'valid_until' => $row['valid_until'],
        'is_disabled' => (int) $row['is_disabled'],
    ],
], JSON_UNESCAPED_UNICODE);
