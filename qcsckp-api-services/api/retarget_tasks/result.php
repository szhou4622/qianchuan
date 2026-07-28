<?php
declare(strict_types=1);

require_once dirname(__DIR__, 2) . '/includes/bootstrap.php';
require_once dirname(__DIR__, 2) . '/includes/retarget_task_common.php';

if ($_SERVER['REQUEST_METHOD'] !== 'POST') api_json(['success' => false, 'message' => '请使用 POST'], 405);
$user = authenticate_device($pdo);
$input = api_json_input();
$taskUid = trim((string)($input['task_uid'] ?? ''));
$status = trim((string)($input['status'] ?? ''));
$claimToken = trim((string)($input['claim_token'] ?? ''));
if ($taskUid === '' || !in_array($status, ['executing','succeeded','failed'], true) || !preg_match('/^[a-f0-9]{64}$/', $claimToken)) {
    api_json(['success' => false, 'message' => '任务ID或状态无效'], 400);
}
$task = fetch_task_by_uid($pdo, $taskUid, (int)$user['id']);
if (!$task) api_json(['success' => false, 'message' => '任务不存在'], 404);
if (!hash_equals((string)($task['claim_token'] ?? ''), $claimToken)) {
    api_json(['success' => false, 'message' => '任务租约已失效'], 409);
}
if (in_array($task['status'], RETARGET_TERMINAL_STATUSES, true)) {
    api_json(['success' => true, 'duplicate' => true, 'data' => ['task_uid' => $taskUid, 'status' => $task['status']]]);
}
if (!in_array($task['status'], ['claimed', 'executing'], true)) {
    api_json(['success' => false, 'message' => '任务尚未领取或租约状态无效'], 409);
}
$message = mb_substr(trim((string)($input['message'] ?? '')), 0, 2000);
$detail = mb_substr((string)($input['detail'] ?? ''), 0, 12000);
$regulateTaskId = mb_substr(trim((string)($input['regulate_task_id'] ?? '')), 0, 128);
$resultJson = json_encode(is_array($input['result'] ?? null) ? $input['result'] : [], JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
if ($status === 'executing') {
    $start = $pdo->prepare("UPDATE retarget_tasks SET status='executing',started_at=COALESCE(started_at,NOW()),lease_expires_at=DATE_ADD(NOW(),INTERVAL 15 MINUTE),result_message=? WHERE id=? AND claim_token=? AND (status='executing' OR (status='claimed' AND expires_at>NOW()))");
    $start->execute([$message ?: '桌面工具正在执行追投', (int)$task['id'], $claimToken]);
    if ($start->rowCount() !== 1) {
        expire_old_tasks($pdo, (int)$user['id']);
        $expiredTask = fetch_task_by_uid($pdo, $taskUid, (int)$user['id']);
        if ($expiredTask) enqueue_task_card_update($pdo, (int)$expiredTask['id']);
        api_json(['success' => false, 'message' => '任务已超过卡片有效期或租约状态已变化'], 409);
    }
} else {
    $finish = $pdo->prepare("UPDATE retarget_tasks SET status=?,active_dedupe_key=NULL,finished_at=NOW(),lease_expires_at=NULL,regulate_task_id=?,result_message=?,result_detail=?,result_json=? WHERE id=? AND claim_token=? AND status IN ('claimed','executing')");
    $finish->execute([$status, $regulateTaskId ?: null, $message, $detail, $resultJson, (int)$task['id'], $claimToken]);
    if ($finish->rowCount() !== 1) {
        $latest = fetch_task_by_uid($pdo, $taskUid, (int)$user['id']);
        if ($latest && in_array($latest['status'], RETARGET_TERMINAL_STATUSES, true)) {
            api_json(['success' => true, 'duplicate' => true, 'data' => ['task_uid' => $taskUid, 'status' => $latest['status']]]);
        }
        api_json(['success' => false, 'message' => '任务状态已变化，结果未写入'], 409);
    }
}
$task = fetch_task_by_uid($pdo, $taskUid, (int)$user['id']);
enqueue_task_card_update($pdo, (int)$task['id']);
api_json(['success' => true, 'data' => ['task_uid' => $taskUid, 'status' => $task['status']]]);
