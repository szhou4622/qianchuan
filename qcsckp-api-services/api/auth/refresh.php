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
$sessionRow = desktop_auth_session($pdo, $token, $deviceHash, 'access');

$pdo->beginTransaction();
try {
    $now = desktop_auth_now()->format('Y-m-d H:i:s');
    $pdo->prepare('UPDATE desktop_auth_sessions SET revoked_at=? WHERE token_hash=?')
        ->execute([$now, hash('sha256', $token)]);
    $profile = desktop_auth_profile($pdo, (int) $sessionRow['account_id']);
    $account = desktop_auth_account($pdo, (string) $sessionRow['username']);
    if (!$account) {
        throw new RuntimeException('account_missing');
    }
    desktop_auth_bind_device(
        $pdo,
        (int) $sessionRow['account_id'],
        $deviceHash,
        substr(trim((string) ($input['client_version'] ?? '')), 0, 64)
    );
    $newSession = desktop_auth_issue_session($pdo, $account, $profile, $deviceHash, 'access');
    $pdo->commit();
    desktop_auth_reply_ok(desktop_auth_payload($account, $profile, $newSession));
} catch (Throwable $error) {
    if ($pdo->inTransaction()) {
        $pdo->rollBack();
    }
    desktop_auth_reply_error('refresh_failed', '登录状态刷新失败', 500, true);
}

