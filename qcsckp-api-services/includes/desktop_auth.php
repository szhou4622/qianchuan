<?php
declare(strict_types=1);

/** 生产版桌面端中心认证。这里只处理工具账号、有效期和随机设备ID。 */

function desktop_auth_ensure_schema(PDO $pdo): void
{
    $pdo->exec(
        "CREATE TABLE IF NOT EXISTS desktop_auth_users (
            account_id BIGINT NOT NULL PRIMARY KEY,
            tool_user_id VARCHAR(64) NOT NULL UNIQUE,
            must_change_password TINYINT(1) NOT NULL DEFAULT 0,
            token_version INT NOT NULL DEFAULT 1,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            CONSTRAINT fk_desktop_auth_user_account
              FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
    );
    $pdo->exec(
        "CREATE TABLE IF NOT EXISTS desktop_auth_devices (
            account_id BIGINT NOT NULL PRIMARY KEY,
            device_id_hash CHAR(64) NOT NULL UNIQUE,
            client_version VARCHAR(64) NULL,
            bound_at DATETIME NOT NULL,
            last_seen_at DATETIME NOT NULL,
            revoked_at DATETIME NULL,
            CONSTRAINT fk_desktop_auth_device_account
              FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
    );
    $pdo->exec(
        "CREATE TABLE IF NOT EXISTS desktop_auth_sessions (
            token_hash CHAR(64) NOT NULL PRIMARY KEY,
            account_id BIGINT NOT NULL,
            device_id_hash CHAR(64) NOT NULL,
            token_version INT NOT NULL,
            scope VARCHAR(32) NOT NULL,
            expires_at DATETIME NOT NULL,
            last_seen_at DATETIME NOT NULL,
            revoked_at DATETIME NULL,
            created_at DATETIME NOT NULL,
            INDEX idx_desktop_auth_session_account (account_id, expires_at),
            CONSTRAINT fk_desktop_auth_session_account
              FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
    );
}

function desktop_auth_now(): DateTimeImmutable
{
    return new DateTimeImmutable('now', new DateTimeZone('UTC'));
}

function desktop_auth_json_input(): array
{
    $raw = file_get_contents('php://input');
    $value = json_decode($raw === false ? '' : $raw, true);
    return is_array($value) ? $value : [];
}

function desktop_auth_reply_ok(array $data, int $status = 200): never
{
    http_response_code($status);
    echo json_encode(
        ['success' => true, 'data' => $data, 'error' => null],
        JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES
    );
    exit;
}

function desktop_auth_reply_error(
    string $code,
    string $message,
    int $status = 400,
    bool $retryable = false
): never {
    http_response_code($status);
    echo json_encode(
        [
            'success' => false,
            'data' => null,
            'error' => [
                'code' => $code,
                'message' => $message,
                'retryable' => $retryable,
            ],
        ],
        JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES
    );
    exit;
}

function desktop_auth_bearer(): string
{
    $header = trim((string) ($_SERVER['HTTP_AUTHORIZATION'] ?? ''));
    if (!preg_match('/^Bearer\s+(.+)$/i', $header, $match)) {
        desktop_auth_reply_error('access_token_required', '登录令牌无效', 401);
    }
    return trim((string) $match[1]);
}

function desktop_auth_device_hash(string $deviceId): string
{
    if (!preg_match('/^device_[a-f0-9]{32,64}$/', $deviceId)) {
        desktop_auth_reply_error('invalid_device_id', '设备标识无效', 400);
    }
    return hash('sha256', $deviceId);
}

function desktop_auth_account(PDO $pdo, string $username): ?array
{
    $st = $pdo->prepare(
        "SELECT id, username, password_hash, role, parent_id, valid_from,
                valid_until, is_disabled
         FROM accounts WHERE username=? LIMIT 1"
    );
    $st->execute([$username]);
    $row = $st->fetch(PDO::FETCH_ASSOC);
    return $row ?: null;
}

