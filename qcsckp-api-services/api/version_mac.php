<?php
declare(strict_types=1);

require_once dirname(__DIR__) . '/includes/bootstrap.php';
require_once dirname(__DIR__) . '/includes/desktop_release.php';

// macOS 客户端：检测「更新包」（kind=update）
desktop_release_emit_version_json($pdo, 'mac', 'update');
