<?php
declare(strict_types=1);

require_once dirname(__DIR__, 2) . '/includes/bootstrap.php';
require_once dirname(__DIR__, 2) . '/includes/retarget_task_common.php';

function respond_card_action(array $envelope, array $task, string $toast, string $toastType = 'success', bool $expanded = false): void
{
    $card = task_card($task, '', $expanded);
    if (!empty($envelope['encrypt']) || array_key_exists('schema', $envelope)) {
        api_json([
            'toast' => ['type' => $toastType, 'content' => $toast],
            'card' => ['type' => 'raw', 'data' => $card],
        ]);
    }
    api_json($card);
}

if ($_SERVER['REQUEST_METHOD'] !== 'POST') api_json(['code' => 405, 'msg' => '请使用 POST'], 405);
ensure_retarget_task_schema($pdo);
$feishu = feishu_config($config);
if (empty($feishu['verification_token']) && empty($feishu['encrypt_key'])) {
    api_json(['code' => 503, 'msg' => '飞书回调校验参数未配置'], 503);
}
$raw = (string)file_get_contents('php://input');
$parsed = json_decode($raw, true);
if (!is_array($parsed)) api_json(['code' => 400, 'msg' => 'JSON无效'], 400);
$input = decrypt_feishu_payload($config, $parsed);
if (($input['type'] ?? '') === 'url_verification' || isset($input['challenge'])) {
    $token = (string)($input['token'] ?? '');
    $expected = (string)(feishu_config($config)['verification_token'] ?? '');
    if ((string)getenv('QCSCKP_SERVER_TEST_MODE') === '1') {
        error_log('Feishu challenge handled: ' . json_encode([
            'encrypted' => !empty($parsed['encrypt']),
            'token_present' => $token !== '',
            'token_match' => $expected === '' || ($token !== '' && hash_equals($expected, $token)),
            'has_signature' => !empty($_SERVER['HTTP_X_LARK_SIGNATURE']),
        ]));
    }
    if ($expected !== '' && !hash_equals($expected, $token)) api_json(['code' => 403, 'msg' => 'Verification Token无效'], 403);
    api_json(['challenge' => (string)($input['challenge'] ?? '')]);
}
verify_feishu_callback_signature($config, $raw);
$expectedToken = (string)(feishu_config($config)['verification_token'] ?? '');
$callbackToken = (string)($input['token'] ?? $input['header']['token'] ?? '');
$operator = feishu_operator_open_id($input);
$authorized = trim((string)(feishu_config($config)['authorized_open_id'] ?? ''));
if ((string)getenv('QCSCKP_SERVER_TEST_MODE') === '1') {
    $event = is_array($input['event'] ?? null) ? $input['event'] : [];
    $actionData = is_array($event['action'] ?? null)
        ? $event['action']
        : (is_array($input['action'] ?? null) ? $input['action'] : []);
    $actionValue = is_array($actionData['value'] ?? null) ? $actionData['value'] : [];
    error_log('Feishu action payload check: ' . json_encode([
        'encrypted' => !empty($parsed['encrypt']),
        'decrypted_top_keys' => array_keys($input),
        'token_present' => $callbackToken !== '',
        'token_match' => $expectedToken === '' || ($callbackToken !== '' && hash_equals($expectedToken, $callbackToken)),
        'operator_present' => $operator !== '',
        'operator_match' => $authorized !== '' && $operator !== '' && hash_equals($authorized, $operator),
        'action' => (string)($actionValue['action'] ?? ''),
        'task_uid_suffix' => substr((string)($actionValue['task_uid'] ?? ''), -8),
    ], JSON_UNESCAPED_SLASHES));
}
if ($authorized === '' || $operator === '' || !hash_equals($authorized, $operator)) {
    api_json(['toast' => ['type' => 'error', 'content' => '你没有追投审批权限']]);
}
$value = feishu_action_value($input);
$taskUid = trim((string)($value['task_uid'] ?? ''));
$nonce = trim((string)($value['nonce'] ?? ''));
$action = trim((string)($value['action'] ?? ''));
$task = fetch_task_by_uid($pdo, $taskUid);
if (!$task || $nonce === '' || !hash_equals((string)$task['action_nonce'], $nonce)) {
    api_json(['toast' => ['type' => 'error', 'content' => '提醒任务无效或已被篡改']]);
}
expire_old_tasks($pdo);
$task = fetch_task_by_uid($pdo, $taskUid);
if ($action === 'view') {
    respond_card_action($parsed, $task, '已展开完整触发条件和追投参数', 'info', true);
}
if ($task['status'] !== 'pending') {
    enqueue_task_card_update($pdo, (int)$task['id']);
    respond_card_action($parsed, $task, '该提醒已处理：' . $task['status'], 'info');
}
if ($action === 'approve') {
    $st = $pdo->prepare("UPDATE retarget_tasks SET status='approved_queued',clicker_open_id=?,approved_at=NOW() WHERE id=? AND status='pending' AND expires_at>NOW()");
    $st->execute([$operator, (int)$task['id']]);
    $toast = '已批准，等待桌面工具执行';
} elseif ($action === 'reject') {
    $st = $pdo->prepare("UPDATE retarget_tasks SET status='rejected',active_dedupe_key=NULL,clicker_open_id=?,finished_at=NOW(),result_message='用户在飞书选择暂不追投' WHERE id=? AND status='pending'");
    $st->execute([$operator, (int)$task['id']]);
    $toast = '本次已暂不追投';
} else {
    api_json(['toast' => ['type' => 'error', 'content' => '不支持的卡片动作']]);
}
$task = fetch_task_by_uid($pdo, $taskUid);
enqueue_task_card_update($pdo, (int)$task['id']);
if ($st->rowCount() !== 1) {
    respond_card_action($parsed, $task, '该提醒已由其他点击处理：' . $task['status'], 'info');
}
respond_card_action($parsed, $task, $toast);