function desktop_auth_assert_account(PDO $pdo, array $account): void
{
    if (($account['role'] ?? '') !== 'user') {
        desktop_auth_reply_error('invalid_credentials', '账号或密码错误', 401);
    }
    if ((int) ($account['is_disabled'] ?? 0) === 1) {
        desktop_auth_reply_error('account_disabled', '工具账号已被禁用', 403);
    }
    if (!empty($account['parent_id'])) {
        $st = $pdo->prepare("SELECT is_disabled FROM accounts WHERE id=? AND role='agent'");
        $st->execute([(int) $account['parent_id']]);
        $agent = $st->fetch(PDO::FETCH_ASSOC);
        if ($agent && (int) $agent['is_disabled'] === 1) {
            desktop_auth_reply_error('account_disabled', '工具账号已被禁用', 403);
        }
    }
    $now = new DateTimeImmutable('now', new DateTimeZone('Asia/Shanghai'));
    if (!empty($account['valid_from'])) {
        $from = new DateTimeImmutable((string) $account['valid_from'], new DateTimeZone('Asia/Shanghai'));
        if ($now < $from) {
            desktop_auth_reply_error('account_not_started', '工具账号尚未到生效时间', 403);
        }
    }
    if (!empty($account['valid_until'])) {
        $until = new DateTimeImmutable((string) $account['valid_until'], new DateTimeZone('Asia/Shanghai'));
        if ($now > $until) {
            desktop_auth_reply_error('account_expired', '工具账号已过期', 403);
        }
    }
}

function desktop_auth_profile(PDO $pdo, int $accountId, bool $newAccount = false): array
{
    $st = $pdo->prepare('SELECT * FROM desktop_auth_users WHERE account_id=? LIMIT 1');
    $st->execute([$accountId]);
    $row = $st->fetch(PDO::FETCH_ASSOC);
    if ($row) {
        return $row;
    }
    $now = desktop_auth_now()->format('Y-m-d H:i:s');
    $toolUserId = 'user_' . bin2hex(random_bytes(16));
    $insert = $pdo->prepare(
        'INSERT INTO desktop_auth_users(
            account_id, tool_user_id, must_change_password,
            token_version, created_at, updated_at
         ) VALUES(?, ?, ?, 1, ?, ?)'
    );
    $insert->execute([$accountId, $toolUserId, $newAccount ? 1 : 0, $now, $now]);
    return [
        'account_id' => $accountId,
        'tool_user_id' => $toolUserId,
        'must_change_password' => $newAccount ? 1 : 0,
        'token_version' => 1,
    ];
}

function desktop_auth_bind_device(
    PDO $pdo,
    int $accountId,
    string $deviceHash,
    string $clientVersion
): void {
    $st = $pdo->prepare('SELECT * FROM desktop_auth_devices WHERE account_id=? FOR UPDATE');
    $st->execute([$accountId]);
    $row = $st->fetch(PDO::FETCH_ASSOC);
    $now = desktop_auth_now()->format('Y-m-d H:i:s');
    if (!$row || !empty($row['revoked_at'])) {
        $pdo->prepare(
            'INSERT INTO desktop_auth_devices(
                account_id, device_id_hash, client_version,
                bound_at, last_seen_at, revoked_at
             ) VALUES(?, ?, ?, ?, ?, NULL)
             ON DUPLICATE KEY UPDATE device_id_hash=VALUES(device_id_hash),
                client_version=VALUES(client_version), bound_at=VALUES(bound_at),
                last_seen_at=VALUES(last_seen_at), revoked_at=NULL'
        )->execute([$accountId, $deviceHash, $clientVersion, $now, $now]);
        return;
    }
    if (!hash_equals((string) $row['device_id_hash'], $deviceHash)) {
        desktop_auth_reply_error(
            'device_mismatch',
            '该账号已绑定其他电脑，请联系管理员解绑设备',
            409
        );
    }
    $pdo->prepare(
        'UPDATE desktop_auth_devices SET client_version=?, last_seen_at=? WHERE account_id=?'
    )->execute([$clientVersion, $now, $accountId]);
}

