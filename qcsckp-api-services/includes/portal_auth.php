<?php
declare(strict_types=1);

/**
 * 门户（普通用户）会话：与后台 admin 的 $_SESSION['user_id'] 分离，使用 portal_uid。
 */

function portal_uid(): ?int
{
    if (empty($_SESSION['portal_uid'])) {
        return null;
    }
    return (int) $_SESSION['portal_uid'];
}

/**
 * @return array<string,mixed>|null 含 id, username, role, valid_from, valid_until, parent_id, is_disabled
 */
function portal_user(PDO $pdo): ?array
{
    $uid = portal_uid();
    if ($uid === null) {
        return null;
    }
    $st = $pdo->prepare(
        'SELECT id, username, role, parent_id, valid_from, valid_until, is_disabled FROM accounts WHERE id = ? LIMIT 1'
    );
    $st->execute([$uid]);
    $row = $st->fetch(PDO::FETCH_ASSOC);
    if (!$row || $row['role'] !== 'user') {
        unset($_SESSION['portal_uid'], $_SESSION['portal_aadvid']);
        return null;
    }
    if ((int) $row['is_disabled'] === 1) {
        unset($_SESSION['portal_uid'], $_SESSION['portal_aadvid']);
        return null;
    }
    if (!empty($row['parent_id'])) {
        $ag = $pdo->prepare('SELECT is_disabled FROM accounts WHERE id = ? AND role = ?');
        $ag->execute([(int) $row['parent_id'], 'agent']);
        $agent = $ag->fetch(PDO::FETCH_ASSOC);
        if ($agent && (int) $agent['is_disabled'] === 1) {
            unset($_SESSION['portal_uid'], $_SESSION['portal_aadvid']);
            return null;
        }
    }
    $now = new DateTimeImmutable('now', new DateTimeZone('Asia/Shanghai'));
    if (!empty($row['valid_from'])) {
        $vf = new DateTimeImmutable((string) $row['valid_from'], new DateTimeZone('Asia/Shanghai'));
        if ($now < $vf) {
            unset($_SESSION['portal_uid'], $_SESSION['portal_aadvid']);
            return null;
        }
    }
    if (!empty($row['valid_until'])) {
        $vu = new DateTimeImmutable((string) $row['valid_until'], new DateTimeZone('Asia/Shanghai'));
        if ($now > $vu) {
            unset($_SESSION['portal_uid'], $_SESSION['portal_aadvid']);
            return null;
        }
    }
    return $row;
}

/**
 * @return array<string,mixed>|null
 */
function portal_require_login(PDO $pdo): ?array
{
    $u = portal_user($pdo);
    if (!$u) {
        header('Location: /login.php');
        exit;
    }
    return $u;
}

/**
 * @return array<string,mixed>|null
 */
function portal_try_login(PDO $pdo, string $username, string $password): ?array
{
    $st = $pdo->prepare(
        'SELECT id, username, password_hash, role, parent_id, valid_from, valid_until, is_disabled FROM accounts WHERE username = ? LIMIT 1'
    );
    $st->execute([$username]);
    $row = $st->fetch(PDO::FETCH_ASSOC);
    if (!$row || $row['role'] !== 'user') {
        return null;
    }
    if ((int) $row['is_disabled'] === 1) {
        return null;
    }
    if (!password_verify($password, (string) $row['password_hash'])) {
        return null;
    }
    if (!empty($row['parent_id'])) {
        $ag = $pdo->prepare('SELECT is_disabled FROM accounts WHERE id = ? AND role = ?');
        $ag->execute([(int) $row['parent_id'], 'agent']);
        $agent = $ag->fetch(PDO::FETCH_ASSOC);
        if ($agent && (int) $agent['is_disabled'] === 1) {
            return null;
        }
    }
    $now = new DateTimeImmutable('now', new DateTimeZone('Asia/Shanghai'));
    if (!empty($row['valid_from'])) {
        $vf = new DateTimeImmutable((string) $row['valid_from'], new DateTimeZone('Asia/Shanghai'));
        if ($now < $vf) {
            return null;
        }
    }
    if (!empty($row['valid_until'])) {
        $vu = new DateTimeImmutable((string) $row['valid_until'], new DateTimeZone('Asia/Shanghai'));
        if ($now > $vu) {
            return null;
        }
    }
    return $row;
}

function portal_logout(): void
{
    unset($_SESSION['portal_uid'], $_SESSION['portal_aadvid'], $_SESSION['portal_dashboard_label']);
}

function portal_selected_aadvid(): ?string
{
    $a = $_SESSION['portal_aadvid'] ?? null;
    if (!is_string($a) || $a === '') {
        return null;
    }
    return $a;
}
