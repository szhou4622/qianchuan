<?php
/**
 * 复制为 config.php 并填写数据库等配置（不要将含真实密码的 config.php 提交到公开仓库）
 */
return [
    'db' => [
        'host' => '127.0.0.1',
        'port' => 3306,
        'name' => 'qcscjk',
        'user' => 'qcscjk',
        'pass' => 'your_password_here',
        'charset' => 'utf8mb4',
    ],
    // 飞书自建应用机器人。真实值只放 config.php，不要提交到仓库。
    'feishu_app' => [
        'enabled' => false,
        'app_id' => '',
        'app_secret' => '',
        'verification_token' => '',
        'encrypt_key' => '',
        // 单一批准人；群内其他成员只能查看，不能执行追投。
        'authorized_open_id' => '',
        // 可同时发送到多个群和个人；留空数组表示不向该类型发送。
        'chat_ids' => [],
        'open_ids' => [],
    ],
];