function desktop_auth_issue_session(
    PDO $pdo,
    array $account,
    array $profile,
    string $deviceHash,
    string $scope
): array {
    $now = desktop_auth_now();
    $ttl = $scope === 'password_change' ? new DateInterval('PT15M') : new DateInterval('PT12H');
    $expires = $now->add($ttl);
    $validUntilUtc = null;
    if (!empty($account['valid_until'])) {
        $validUntilUtc = (new DateTimeImmutable(
            (string) $account['valid_until'],
            new DateTimeZone('Asia/Shanghai')
        ))->setTimezone(new DateTimeZone('UTC'));
        if ($validUntilUtc < $expires) {
            $expires = $validUntilUtc;
        }
    }
    $offlineGrace = $now->add(new DateInterval('PT72H'));
    if ($validUntilUtc !== null && $validUntilUtc < $offlineGrace) {
        $offlineGrace = $validUntilUtc;
    }
    $token = rtrim(strtr(base64_encode(random_bytes(32)), '+/', '-_'), '=');
    $hash = hash('sha256', $token);
    $stamp = $now->format('Y-m-d H:i:s');
    $pdo->prepare(
        'INSERT INTO desktop_auth_sessions(
            token_hash, account_id, device_id_hash, token_version,
            scope, expires_at, last_seen_at, created_at
         ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)'
    )->execute([
        $hash,
        (int) $account['id'],
        $deviceHash,
        (int) $profile['token_version'],
        $scope,
        $expires->format('Y-m-d H:i:s'),
        $stamp,
        $stamp,
    ]);
    return [
        'token' => $token,
        'token_expires_at' => $expires->format(DATE_ATOM),
        'offline_grace_until' => $offlineGrace->format(DATE_ATOM),
        'valid_until' => $validUntilUtc ? $validUntilUtc->format(DATE_ATOM) : $offlineGrace->format(DATE_ATOM),
    ];
}

function desktop_auth_session(
    PDO $pdo,
    string $token,
    string $deviceHash,
    string $requiredScope = 'access'
): array {
    $hash = hash('sha256', $token);
    $st = $pdo->prepare(
        "SELECT s.*, a.username, a.password_hash, a.role, a.parent_id,
                a.valid_from, a.valid_until, a.is_disabled,
                u.tool_user_id, u.must_change_password,
                u.token_version AS current_token_version
         FROM desktop_auth_sessions s
         JOIN accounts a ON a.id=s.account_id
         JOIN desktop_auth_users u ON u.account_id=s.account_id
         WHERE s.token_hash=? LIMIT 1"
    );
    $st->execute([$hash]);
    $row = $st->fetch(PDO::FETCH_ASSOC);
    if (!$row || !empty($row['revoked_at'])) {
        desktop_auth_reply_error('invalid_access_token', '登录令牌已失效', 401);
    }
    if (!hash_equals((string) $row['device_id_hash'], $deviceHash)) {
        desktop_auth_reply_error('device_mismatch', '登录令牌与当前设备不匹配', 409);
    }
    if ((int) $row['token_version'] !== (int) $row['current_token_version']) {
        desktop_auth_reply_error('invalid_access_token', '登录令牌已被撤销', 401);
    }
    if ((string) $row['scope'] !== $requiredScope) {
        desktop_auth_reply_error('invalid_token_scope', '登录令牌用途不正确', 403);
    }
    if (new DateTimeImmutable((string) $row['expires_at'], new DateTimeZone('UTC')) <= desktop_auth_now()) {
        desktop_auth_reply_error('access_token_expired', '登录令牌已过期', 401);
    }
    desktop_auth_assert_account($pdo, $row);
    $now = desktop_auth_now()->format('Y-m-d H:i:s');
    $pdo->prepare('UPDATE desktop_auth_sessions SET last_seen_at=? WHERE token_hash=?')
        ->execute([$now, $hash]);
    return $row;
}

function desktop_auth_payload(array $account, array $profile, array $session): array
{
    return [
        'remote_account_id' => (string) $account['id'],
        'tool_user_id' => (string) $profile['tool_user_id'],
        'username' => (string) $account['username'],
        'access_token' => (string) $session['token'],
        'token_expires_at' => (string) $session['token_expires_at'],
        'offline_grace_until' => (string) $session['offline_grace_until'],
        'valid_until' => (string) $session['valid_until'],
        'must_change_password' => false,
    ];
}

