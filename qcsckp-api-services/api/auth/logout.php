<?php
declare(strict_types=1);

header('Content-Type: application/json; charset=utf-8');
require_once dirname(__DIR__, 2) . '/includes/bootstrap.php';
require_once dirname(__DIR__, 2) . '/includes/desktop_auth.php';

if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
    desktop_auth_reply_error('method_not_allowed', '请使用POST', 405);
}
desktop_auth_ensure_schema($pdo);
$input = desktop_auth_json_input();
$deviceHash = desktop_auth_device_hash(trim((string) ($input['device_id'] ?? '')));
$token = desktop_auth_bearer();
desktop_auth_session($pdo, $token, $deviceHash, 'access');
$pdo->prepare('UPDATE desktop_auth_sessions SET revoked_at=? WHERE token_hash=?')
    ->execute([desktop_auth_now()->format('Y-m-d H:i:s'), hash('sha256', $token)]);
desktop_auth_reply_ok(['logged_out' => true]);

