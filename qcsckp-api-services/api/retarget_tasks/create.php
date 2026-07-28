<?php
declare(strict_types=1);

require_once dirname(__DIR__, 2) . '/includes/bootstrap.php';
require_once dirname(__DIR__, 2) . '/includes/retarget_task_common.php';

if ($_SERVER['REQUEST_METHOD'] !== 'POST') api_json(['success' => false, 'message' => '请使用 POST'], 405);
$user = authenticate_device($pdo);
$input = api_json_input();
$aavid = mb_substr(trim((string)($input['aavid'] ?? '')), 0, 64);
$accountName = mb_substr(trim((string)($input['account_name'] ?? '')), 0, 200);
$adId = mb_substr(trim((string)($input['ad_id'] ?? '')), 0, 64);
$targetUid = mb_substr(trim((string)($input['target_uid'] ?? '')), 0, 64);
$planName = mb_substr(trim((string)($input['plan_name'] ?? '')), 0, 256);
$promotionScene = mb_substr(trim((string)($input['promotion_scene'] ?? 'live')), 0, 32);
$planSystem = mb_substr(trim((string)($input['plan_system'] ?? 'unknown')), 0, 32);
$triggerLevel = mb_substr(trim((string)($input['trigger_level'] ?? 'material')), 0, 32);
$productId = mb_substr(trim((string)($input['product_id'] ?? '')), 0, 128);
$productName = mb_substr(trim((string)($input['product_name'] ?? '')), 0, 512);
$materialId = mb_substr(trim((string)($input['material_id'] ?? '')), 0, 128);
$strategyId = mb_substr(trim((string)($input['strategy_id'] ?? '')), 0, 128);
$strategyName = mb_substr(trim((string)($input['strategy_name'] ?? '')), 0, 128);
$materialName = mb_substr(trim((string)($input['material_name'] ?? '')), 0, 512);
$strategyHash = trim((string)($input['strategy_hash'] ?? ''));
if (
    $aavid === '' || $adId === '' || $targetUid === '' || $materialId === ''
    || $strategyId === '' || !in_array($promotionScene, ['live', 'product'], true)
    || !in_array($planSystem, ['global', 'chengfang'], true)
    || !in_array($triggerLevel, ['material', 'product'], true)
    || ($triggerLevel === 'product' && $productId === '')
    || !preg_match('/^[a-f0-9]{64}$/', $strategyHash)
) {
    api_json(['success' => false, 'message' => '账户、广告、素材、策略或策略版本参数不完整'], 400);
}
expire_old_tasks($pdo, (int)$user['id']);
$dedupe = hash('sha256', implode('|', [(string)$user['id'], $targetUid, $materialId, $strategyId]));
$taskUid = uuid_v4();
$nonce = bin2hex(random_bytes(32));
$json = static function ($v): string {
    return json_encode(is_array($v) ? $v : [], JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
};
try {
    $st = $pdo->prepare(
        "INSERT INTO retarget_tasks(task_uid,user_id,active_dedupe_key,aavid,account_name,ad_id,target_uid,plan_name,promotion_scene,plan_system,trigger_level,product_id,product_name,material_id,material_name,strategy_id,strategy_name,strategy_hash,status,action_nonce,trigger_snapshot_json,query_snapshot_json,retargeting_json,rule_snapshot_json,expires_at) "
        . "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',?,?,?,?,?,DATE_ADD(NOW(),INTERVAL 30 MINUTE))"
    );
    $st->execute([
        $taskUid, (int)$user['id'], $dedupe, $aavid, $accountName, $adId,
        $targetUid, $planName, $promotionScene, $planSystem, $triggerLevel, $productId, $productName,
        $materialId, $materialName,
        $strategyId, $strategyName, $strategyHash, $nonce,
        $json($input['trigger_snapshot'] ?? []), $json($input['query_snapshot'] ?? []),
        $json($input['retargeting'] ?? []), $json($input['rule_snapshot'] ?? []),
    ]);
} catch (PDOException $e) {
    if ((string)$e->getCode() !== '23000') throw $e;
    $dup = $pdo->prepare('SELECT * FROM retarget_tasks WHERE user_id=? AND active_dedupe_key=? LIMIT 1');
    $dup->execute([(int)$user['id'], $dedupe]);
    $existing = $dup->fetch();
    if ($existing) {
        api_json(['success' => true, 'duplicate' => true, 'data' => ['task_uid' => $existing['task_uid'], 'status' => $existing['status'], 'expires_at' => $existing['expires_at']]]);
    }
    throw $e;
}
$task = fetch_task_by_uid($pdo, $taskUid, (int)$user['id']);
try {
    $sent = send_task_cards($pdo, $config, $task);
} catch (Throwable $e) {
    $pdo->prepare("UPDATE retarget_tasks SET status='failed',active_dedupe_key=NULL,finished_at=NOW(),result_message=? WHERE task_uid=?")
        ->execute(['飞书卡片发送失败：' . mb_substr($e->getMessage(), 0, 1000), $taskUid]);
    $failedTask = fetch_task_by_uid($pdo, $taskUid, (int)$user['id']);
    if ($failedTask) enqueue_task_card_update($pdo, (int)$failedTask['id']);
    api_json(['success' => false, 'message' => '飞书卡片发送失败：' . $e->getMessage(), 'task_uid' => $taskUid], 502);
}
api_json(['success' => true, 'duplicate' => false, 'data' => ['task_uid' => $taskUid, 'status' => 'pending', 'expires_at' => $task['expires_at'], 'sent_count' => $sent]]);
