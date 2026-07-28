<?php
declare(strict_types=1);

require_once dirname(__DIR__, 2) . '/includes/bootstrap.php';
require_once dirname(__DIR__, 2) . '/includes/retarget_task_common.php';

ensure_retarget_task_schema($pdo);
if ($_SERVER['REQUEST_METHOD'] === 'DELETE') {
    $user = authenticate_device($pdo);
    $pdo->prepare('UPDATE desktop_device_sessions SET revoked_at=NOW() WHERE id=? AND revoked_at IS NULL')
        ->execute([(int)$user['session_id']]);
    api_json(['success' => true]);
}
if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
    api_json(['success' => false, 'message' => '请使用 POST 或 DELETE'], 405);
}
$input = api_json_input();
$username = trim((string)($input['username'] ?? ''));
$password = (string)($input['password'] ?? '');
$deviceName = mb_substr(trim((string)($input['device_name'] ?? '千川素材看盘桌面端')), 0, 120);
$user = authenticate_password_user($pdo, $username, $password);
if (!$user) {
    api_json(['success' => false, 'message' => '账号或密码错误，或账号当前不可用'], 401);
}
$token = bin2hex(random_bytes(32));
$hash = hash('sha256', $token);
$pdo->prepare('UPDATE desktop_device_sessions SET revoked_at=NOW() WHERE user_id=? AND device_name=? AND revoked_at IS NULL')
    ->execute([(int)$user['id'], $deviceName]);
$st = $pdo->prepare(
    'INSERT INTO desktop_device_sessions(user_id,token_hash,device_name,expires_at,last_seen_at) '
    . 'VALUES(?,?,?,DATE_ADD(NOW(),INTERVAL 90 DAY),NOW())'
);
$st->execute([(int)$user['id'], $hash, $deviceName]);
api_json([
    'success' => true,
    'data' => [
        'token' => $token,
        'expires_in_days' => 90,
        'username' => (string)$user['username'],
        'device_name' => $deviceName,
    ],
]);
