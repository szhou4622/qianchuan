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
$username = trim((string) ($input['username'] ?? ''));
$password = (string) ($input['password'] ?? '');
$deviceHash = desktop_auth_device_hash(trim((string) ($input['device_id'] ?? '')));
$clientVersion = substr(trim((string) ($input['client_version'] ?? '')), 0, 64);
$account = desktop_auth_account($pdo, $username);
if (!$account || !password_verify($password, (string) $account['password_hash'])) {
    desktop_auth_reply_error('invalid_credentials', '账号或密码错误', 401);
}
desktop_auth_assert_account($pdo, $account);

$pdo->beginTransaction();
try {
    $profile = desktop_auth_profile($pdo, (int) $account['id'], false);
    desktop_auth_bind_device($pdo, (int) $account['id'], $deviceHash, $clientVersion);
    if ((int) $profile['must_change_password'] === 1) {
        $session = desktop_auth_issue_session($pdo, $account, $profile, $deviceHash, 'password_change');
        $pdo->commit();
        desktop_auth_reply_ok([
            'username' => $account['username'],
            'must_change_password' => true,
            'change_token' => $session['token'],
        ]);
    }
    $session = desktop_auth_issue_session($pdo, $account, $profile, $deviceHash, 'access');
    $pdo->commit();
    desktop_auth_reply_ok(desktop_auth_payload($account, $profile, $session));
} catch (Throwable $error) {
    if ($pdo->inTransaction()) {
        $pdo->rollBack();
    }
    if ($error instanceof PDOException) {
        desktop_auth_reply_error('auth_storage_error', '认证服务暂时不可用', 503, true);
    }
    throw $error;
}

