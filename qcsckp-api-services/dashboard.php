<?php
declare(strict_types=1);

require_once __DIR__ . '/includes/bootstrap.php';
require_once __DIR__ . '/includes/portal_auth.php';
require_once __DIR__ . '/includes/dashboard_data.php';

portal_require_login($pdo);
$aadvid = portal_selected_aadvid();
$userId = portal_uid();
if ($aadvid === null || $aadvid === '' || $userId === null) {
    header('Location: /aadvid.php');
    exit;
}
if (!portal_user_has_aadvid($pdo, $userId, $aadvid)) {
    unset($_SESSION['portal_aadvid']);
    header('Location: /aadvid.php');
    exit;
}

header('Content-Type: text/html; charset=utf-8');

$aadvids = dashboard_distinct_aadvids($pdo, $userId);
if (!in_array($aadvid, $aadvids, true)) {
    $aadvids[] = $aadvid;
    sort($aadvids, SORT_STRING);
}
$pageInit = [
    'currentAadvid' => $aadvid,
    'aadvids' => $aadvids,
    'csrf' => csrf_token(),
];
$html = file_get_contents(__DIR__ . '/includes/dashboard_view.html');
$inject = '<script>window.__DASHBOARD_PAGE_INIT__=' . json_encode($pageInit, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES) . ';</script>' . "\n";
if (strpos($html, '<!-- DASHBOARD_PAGE_INIT -->') !== false) {
    $html = str_replace('<!-- DASHBOARD_PAGE_INIT -->', $inject, $html);
} else {
    $html = preg_replace('/<body\b[^>]*>/', '$0' . "\n" . $inject, $html, 1);
}
echo $html;
