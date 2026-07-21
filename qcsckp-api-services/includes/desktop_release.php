<?php
declare(strict_types=1);

/**
 * @return array<int, array<string,mixed>>
 */
function desktop_releases_all(PDO $pdo, ?string $platform = null, ?string $kind = null): array
{
    if ($platform !== null && $kind !== null) {
        $st = $pdo->prepare(
            'SELECT id, platform, kind, version, storage_name, original_filename, file_size, created_at
             FROM desktop_releases WHERE platform = ? AND kind = ? ORDER BY id DESC'
        );
        $st->execute([$platform, $kind]);
        return $st->fetchAll();
    }
    if ($platform !== null) {
        $st = $pdo->prepare(
            'SELECT id, platform, kind, version, storage_name, original_filename, file_size, created_at
             FROM desktop_releases WHERE platform = ? ORDER BY id DESC'
        );
        $st->execute([$platform]);
        return $st->fetchAll();
    }
    $st = $pdo->query(
        'SELECT id, platform, kind, version, storage_name, original_filename, file_size, created_at
         FROM desktop_releases ORDER BY id DESC'
    );
    return $st->fetchAll();
}

/**
 * 按 PHP version_compare 在指定平台与类型下选出版本号最大的一条。
 *
 * @return array<string,mixed>|null
 */
function desktop_release_latest(PDO $pdo, string $platform, string $kind): ?array
{
    $st = $pdo->prepare(
        'SELECT id, platform, kind, version, storage_name, original_filename, file_size, created_at
         FROM desktop_releases WHERE platform = ? AND kind = ?'
    );
    $st->execute([$platform, $kind]);
    $rows = $st->fetchAll();
    if (!$rows) {
        return null;
    }
    $best = null;
    foreach ($rows as $r) {
        $v = (string) $r['version'];
        if ($best === null) {
            $best = $r;
            continue;
        }
        if (version_compare($v, (string) $best['version'], '>')) {
            $best = $r;
        }
    }
    return $best;
}

/**
 * 首页下载展示：优先「安装包」，若尚无记录则回退到「更新包」。
 * 兼容迁移后仅有 win+update 的旧数据，避免首页空白。
 *
 * @return array<string,mixed>|null
 */
function desktop_release_latest_for_homepage(PDO $pdo, string $platform): ?array
{
    $install = desktop_release_latest($pdo, $platform, 'install');
    if ($install !== null) {
        return $install;
    }
    return desktop_release_latest($pdo, $platform, 'update');
}

function desktop_release_format_size(int $bytes): string
{
    if ($bytes < 1024) {
        return $bytes . ' B';
    }
    if ($bytes < 1048576) {
        return round($bytes / 1024, 1) . ' KB';
    }
    return round($bytes / 1048576, 2) . ' MB';
}

function desktop_release_upload_dir(): string
{
    return dirname(__DIR__) . '/uploads/desktop';
}

function desktop_release_public_path(string $storageName): string
{
    return '/uploads/desktop/' . $storageName;
}

function desktop_release_request_scheme(): string
{
    if (!empty($_SERVER['HTTP_X_FORWARDED_PROTO']) && in_array($_SERVER['HTTP_X_FORWARDED_PROTO'], ['http', 'https'], true)) {
        return $_SERVER['HTTP_X_FORWARDED_PROTO'];
    }
    if (!empty($_SERVER['HTTPS']) && $_SERVER['HTTPS'] !== 'off') {
        return 'https';
    }
    if (isset($_SERVER['SERVER_PORT']) && (string) $_SERVER['SERVER_PORT'] === '443') {
        return 'https';
    }
    return 'http';
}

function desktop_release_absolute_download_url(string $storageName): string
{
    $scheme = desktop_release_request_scheme();
    $host = $_SERVER['HTTP_HOST'] ?? 'localhost';
    return $scheme . '://' . $host . desktop_release_public_path($storageName);
}

/**
 * 从 GET / POST JSON / POST 表单读取 current_version。
 */
function desktop_release_parse_current_version_from_request(): string
{
    if ($_SERVER['REQUEST_METHOD'] === 'POST') {
        $raw = file_get_contents('php://input');
        $input = [];
        if ($raw !== '' && $raw !== false) {
            $j = json_decode($raw, true);
            if (is_array($j)) {
                $input = $j;
            }
        }
        return trim((string) ($input['current_version'] ?? $_POST['current_version'] ?? ''));
    }
    return trim((string) ($_GET['current_version'] ?? ''));
}

/**
 * 输出与 /api/version.php 相同结构的 JSON（按平台 + 类型取最新更新包）。
 */
function desktop_release_emit_version_json(PDO $pdo, string $platform, string $kind): void
{
    header('Content-Type: application/json; charset=utf-8');
    header('Cache-Control: no-store, no-cache, must-revalidate');

    $current = desktop_release_parse_current_version_from_request();
    $latest = desktop_release_latest($pdo, $platform, $kind);

    if ($latest === null) {
        echo json_encode([
            'success' => true,
            'data' => [
                'latest_version' => null,
                'has_update' => false,
                'download_url' => null,
                'file_size' => null,
                'original_filename' => null,
            ],
        ], JSON_UNESCAPED_UNICODE);
        return;
    }

    $cv = $current === '' ? '0' : $current;
    $lv = (string) $latest['version'];
    $hasUpdate = version_compare($lv, $cv, '>');
    $url = desktop_release_absolute_download_url((string) $latest['storage_name']);

    echo json_encode([
        'success' => true,
        'data' => [
            'latest_version' => $lv,
            'has_update' => $hasUpdate,
            'download_url' => $url,
            'file_size' => (int) $latest['file_size'],
            'original_filename' => (string) $latest['original_filename'],
        ],
    ], JSON_UNESCAPED_UNICODE);
}
