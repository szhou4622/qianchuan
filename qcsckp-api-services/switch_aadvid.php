<?php
declare(strict_types=1);

require_once __DIR__ . '/includes/bootstrap.php';
require_once __DIR__ . '/includes/portal_auth.php';
require_once __DIR__ . '/includes/dashboard_data.php';

header('Content-Type: application/json; charset=utf-8');

$me = portal_user($pdo);
if ($me === null) {
    http_response_code(401);
    echo json_encode(['success' => false, 'message' => '未登录'], JSON_UNESCAPED_UNICODE);
    exit;
}

$userId = (int) $me['id'];

$input = $_POST;
$contentType = $_SERVER['CONTENT_TYPE'] ?? '';
if (stripos($contentType, 'application/json') !== false) {
    $raw = file_get_contents('php://input');
    if ($raw !== false && $raw !== '') {
        $decoded = json_decode($raw, true);
        if (is_array($decoded)) {
            $input = $decoded;
        }
    }
}

$aadvid = trim((string) ($input['aadvid'] ?? ''));
$token = isset($input['_csrf']) ? (string) $input['_csrf'] : null;

if (!csrf_verify($token)) {
    http_response_code(403);
    echo json_encode(['success' => false, 'message' => '验证失败，请刷新页面后重试。'], JSON_UNESCAPED_UNICODE);
    exit;
}

if ($aadvid === '') {
    echo json_encode(['success' => false, 'message' => '请选择广告主。'], JSON_UNESCAPED_UNICODE);
    exit;
}

if (!portal_user_has_aadvid($pdo, $userId, $aadvid)) {
    http_response_code(403);
    echo json_encode(['success' => false, 'message' => '无效的广告主或暂无该账号的同步数据。'], JSON_UNESCAPED_UNICODE);
    exit;
}

$_SESSION['portal_aadvid'] = $aadvid;

echo json_encode(['success' => true, 'aadvid' => $aadvid], JSON_UNESCAPED_UNICODE);
