<?php
declare(strict_types=1);

/**
 * 仅在 POST /api/account.php 中「看盘用户 + 密码正确」之后写入一条审计日志（失败请求不落库）。
 */

function portal_api_login_client_ip(): string
{
    if (!empty($_SERVER['HTTP_X_FORWARDED_FOR'])) {
        $parts = preg_split('/\s*,\s*/', (string) $_SERVER['HTTP_X_FORWARDED_FOR']);
        if ($parts !== false && isset($parts[0]) && $parts[0] !== '') {
            return substr($parts[0], 0, 45);
        }
    }
    if (!empty($_SERVER['HTTP_X_REAL_IP'])) {
        return substr(trim((string) $_SERVER['HTTP_X_REAL_IP']), 0, 45);
    }
    $ra = $_SERVER['REMOTE_ADDR'] ?? '';
    return substr((string) $ra, 0, 45);
}

/**
 * @return array{client_ip:string, forwarded_for:?string, http_via:?string}
 */
function portal_api_login_request_meta(): array
{
    $xff = $_SERVER['HTTP_X_FORWARDED_FOR'] ?? null;
    $xff = is_string($xff) && $xff !== '' ? substr($xff, 0, 512) : null;
    $via = $_SERVER['HTTP_VIA'] ?? null;
    $via = is_string($via) && $via !== '' ? substr($via, 0, 255) : null;

    return [
        'client_ip' => portal_api_login_client_ip(),
        'forwarded_for' => $xff,
        'http_via' => $via,
    ];
}

/**
 * @param array{
 *   account_id?: int|null,
 *   username: string,
 *   result_code: string,
 *   login_success: bool,
 *   parent_id?: int|null,
 *   valid_from_snapshot?: string|null,
 *   valid_until_snapshot?: string|null,
 *   account_disabled_snapshot?: int|null
 * } $row
 */
function portal_api_login_log_write(PDO $pdo, array $row): void
{
    try {
        $meta = portal_api_login_request_meta();
        $st = $pdo->prepare(
            'INSERT INTO portal_api_login_log (
                account_id, username, result_code, login_success,
                parent_id, valid_from_snapshot, valid_until_snapshot, account_disabled_snapshot,
                client_ip, forwarded_for, http_via
            ) VALUES (
                :account_id, :username, :result_code, :login_success,
                :parent_id, :valid_from_snapshot, :valid_until_snapshot, :account_disabled_snapshot,
                :client_ip, :forwarded_for, :http_via
            )'
        );
        $st->execute([
            ':account_id' => $row['account_id'] ?? null,
            ':username' => substr((string) $row['username'], 0, 64),
            ':result_code' => substr((string) $row['result_code'], 0, 32),
            ':login_success' => !empty($row['login_success']) ? 1 : 0,
            ':parent_id' => $row['parent_id'] ?? null,
            ':valid_from_snapshot' => $row['valid_from_snapshot'] ?? null,
            ':valid_until_snapshot' => $row['valid_until_snapshot'] ?? null,
            ':account_disabled_snapshot' => isset($row['account_disabled_snapshot']) ? (int) $row['account_disabled_snapshot'] : null,
            ':client_ip' => $meta['client_ip'],
            ':forwarded_for' => $meta['forwarded_for'],
            ':http_via' => $meta['http_via'],
        ]);
    } catch (Throwable $e) {
        // 表未建或 DB 异常时不影响登录接口
    }
}
