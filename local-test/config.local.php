<?php
declare(strict_types=1);

$secretPath = trim((string)(getenv('QCSCKP_LOCAL_SECRETS') ?: ''));
if ($secretPath === '' || !is_readable($secretPath)) {
    throw new RuntimeException('本地测试密钥文件不存在。');
}
$raw = file_get_contents($secretPath);
$secrets = is_string($raw) ? json_decode($raw, true) : null;
if (!is_array($secrets) || !is_array($secrets['db'] ?? null) || !is_array($secrets['feishu_app'] ?? null)) {
    throw new RuntimeException('本地测试密钥文件格式无效。');
}

return [
    'db' => $secrets['db'],
    'feishu_app' => $secrets['feishu_app'],
];
