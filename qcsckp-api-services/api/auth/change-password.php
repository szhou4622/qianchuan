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
$newPassword = (string) ($input['new_password'] ?? '');
if (strlen($newPassword) < 10
    || !preg_match('/[a-z]/', $newPassword)
    || !preg_match('/[A-Z]/', $newPassword)
    || !preg_match('/\d/', $newPassword)) {
    desktop_auth_reply_error('weak_password', '新密码至少10位，并包含大小写字母和数字', 400);
}
$deviceHash = desktop_auth_device_hash(trim((string) ($input['device_id'] ?? '')));
$token = desktop_auth_bearer();
$sessionRow = desktop_auth_session($pdo, $token, $deviceHash, 'password_change');

$pdo->beginTransaction();
try {
    $now = desktop_auth_now()->format('Y-m-d H:i:s');
    $pdo->prepare('UPDATE accounts SET password_hash=? WHERE id=?')
        ->execute([password_hash($newPassword, PASSWORD_DEFAULT), (int) $sessionRow['account_id']]);
    $pdo->prepare(
        'UPDATE desktop_auth_users
         SET must_change_password=0, token_version=token_version+1, updated_at=?
         WHERE account_id=?'
    )->execute([$now, (int) $sessionRow['account_id']]);
    $pdo->prepare('UPDATE desktop_auth_sessions SET revoked_at=? WHERE account_id=? AND revoked_at IS NULL')
        ->execute([$now, (int) $sessionRow['account_id']]);
    $profile = desktop_auth_profile($pdo, (int) $sessionRow['account_id']);
    $account = desktop_auth_account($pdo, (string) $sessionRow['username']);
    if (!$account) {
        throw new RuntimeException('account_missing');
    }
    $newSession = desktop_auth_issue_session($pdo, $account, $profile, $deviceHash, 'access');
    $pdo->commit();
    desktop_auth_reply_ok(desktop_auth_payload($account, $profile, $newSession));
} catch (Throwable $error) {
    if ($pdo->inTransaction()) {
        $pdo->rollBack();
    }
    desktop_auth_reply_error('password_change_failed', '密码修改失败，请重试', 500, true);
}

