<?php
declare(strict_types=1);

session_start();

$overrideConfigPath = trim((string)(getenv('QCSCKP_SERVER_CONFIG') ?: ''));
$configPath = $overrideConfigPath !== '' ? $overrideConfigPath : dirname(__DIR__) . '/config.php';
if (!is_readable($configPath)) {
    http_response_code(500);
    exit('服务端配置文件不存在或不可读。');
}
$config = require $configPath;
if (!is_array($config)) {
    http_response_code(500);
    exit('服务端配置文件格式无效。');
}

require_once __DIR__ . '/db.php';
require_once __DIR__ . '/auth.php';
