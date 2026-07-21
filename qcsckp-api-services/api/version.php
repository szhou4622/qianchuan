<?php
declare(strict_types=1);

require_once dirname(__DIR__) . '/includes/bootstrap.php';
require_once dirname(__DIR__) . '/includes/desktop_release.php';

// Windows 客户端：检测「更新包」（kind=update），与首页完整安装包（win+install）分离
desktop_release_emit_version_json($pdo, 'win', 'update');
