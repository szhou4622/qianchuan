<?php
declare(strict_types=1);

function csrf_token(): string
{
    if (empty($_SESSION['_csrf'])) {
        $_SESSION['_csrf'] = bin2hex(random_bytes(32));
    }
    return $_SESSION['_csrf'];
}

function csrf_verify(?string $token): bool
{
    return is_string($token)
        && isset($_SESSION['_csrf'])
        && hash_equals($_SESSION['_csrf'], $token);
}

function current_user(PDO $pdo): ?array
{
    if (empty($_SESSION['user_id'])) {
        return null;
    }
    $st = $pdo->prepare(
        'SELECT id, username, role, is_disabled FROM accounts WHERE id = ? LIMIT 1'
    );
    $st->execute([(int) $_SESSION['user_id']]);
    $row = $st->fetch();
    if (!$row || (int) $row['is_disabled'] === 1) {
        return null;
    }
    return $row;
}

function require_panel_login(PDO $pdo): array
{
    $u = current_user($pdo);
    if (!$u || !in_array($u['role'], ['super_admin', 'agent'], true)) {
        header('Location: /admin/login.php');
        exit;
    }
    return $u;
}

function require_super_admin(PDO $pdo): array
{
    $u = require_panel_login($pdo);
    if ($u['role'] !== 'super_admin') {
        http_response_code(403);
        exit('无权访问');
    }
    return $u;
}

function login_user(PDO $pdo, string $username, string $password): ?array
{
    $st = $pdo->prepare(
        'SELECT id, username, password_hash, role, is_disabled FROM accounts WHERE username = ? LIMIT 1'
    );
    $st->execute([$username]);
    $row = $st->fetch();
    if (!$row || (int) $row['is_disabled'] === 1) {
        return null;
    }
    if (!in_array($row['role'], ['super_admin', 'agent'], true)) {
        return null;
    }
    if (!password_verify($password, $row['password_hash'])) {
        return null;
    }
    return $row;
}
