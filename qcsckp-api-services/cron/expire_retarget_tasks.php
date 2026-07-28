<?php
declare(strict_types=1);

if (PHP_SAPI !== 'cli') {
    http_response_code(404);
    exit;
}

require_once dirname(__DIR__) . '/includes/bootstrap.php';
require_once dirname(__DIR__) . '/includes/retarget_task_common.php';

ensure_retarget_task_schema($pdo);
$st = $pdo->query("SELECT task_uid FROM retarget_tasks WHERE (status IN ('pending','approved_queued','claimed') AND expires_at<=NOW()) OR (status='executing' AND expires_at<=NOW() AND (lease_expires_at IS NULL OR lease_expires_at<=NOW()))");
$taskUids = $st ? $st->fetchAll() : [];
expire_old_tasks($pdo);
$updated = 0;
foreach ($taskUids as $row) {
    $task = fetch_task_by_uid($pdo, (string)$row['task_uid']);
    if (!$task) continue;
    update_task_cards($pdo, $config, $task);
    $updated++;
}
$queued = process_task_card_update_queue($pdo, $config, 50);
$pdo->exec("DELETE FROM desktop_device_sessions WHERE expires_at<DATE_SUB(NOW(),INTERVAL 30 DAY) OR revoked_at<DATE_SUB(NOW(),INTERVAL 30 DAY)");
fwrite(STDOUT, sprintf(
    "expired cards updated: %d; queued card updates: %d; queued failures: %d\n",
    $updated,
    (int)$queued['processed'],
    (int)$queued['failed']
));
