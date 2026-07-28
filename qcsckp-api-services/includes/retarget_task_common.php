<?php
declare(strict_types=1);

const RETARGET_ACTIVE_STATUSES = ['pending', 'approved_queued', 'claimed', 'executing'];
const RETARGET_TERMINAL_STATUSES = ['succeeded', 'failed', 'rejected', 'expired', 'cancelled'];

function api_json(array $payload, int $status = 200): void
{
    http_response_code($status);
    header('Content-Type: application/json; charset=utf-8');
    echo json_encode($payload, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
    exit;
}

function api_json_input(): array
{
    $raw = file_get_contents('php://input');
    if (!is_string($raw) || $raw === '') {
        return [];
    }
    $data = json_decode($raw, true);
    return is_array($data) ? $data : [];
}

function ensure_retarget_task_schema(PDO $pdo): void
{
    $pdo->exec("CREATE TABLE IF NOT EXISTS desktop_device_sessions (
      id bigint unsigned NOT NULL AUTO_INCREMENT,
      user_id int unsigned NOT NULL,
      token_hash char(64) NOT NULL,
      device_name varchar(120) NOT NULL DEFAULT '',
      expires_at datetime NOT NULL,
      revoked_at datetime DEFAULT NULL,
      last_seen_at datetime DEFAULT NULL,
      created_at datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
      PRIMARY KEY (id),
      UNIQUE KEY uk_device_token_hash (token_hash),
      KEY idx_device_user (user_id),
      KEY idx_device_expiry (expires_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci");

    $pdo->exec("CREATE TABLE IF NOT EXISTS retarget_tasks (
      id bigint unsigned NOT NULL AUTO_INCREMENT,
      task_uid char(36) NOT NULL,
      user_id int unsigned NOT NULL,
      active_dedupe_key char(64) DEFAULT NULL,
      aavid varchar(64) NOT NULL,
      account_name varchar(200) NOT NULL DEFAULT '',
      ad_id varchar(64) NOT NULL,
      target_uid varchar(64) NOT NULL DEFAULT 'legacy_unscoped',
      plan_name varchar(256) NOT NULL DEFAULT '',
      promotion_scene varchar(32) NOT NULL DEFAULT 'live',
      plan_system varchar(32) NOT NULL DEFAULT 'unknown',
      trigger_level varchar(32) NOT NULL DEFAULT 'material',
      product_id varchar(128) NOT NULL DEFAULT '',
      product_name varchar(512) NOT NULL DEFAULT '',
      material_id varchar(128) NOT NULL,
      material_name varchar(512) NOT NULL DEFAULT '',
      materials_json longtext,
      strategy_id varchar(128) NOT NULL,
      strategy_name varchar(128) NOT NULL DEFAULT '',
      strategy_hash char(64) NOT NULL,
      status varchar(32) NOT NULL,
      action_nonce char(64) NOT NULL,
      trigger_snapshot_json longtext,
      query_snapshot_json longtext,
      retargeting_json longtext,
      rule_snapshot_json longtext,
      clicker_open_id varchar(128) DEFAULT NULL,
      claimed_device varchar(120) DEFAULT NULL,
      claim_token char(64) DEFAULT NULL,
      lease_expires_at datetime DEFAULT NULL,
      regulate_task_id varchar(128) DEFAULT NULL,
      result_message text,
      result_detail longtext,
      result_json longtext,
      approved_at datetime DEFAULT NULL,
      started_at datetime DEFAULT NULL,
      finished_at datetime DEFAULT NULL,
      expires_at datetime NOT NULL,
      created_at datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
      updated_at datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
      PRIMARY KEY (id),
      UNIQUE KEY uk_retarget_task_uid (task_uid),
      UNIQUE KEY uk_retarget_active_dedupe (active_dedupe_key),
      KEY idx_retarget_user_status (user_id,status,expires_at),
      KEY idx_retarget_target_time (user_id,target_uid,created_at),
      KEY idx_retarget_account_time (user_id,aavid,created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci");

    // 本地测试库可能由旧版创建，按列增量补齐；重复列错误安全忽略。
    foreach ([
        "ADD COLUMN target_uid varchar(64) NOT NULL DEFAULT 'legacy_unscoped' AFTER ad_id",
        "ADD COLUMN plan_name varchar(256) NOT NULL DEFAULT '' AFTER target_uid",
        "ADD COLUMN promotion_scene varchar(32) NOT NULL DEFAULT 'live' AFTER plan_name",
        "ADD COLUMN plan_system varchar(32) NOT NULL DEFAULT 'unknown' AFTER promotion_scene",
        "ADD COLUMN trigger_level varchar(32) NOT NULL DEFAULT 'material' AFTER plan_system",
        "ADD COLUMN product_id varchar(128) NOT NULL DEFAULT '' AFTER trigger_level",
        "ADD COLUMN product_name varchar(512) NOT NULL DEFAULT '' AFTER product_id",
        "ADD COLUMN materials_json longtext AFTER material_name",
    ] as $alter) {
        try {
            $pdo->exec("ALTER TABLE retarget_tasks $alter");
        } catch (PDOException $ignored) {
            // 1060: Duplicate column name。其他迁移错误会由后续真实查询明确暴露。
            if ((int)($ignored->errorInfo[1] ?? 0) !== 1060) {
                throw $ignored;
            }
        }
    }
    try {
        $pdo->exec(
            "ALTER TABLE retarget_tasks ADD KEY idx_retarget_target_time "
            . "(user_id,target_uid,created_at)"
        );
    } catch (PDOException $ignored) {
        if ((int)($ignored->errorInfo[1] ?? 0) !== 1061) {
            throw $ignored;
        }
    }

    $pdo->exec("CREATE TABLE IF NOT EXISTS retarget_task_messages (
      id bigint unsigned NOT NULL AUTO_INCREMENT,
      task_id bigint unsigned NOT NULL,
      receive_id_type varchar(32) NOT NULL,
      receive_id varchar(128) NOT NULL,
      message_id varchar(128) NOT NULL,
      created_at datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
      updated_at datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
      PRIMARY KEY (id),
      UNIQUE KEY uk_retarget_task_message (task_id,message_id),
      KEY idx_retarget_message_task (task_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci");

    $pdo->exec("CREATE TABLE IF NOT EXISTS retarget_card_update_jobs (
      id bigint unsigned NOT NULL AUTO_INCREMENT,
      task_id bigint unsigned NOT NULL,
      expanded tinyint(1) NOT NULL DEFAULT 0,
      attempts int unsigned NOT NULL DEFAULT 0,
      available_at datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
      locked_at datetime DEFAULT NULL,
      last_error varchar(1000) DEFAULT NULL,
      created_at datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
      updated_at datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
      PRIMARY KEY (id),
      UNIQUE KEY uk_retarget_card_update_task (task_id),
      KEY idx_retarget_card_update_due (available_at,locked_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci");
}

function user_is_active(PDO $pdo, array $row): bool
{
    if ((int)($row['is_disabled'] ?? 0) === 1 || ($row['role'] ?? '') !== 'user') {
        return false;
    }
    $now = time();
    if (!empty($row['valid_from']) && strtotime((string)$row['valid_from']) > $now) {
        return false;
    }
    if (!empty($row['valid_until']) && strtotime((string)$row['valid_until']) < $now) {
        return false;
    }
    if (!empty($row['parent_id'])) {
        $st = $pdo->prepare('SELECT is_disabled FROM accounts WHERE id=? AND role=? LIMIT 1');
        $st->execute([(int)$row['parent_id'], 'agent']);
        $agent = $st->fetch();
        if ($agent && (int)$agent['is_disabled'] === 1) {
            return false;
        }
    }
    return true;
}

function authenticate_password_user(PDO $pdo, string $username, string $password): ?array
{
    $st = $pdo->prepare(
        'SELECT id,username,password_hash,role,parent_id,valid_from,valid_until,is_disabled FROM accounts WHERE username=? LIMIT 1'
    );
    $st->execute([trim($username)]);
    $row = $st->fetch();
    if (!$row || !password_verify($password, (string)$row['password_hash']) || !user_is_active($pdo, $row)) {
        return null;
    }
    return $row;
}

function bearer_token(): string
{
    $header = $_SERVER['HTTP_AUTHORIZATION'] ?? '';
    if (!$header && function_exists('getallheaders')) {
        $headers = getallheaders();
        $header = $headers['Authorization'] ?? $headers['authorization'] ?? '';
    }
    return preg_match('/^Bearer\s+(.+)$/i', trim((string)$header), $m) ? trim($m[1]) : '';
}

function authenticate_device(PDO $pdo): array
{
    ensure_retarget_task_schema($pdo);
    $token = bearer_token();
    if ($token === '') {
        api_json(['success' => false, 'message' => '缺少设备令牌'], 401);
    }
    $hash = hash('sha256', $token);
    $st = $pdo->prepare(
        'SELECT s.id AS session_id,s.device_name,a.id,a.username,a.role,a.parent_id,a.valid_from,a.valid_until,a.is_disabled '
        . 'FROM desktop_device_sessions s JOIN accounts a ON a.id=s.user_id '
        . 'WHERE s.token_hash=? AND s.revoked_at IS NULL AND s.expires_at>NOW() LIMIT 1'
    );
    $st->execute([$hash]);
    $row = $st->fetch();
    if (!$row || !user_is_active($pdo, $row)) {
        api_json(['success' => false, 'message' => '设备令牌无效或已过期'], 401);
    }
    $pdo->prepare('UPDATE desktop_device_sessions SET last_seen_at=NOW() WHERE id=?')->execute([(int)$row['session_id']]);
    return $row;
}

function uuid_v4(): string
{
    $data = random_bytes(16);
    $data[6] = chr((ord($data[6]) & 0x0f) | 0x40);
    $data[8] = chr((ord($data[8]) & 0x3f) | 0x80);
    return vsprintf('%s%s-%s-%s-%s-%s%s%s', str_split(bin2hex($data), 4));
}

function feishu_config(array $config): array
{
    $f = $config['feishu_app'] ?? [];
    return is_array($f) ? $f : [];
}

function local_server_test_mode(): bool
{
    $raw = strtolower(trim((string)(getenv('QCSCKP_SERVER_TEST_MODE') ?: '')));
    return in_array($raw, ['1', 'true', 'yes', 'on'], true);
}

function feishu_mock_enabled(array $config): bool
{
    $f = feishu_config($config);
    return local_server_test_mode() && !empty($f['mock']);
}

function http_json_request(string $method, string $url, array $headers = [], ?array $body = null): array
{
    if (!function_exists('curl_init')) {
        throw new RuntimeException('服务器未启用 cURL，无法调用飞书 OpenAPI');
    }
    $ch = curl_init($url);
    $allHeaders = array_merge(['Content-Type: application/json; charset=utf-8'], $headers);
    curl_setopt_array($ch, [
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_CUSTOMREQUEST => $method,
        CURLOPT_HTTPHEADER => $allHeaders,
        CURLOPT_CONNECTTIMEOUT => 10,
        CURLOPT_TIMEOUT => 20,
    ]);
    if ($body !== null) {
        curl_setopt($ch, CURLOPT_POSTFIELDS, json_encode($body, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES));
    }
    $raw = curl_exec($ch);
    $status = (int)curl_getinfo($ch, CURLINFO_HTTP_CODE);
    $err = curl_error($ch);
    unset($ch);
    if ($raw === false || $err !== '') {
        throw new RuntimeException('飞书请求失败: ' . $err);
    }
    $data = json_decode((string)$raw, true);
    if (!is_array($data)) {
        throw new RuntimeException('飞书返回非JSON，HTTP ' . $status);
    }
    if ($status < 200 || $status >= 300 || (isset($data['code']) && (int)$data['code'] !== 0)) {
        throw new RuntimeException('飞书返回失败: ' . mb_substr((string)($data['msg'] ?? $raw), 0, 500));
    }
    return $data;
}

function feishu_tenant_token(array $config): string
{
    $f = feishu_config($config);
    if (empty($f['enabled']) || empty($f['app_id']) || empty($f['app_secret'])) {
        throw new RuntimeException('飞书自建应用尚未配置');
    }
    $data = http_json_request('POST', 'https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal', [], [
        'app_id' => (string)$f['app_id'],
        'app_secret' => (string)$f['app_secret'],
    ]);
    $token = (string)($data['tenant_access_token'] ?? '');
    if ($token === '') {
        throw new RuntimeException('飞书未返回 tenant_access_token');
    }
    return $token;
}

function safe_json_decode(?string $raw): array
{
    if (!$raw) {
        return [];
    }
    $v = json_decode($raw, true);
    return is_array($v) ? $v : [];
}

function retarget_method_summary(array $retargeting): string
{
    $method = (string)($retargeting['method'] ?? '');
    if ($method === 'volume') {
        $v = is_array($retargeting['volume'] ?? null) ? $retargeting['volume'] : [];
        return sprintf('放量追投｜总预算 %s 元｜时长 %s 小时', $v['total_budget_yuan'] ?? '—', $v['duration_hours'] ?? '—');
    }
    $cc = is_array($retargeting['cost_control'] ?? null) ? $retargeting['cost_control'] : [];
    if (($cc['optimization_goal'] ?? '') === 'live_room') {
        $x = is_array($cc['live_room'] ?? null) ? $cc['live_room'] : [];
        return sprintf('控成本追投｜日预算 %s 元｜出价 %s 元', $x['daily_budget_yuan'] ?? '—', $x['bid_per_conversion_yuan'] ?? '—');
    }
    $x = is_array($cc['net_roi'] ?? null) ? $cc['net_roi'] : [];
    return sprintf('控成本追投｜日预算 %s 元｜净成交ROI %s', $x['daily_budget_yuan'] ?? '—', $x['net_roi_target'] ?? '—');
}

function trigger_metric_summary(array $trigger): string
{
    $evaluation = is_array($trigger['evaluation'] ?? null) ? $trigger['evaluation'] : [];
    $lines = [];
    foreach (($evaluation['groups'] ?? []) as $group) {
        if (!is_array($group)) continue;
        foreach (($group['conditions'] ?? []) as $condition) {
            if (!is_array($condition) || empty($condition['passed'])) continue;
            $metric = (string)($condition['metric'] ?? '指标');
            $op = ['gt' => '>', 'gte' => '≥', 'lt' => '<', 'lte' => '≤', 'eq' => '='][(string)($condition['op'] ?? '')] ?? (string)($condition['op'] ?? '');
            $lines[] = $metric . ' ' . ($condition['actual'] ?? '—') . ' ' . $op . ' ' . ($condition['threshold'] ?? '—');
            if (count($lines) >= 5) break 2;
        }
    }
    return $lines ? implode('；', $lines) : '规则条件已命中';
}

function trigger_metric_details(array $trigger): string
{
    $evaluation = is_array($trigger['evaluation'] ?? null) ? $trigger['evaluation'] : [];
    $lines = [];
    foreach (($evaluation['groups'] ?? []) as $groupIndex => $group) {
        if (!is_array($group)) continue;
        foreach (($group['conditions'] ?? []) as $condition) {
            if (!is_array($condition)) continue;
            $metric = (string)($condition['metric'] ?? '指标');
            $op = ['gt' => '>', 'gte' => '≥', 'lt' => '<', 'lte' => '≤', 'eq' => '='][(string)($condition['op'] ?? '')] ?? (string)($condition['op'] ?? '');
            $passed = !empty($condition['passed']) ? '命中' : '未命中';
            $lines[] = sprintf('组%s｜%s：%s %s %s（%s）', (int)$groupIndex + 1, $metric, $condition['actual'] ?? '—', $op, $condition['threshold'] ?? '—', $passed);
        }
    }
    return $lines ? implode("\n", $lines) : '未保存条件明细';
}

function task_card(array $task, string $displayStatus = '', bool $expanded = false): array
{
    $retargeting = safe_json_decode((string)($task['retargeting_json'] ?? ''));
    $trigger = safe_json_decode((string)($task['trigger_snapshot_json'] ?? ''));
    $result = safe_json_decode((string)($task['result_json'] ?? ''));
    $materials = safe_json_decode((string)($task['materials_json'] ?? ''));
    if (!$materials || array_keys($materials) !== range(0, count($materials) - 1)) {
        $materials = [[
            'material_id' => (string)($task['material_id'] ?? ''),
            'material_name' => (string)($task['material_name'] ?? ''),
            'product_id' => (string)($task['product_id'] ?? ''),
            'product_name' => (string)($task['product_name'] ?? ''),
        ]];
    }
    $materials = array_values(array_filter(
        $materials,
        static fn($item): bool => is_array($item) && trim((string)($item['material_id'] ?? '')) !== ''
    ));
    $status = $displayStatus !== '' ? $displayStatus : (string)($task['status'] ?? 'pending');
    $statusText = [
        'pending' => '等待确认', 'approved_queued' => '已批准，等待桌面工具', 'claimed' => '桌面工具已领取',
        'executing' => '正在追投', 'succeeded' => '追投成功', 'failed' => '追投失败',
        'rejected' => '已暂不追投', 'expired' => '已过期', 'cancelled' => '已取消',
    ][$status] ?? $status;
    $template = $status === 'succeeded' ? 'green' : (in_array($status, ['failed','expired','rejected'], true) ? 'red' : 'blue');
    $reason = (string)($trigger['strategy_title'] ?? $task['strategy_name'] ?? '追投策略命中');
    $sceneText = (($task['promotion_scene'] ?? 'live') === 'product') ? '推商品' : '推直播';
    $planSystemText = [
        'global' => '传统全域',
        'chengfang' => '千川乘方',
        'unknown' => '待确认',
    ][(string)($task['plan_system'] ?? 'unknown')] ?? '待确认';
    $levelText = (($task['trigger_level'] ?? 'material') === 'product') ? '商品级' : '素材级';
    $productLine = '';
    if (!empty($task['product_id'])) {
        $productLine = "\n**商品名称：** " . (($task['product_name'] ?? '') ?: '未命名商品')
            . "\n**商品ID：** `" . ($task['product_id'] ?? '') . '`';
    }
    $accountName = trim((string)($task['account_name'] ?? '')) ?: '未命名账户';
    $planName = trim((string)($task['plan_name'] ?? '')) ?: '未命名计划';
    $materialLines = [];
    foreach ($materials as $index => $material) {
        $materialName = mb_substr(
            trim((string)($material['material_name'] ?? '')) ?: '未命名素材',
            0,
            160
        );
        $materialId = trim((string)($material['material_id'] ?? ''));
        $materialProductName = trim((string)($material['product_name'] ?? ''));
        $materialProductId = trim((string)($material['product_id'] ?? ''));
        $line = ((int)$index + 1) . '. ' . $materialName . "\n   素材ID：`" . $materialId . '`';
        if ($materialProductName !== '' || $materialProductId !== '') {
            $line .= "\n   关联商品：" . ($materialProductName !== '' ? $materialProductName : '未命名商品');
            if ($materialProductId !== '') {
                $line .= '（`' . $materialProductId . '`）';
            }
        }
        $materialLines[] = $line;
    }
    $materialCount = count($materialLines);
    $elements = [
        ['tag' => 'markdown', 'content' => "**千川账户：** " . $accountName
            . "\n**账户ID：** `" . ($task['aavid'] ?? '') . '`'
            . "\n**计划名称：** " . $planName
            . "\n**计划ID：** `" . ($task['ad_id'] ?? '') . '`'
            . "\n**推广场景：** " . $sceneText
            . "\n**计划体系：** " . $planSystemText
            . "\n**触发层级：** " . $levelText
            . $productLine
            . "\n\n**本卡追投素材（" . $materialCount . "条）：**"
            . "\n" . implode("\n", $materialLines)
            . "\n\n**策略：** " . $reason],
        ['tag' => 'markdown', 'content' => "**命中原因：** " . trigger_metric_summary($trigger) . "\n**追投参数：** " . retarget_method_summary($retargeting) . "\n**有效期至：** " . ($task['expires_at'] ?? '') . "\n**当前状态：** " . $statusText],
    ];
    if (!empty($task['result_message'])) {
        $elements[] = ['tag' => 'markdown', 'content' => "**执行结果：** " . mb_substr((string)$task['result_message'], 0, 500)];
    }
    $regulateTaskIds = [];
    foreach ((is_array($result['regulate_task_ids'] ?? null) ? $result['regulate_task_ids'] : []) as $taskId) {
        $taskId = mb_substr(trim((string)$taskId), 0, 128);
        if ($taskId !== '' && !in_array($taskId, $regulateTaskIds, true)) $regulateTaskIds[] = $taskId;
    }
    if (!$regulateTaskIds && !empty($task['regulate_task_id'])) {
        $regulateTaskIds[] = mb_substr((string)$task['regulate_task_id'], 0, 128);
    }
    if ($regulateTaskIds) {
        $taskIdLines = array_map(
            static fn($taskId, $index): string => ((int)$index + 1) . '. `' . $taskId . '`',
            $regulateTaskIds,
            array_keys($regulateTaskIds)
        );
        $elements[] = ['tag' => 'markdown', 'content' => "**千川调控任务ID：**\n" . implode("\n", $taskIdLines)];
    }
    if ($expanded) {
        $prettyRetargeting = json_encode($retargeting, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES | JSON_PRETTY_PRINT);
        $elements[] = ['tag' => 'hr'];
        $elements[] = ['tag' => 'markdown', 'content' => "**完整触发条件**\n" . mb_substr(trigger_metric_details($trigger), 0, 2500)];
        $elements[] = ['tag' => 'markdown', 'content' => "**追投参数快照**\n```json\n" . mb_substr((string)$prettyRetargeting, 0, 2500) . "\n```"];
    }
    if ($status === 'pending') {
        $valueBase = ['task_uid' => (string)$task['task_uid'], 'nonce' => (string)$task['action_nonce']];
        $elements[] = [
            'tag' => 'action',
            'actions' => [
                ['tag' => 'button', 'text' => ['tag' => 'plain_text', 'content' => '确认追投'], 'type' => 'primary', 'value' => $valueBase + ['action' => 'approve']],
                ['tag' => 'button', 'text' => ['tag' => 'plain_text', 'content' => '暂不追投'], 'type' => 'danger', 'value' => $valueBase + ['action' => 'reject']],
                ['tag' => 'button', 'text' => ['tag' => 'plain_text', 'content' => '查看详情'], 'value' => $valueBase + ['action' => 'view']],
            ],
        ];
    }
    return [
        'config' => ['wide_screen_mode' => true, 'enable_forward' => false, 'update_multi' => true],
        'header' => ['template' => $template, 'title' => ['tag' => 'plain_text', 'content' => '千川追投提醒 · ' . $sceneText . ' · ' . $planSystemText . ' · ' . $statusText]],
        'elements' => $elements,
    ];
}

function send_task_cards(PDO $pdo, array $config, array $task): int
{
    $f = feishu_config($config);
    $targets = [];
    foreach (($f['chat_ids'] ?? []) as $id) {
        if (trim((string)$id) !== '') $targets[] = ['chat_id', trim((string)$id)];
    }
    foreach (($f['open_ids'] ?? []) as $id) {
        if (trim((string)$id) !== '') $targets[] = ['open_id', trim((string)$id)];
    }
    if (!$targets) {
        throw new RuntimeException('飞书未配置群ID或个人Open ID');
    }
    if (feishu_mock_enabled($config)) {
        $sent = 0;
        foreach ($targets as [$type, $id]) {
            $messageId = 'mock_' . hash('sha256', (string)$task['task_uid'] . '|' . $type . '|' . $id);
            $st = $pdo->prepare('INSERT IGNORE INTO retarget_task_messages(task_id,receive_id_type,receive_id,message_id) VALUES(?,?,?,?)');
            $st->execute([(int)$task['id'], $type, $id, $messageId]);
            $sent++;
        }
        return $sent;
    }
    $token = feishu_tenant_token($config);
    $card = task_card($task);
    $sent = 0;
    $errors = [];
    foreach ($targets as [$type, $id]) {
        try {
            $url = 'https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=' . rawurlencode($type);
            $res = http_json_request('POST', $url, ['Authorization: Bearer ' . $token], [
                'receive_id' => $id,
                'msg_type' => 'interactive',
                'content' => json_encode($card, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES),
            ]);
            $messageId = (string)($res['data']['message_id'] ?? '');
            if ($messageId !== '') {
                $st = $pdo->prepare('INSERT IGNORE INTO retarget_task_messages(task_id,receive_id_type,receive_id,message_id) VALUES(?,?,?,?)');
                $st->execute([(int)$task['id'], $type, $id, $messageId]);
                $sent++;
            }
        } catch (Throwable $e) {
            $errors[] = $type . ':' . $id . ' ' . $e->getMessage();
            error_log('send Feishu task card failed: ' . end($errors));
        }
    }
    if ($sent === 0) {
        throw new RuntimeException($errors ? implode('; ', $errors) : '飞书未返回消息ID');
    }
    return $sent;
}

function update_task_cards(PDO $pdo, array $config, array $task, bool $expanded = false): bool
{
    $messages = $pdo->prepare('SELECT message_id FROM retarget_task_messages WHERE task_id=?');
    $messages->execute([(int)$task['id']]);
    $rows = $messages->fetchAll();
    if (!$rows) return true;
    if (feishu_mock_enabled($config)) return true;
    $ok = true;
    try {
        $token = feishu_tenant_token($config);
        $content = json_encode(task_card($task, '', $expanded), JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
        foreach ($rows as $row) {
            $url = 'https://open.feishu.cn/open-apis/im/v1/messages/' . rawurlencode((string)$row['message_id']);
            try {
                http_json_request('PATCH', $url, ['Authorization: Bearer ' . $token], ['content' => $content]);
            } catch (Throwable $ignored) {
                $ok = false;
                error_log('update Feishu task card failed: ' . $ignored->getMessage());
            }
        }
    } catch (Throwable $e) {
        $ok = false;
        error_log('get Feishu token for card update failed: ' . $e->getMessage());
    }
    return $ok;
}

function enqueue_task_card_update(PDO $pdo, int $taskId, bool $expanded = false): void
{
    $st = $pdo->prepare(
        'INSERT INTO retarget_card_update_jobs(task_id,expanded,available_at,locked_at,last_error) '
        . 'VALUES(?,?,NOW(),NULL,NULL) '
        . 'ON DUPLICATE KEY UPDATE expanded=VALUES(expanded),attempts=0,available_at=NOW(),locked_at=NULL,last_error=NULL'
    );
    $st->execute([$taskId, $expanded ? 1 : 0]);
}

function process_task_card_update_queue(PDO $pdo, array $config, int $limit = 20): array
{
    ensure_retarget_task_schema($pdo);
    $limit = max(1, min(100, $limit));
    $jobs = $pdo->query(
        "SELECT id,task_id,expanded,attempts FROM retarget_card_update_jobs "
        . "WHERE available_at<=NOW() AND (locked_at IS NULL OR locked_at<DATE_SUB(NOW(),INTERVAL 2 MINUTE)) "
        . "ORDER BY id ASC LIMIT " . $limit
    )->fetchAll();
    $processed = 0;
    $failed = 0;
    foreach ($jobs as $job) {
        $claim = $pdo->prepare(
            "UPDATE retarget_card_update_jobs SET locked_at=NOW() "
            . "WHERE id=? AND (locked_at IS NULL OR locked_at<DATE_SUB(NOW(),INTERVAL 2 MINUTE))"
        );
        $claim->execute([(int)$job['id']]);
        if ($claim->rowCount() !== 1) continue;
        $taskSt = $pdo->prepare('SELECT * FROM retarget_tasks WHERE id=? LIMIT 1');
        $taskSt->execute([(int)$job['task_id']]);
        $task = $taskSt->fetch();
        if (!$task) {
            $pdo->prepare('DELETE FROM retarget_card_update_jobs WHERE id=?')->execute([(int)$job['id']]);
            continue;
        }
        if (update_task_cards($pdo, $config, $task, !empty($job['expanded']))) {
            $pdo->prepare('DELETE FROM retarget_card_update_jobs WHERE id=? AND locked_at IS NOT NULL')
                ->execute([(int)$job['id']]);
            $processed++;
            continue;
        }
        $attempts = (int)$job['attempts'] + 1;
        $delay = min(300, 5 * (2 ** min(6, $attempts - 1)));
        $retry = $pdo->prepare(
            'UPDATE retarget_card_update_jobs SET attempts=?,available_at=DATE_ADD(NOW(),INTERVAL ? SECOND),'
            . 'locked_at=NULL,last_error=? WHERE id=? AND locked_at IS NOT NULL'
        );
        $retry->execute([$attempts, $delay, 'Feishu card update failed; see server error log', (int)$job['id']]);
        $failed++;
    }
    return ['processed' => $processed, 'failed' => $failed];
}

function fetch_task_by_uid(PDO $pdo, string $taskUid, ?int $userId = null): ?array
{
    $sql = 'SELECT * FROM retarget_tasks WHERE task_uid=?';
    $params = [$taskUid];
    if ($userId !== null) {
        $sql .= ' AND user_id=?';
        $params[] = $userId;
    }
    $sql .= ' LIMIT 1';
    $st = $pdo->prepare($sql);
    $st->execute($params);
    $row = $st->fetch();
    return $row ?: null;
}

function expire_old_tasks(PDO $pdo, ?int $userId = null): void
{
    $where = "((status IN ('pending','approved_queued','claimed') AND expires_at<=NOW()) "
        . "OR (status='executing' AND expires_at<=NOW() AND (lease_expires_at IS NULL OR lease_expires_at<=NOW())))";
    $params = [];
    if ($userId !== null) {
        $where .= ' AND user_id=?';
        $params[] = $userId;
    }
    $pdo->prepare("UPDATE retarget_tasks SET status='expired',active_dedupe_key=NULL,claim_token=NULL,lease_expires_at=NULL,finished_at=NOW(),result_message='追投卡片已超过30分钟有效期' WHERE $where")->execute($params);
}

function verify_feishu_callback_signature(array $config, string $raw): void
{
    $f = feishu_config($config);
    $unsigned = json_decode($raw, true);
    if (
        is_array($unsigned)
        && empty($unsigned['encrypt'])
        && (($unsigned['type'] ?? '') === 'url_verification' || isset($unsigned['challenge']))
    ) {
        $expectedToken = (string)($f['verification_token'] ?? '');
        $providedToken = (string)($unsigned['token'] ?? '');
        if ($expectedToken !== '' && hash_equals($expectedToken, $providedToken)) {
            return;
        }
    }
    $timestamp = (string)($_SERVER['HTTP_X_LARK_REQUEST_TIMESTAMP'] ?? '');
    $nonce = (string)($_SERVER['HTTP_X_LARK_REQUEST_NONCE'] ?? '');
    $signature = (string)($_SERVER['HTTP_X_LARK_SIGNATURE'] ?? '');
    $isNewCardCallback = is_array($unsigned)
        && (array_key_exists('encrypt', $unsigned) || array_key_exists('schema', $unsigned));
    $algorithm = $isNewCardCallback ? 'sha256' : 'sha1';
    $secret = (string)(
        $isNewCardCallback
            ? ($f['encrypt_key'] ?? '')
            : ($f['verification_token'] ?? '')
    );
    $signatureMatches = false;
    if ($timestamp !== '' && $nonce !== '' && $signature !== '' && $secret !== '') {
        $expected = hash($algorithm, $timestamp . $nonce . $secret . $raw);
        $signatureMatches = hash_equals($expected, $signature);
    }
    if ((string)getenv('QCSCKP_SERVER_TEST_MODE') === '1') {
        $normalizedTimestamp = ctype_digit($timestamp)
            ? (strlen($timestamp) >= 13 ? intdiv((int)$timestamp, 1000) : (int)$timestamp)
            : 0;
        error_log('Feishu action signature check: ' . json_encode([
            'scheme' => $isNewCardCallback ? 'new_sha256_encrypt_key' : 'legacy_sha1_verification_token',
            'top_keys' => is_array($unsigned) ? array_keys($unsigned) : [],
            'has_timestamp' => $timestamp !== '',
            'timestamp_digits' => ctype_digit($timestamp),
            'timestamp_length' => strlen($timestamp),
            'timestamp_age_seconds' => $normalizedTimestamp > 0 ? abs(time() - $normalizedTimestamp) : null,
            'has_nonce' => $nonce !== '',
            'has_signature' => $signature !== '',
            'secret_configured' => $secret !== '',
            'signature_match' => $signatureMatches,
        ]));
    }
    if ($secret === '') {
        api_json(['code' => 503, 'msg' => '回调验签参数未配置'], 503);
    }
    if ($timestamp === '' || $nonce === '' || $signature === '') {
        api_json(['code' => 403, 'msg' => '回调签名缺失'], 403);
    }
    if (strlen($timestamp) > 256 || strlen($nonce) > 256) {
        api_json(['code' => 403, 'msg' => '回调签名字段无效'], 403);
    }
    $expectedSignatureLength = $algorithm === 'sha256' ? 64 : 40;
    if (
        strlen($signature) !== $expectedSignatureLength
        || !ctype_xdigit($signature)
    ) {
        api_json(['code' => 403, 'msg' => '回调签名格式无效'], 403);
    }
    if ((string)getenv('QCSCKP_SERVER_TEST_MODE') !== '1' && ctype_digit($timestamp)) {
        // 飞书官方 SDK 将 timestamp 当作参与签名的原始字符串，并不要求它一定是纯数字。
        // 对标准 Unix 时间戳额外做 5 分钟新鲜度校验；对平台传来的非数字格式则依赖
        // SHA-256/SHA-1 验签、HTTPS、任务 nonce 和任务状态机共同防止伪造及重复执行。
        $requestTime = strlen($timestamp) >= 13 ? intdiv((int)$timestamp, 1000) : (int)$timestamp;
        if (abs(time() - $requestTime) > 300) {
            api_json(['code' => 403, 'msg' => '回调时间戳已失效'], 403);
        }
    }
    if (!$signatureMatches) {
        api_json(['code' => 403, 'msg' => '回调签名无效'], 403);
    }
}

function decrypt_feishu_payload(array $config, array $input): array
{
    if (empty($input['encrypt'])) return $input;
    $keyText = (string)(feishu_config($config)['encrypt_key'] ?? '');
    if ($keyText === '') api_json(['code' => 400, 'msg' => '服务器未配置 Encrypt Key'], 400);
    $key = hash('sha256', $keyText, true);
    $cipher = base64_decode((string)$input['encrypt'], true);
    if ($cipher === false) api_json(['code' => 400, 'msg' => '回调密文格式无效'], 400);
    if (strlen($cipher) <= 16) api_json(['code' => 400, 'msg' => 'Encrypted callback is too short'], 400);
    $iv = substr($cipher, 0, 16);
    $plain = openssl_decrypt(substr($cipher, 16), 'AES-256-CBC', $key, OPENSSL_RAW_DATA, $iv);
    $decoded = is_string($plain) ? json_decode($plain, true) : null;
    if (!is_array($decoded)) api_json(['code' => 400, 'msg' => '回调解密失败'], 400);
    return $decoded;
}

function feishu_operator_open_id(array $input): string
{
    return trim((string)(
        $input['event']['operator']['operator_id']['open_id']
        ?? $input['event']['operator']['open_id']
        ?? $input['operator']['operator_id']['open_id']
        ?? $input['operator']['open_id']
        ?? $input['open_id']
        ?? ''
    ));
}

function feishu_action_value(array $input): array
{
    $value = $input['event']['action']['value'] ?? $input['action']['value'] ?? [];
    return is_array($value) ? $value : [];
}
