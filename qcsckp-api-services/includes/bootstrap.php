<?php
declare(strict_types=1);

session_start();

$configPath = dirname(__DIR__) . '/config.php';
if (!is_readable($configPath)) {
    http_response_code(500);
    exit('请复制 config.example.php 为 config.php 并配置数据库。');
}
$config = require $configPath;

require_once __DIR__ . '/db.php';
require_once __DIR__ . '/auth.php';
