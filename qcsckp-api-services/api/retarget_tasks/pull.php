<?php
declare(strict_types=1);

require_once dirname(__DIR__, 2) . '/includes/bootstrap.php';
require_once dirname(__DIR__, 2) . '/includes/retarget_task_common.php';

if ($_SERVER['REQUEST_METHOD'] !== 'GET') api_json(['success' => false, 'message' => '请使用 GET'], 405);
$user = authenticate_device($pdo);
$uid = (int)$user['id'];
$device = mb_substr(trim((string)($user['device_name'] ?? '桌面端')), 0, 120);
$expiredSt = $pdo->prepare("SELECT task_uid FROM retarget_tasks WHERE user_id=? AND ((status IN ('pending','approved_queued','claimed') AND expires_at<=NOW()) OR (status='executing' AND expires_at<=NOW() AND (lease_expires_at IS NULL OR lease_expires_at<=NOW())))");
$expiredSt->execute([$uid]);
$expiredUids = $expiredSt->fetchAll();
expire_old_tasks($pdo, $uid);
foreach ($expiredUids as $expiredRow) {
    $expiredTask = fetch_task_by_uid($pdo, (string)$expiredRow['task_uid'], $uid);
    if ($expiredTask) enqueue_task_card_update($pdo, (int)$expiredTask['id']);
}
$staleExecuting = $pdo->prepare("SELECT task_uid FROM retarget_tasks WHERE user_id=? AND status='executing' AND lease_expires_at IS NOT NULL AND lease_expires_at<=NOW() AND expires_at>NOW()");
$staleExecuting->execute([$uid]);
$staleExecutingUids = $staleExecuting->fetchAll();
$pdo->prepare(
    "UPDATE retarget_tasks SET status='failed',active_dedupe_key=NULL,lease_expires_at=NULL,finished_at=NOW(),result_message='桌面工具执行中断，结果状态未知；为避免重复追投未自动重试' "
    . "WHERE user_id=? AND status='executing' AND lease_expires_at IS NOT NULL AND lease_expires_at<=NOW() AND expires_at>NOW()"
)->execute([$uid]);
foreach ($staleExecutingUids as $staleRow) {
    $failedTask = fetch_task_by_uid($pdo, (string)$staleRow['task_uid'], $uid);
    if ($failedTask) enqueue_task_card_update($pdo, (int)$failedTask['id']);
}
$pdo->prepare(
    "UPDATE retarget_tasks SET status='approved_queued',claimed_device=NULL,claim_token=NULL,lease_expires_at=NULL "
    . "WHERE user_id=? AND status='claimed' AND lease_expires_at IS NOT NULL AND lease_expires_at<=NOW() AND expires_at>NOW()"
)->execute([$uid]);
$pdo->beginTransaction();
try {
    $st = $pdo->prepare("SELECT * FROM retarget_tasks WHERE user_id=? AND status='approved_queued' AND expires_at>NOW() ORDER BY approved_at,id LIMIT 1 FOR UPDATE");
    $st->execute([$uid]);
    $task = $st->fetch();
    if (!$task) {
        $pdo->commit();
        api_json(['success' => true, 'data' => null]);
    }
    $claimToken = bin2hex(random_bytes(32));
    $pdo->prepare("UPDATE retarget_tasks SET status='claimed',claimed_device=?,claim_token=?,lease_expires_at=DATE_ADD(NOW(),INTERVAL 15 MINUTE) WHERE id=?")
        ->execute([$device, $claimToken, (int)$task['id']]);
    $pdo->commit();
} catch (Throwable $e) {
    if ($pdo->inTransaction()) $pdo->rollBack();
    throw $e;
}
$task = fetch_task_by_uid($pdo, (string)$task['task_uid'], $uid);
foreach (['materials_json','trigger_snapshot_json','query_snapshot_json','retargeting_json','rule_snapshot_json'] as $field) {
    $task[str_replace('_json', '', $field)] = safe_json_decode((string)($task[$field] ?? ''));
    unset($task[$field]);
}
unset($task['action_nonce'], $task['active_dedupe_key'], $task['user_id']);
api_json(['success' => true, 'data' => $task]);
