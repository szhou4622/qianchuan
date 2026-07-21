<?php
declare(strict_types=1);

header('Content-Type: application/json; charset=utf-8');
header('Cache-Control: no-store, no-cache, must-revalidate');

require_once dirname(__DIR__) . '/includes/bootstrap.php';
require_once dirname(__DIR__) . '/includes/portal_auth.php';
require_once dirname(__DIR__) . '/includes/dashboard_data.php';

if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
    http_response_code(405);
    echo json_encode(['success' => false, 'message' => '请使用 POST'], JSON_UNESCAPED_UNICODE);
    exit;
}

$u = portal_user($pdo);
if (!$u) {
    http_response_code(401);
    echo json_encode(['success' => false, 'message' => '请先登录'], JSON_UNESCAPED_UNICODE);
    exit;
}

$aadvid = portal_selected_aadvid();
if ($aadvid === null || $aadvid === '') {
    http_response_code(400);
    echo json_encode(['success' => false, 'message' => '请先选择广告主'], JSON_UNESCAPED_UNICODE);
    exit;
}

$userId = (int) $u['id'];
if (!portal_user_has_aadvid($pdo, $userId, $aadvid)) {
    http_response_code(403);
    echo json_encode(['success' => false, 'message' => '无权访问该广告主'], JSON_UNESCAPED_UNICODE);
    exit;
}

$raw = file_get_contents('php://input');
$input = [];
if ($raw !== '' && $raw !== false) {
    // 超大整数（material_id 等）在 JSON 数字中会丢精度；解码为字符串以便与 varchar 列精确匹配
    $jsonFlags = JSON_BIGINT_AS_STRING;
    $j = json_decode($raw, true, 512, $jsonFlags);
    if (is_array($j)) {
        $input = $j;
    }
}

$action = (string) ($input['action'] ?? '');

switch ($action) {
    case 'table_data':
        $period = (string) ($input['period'] ?? '1h');
        $sortBy = (string) ($input['sortBy'] ?? 'costDiff');
        $sortOrder = (string) ($input['sortOrder'] ?? 'desc');
        $page = (int) ($input['page'] ?? 1);
        $pageSize = (int) ($input['pageSize'] ?? 50);
        $r = dashboard_get_table_data($pdo, $userId, $aadvid, $period, $sortBy, $sortOrder, $page, $pageSize);
        echo json_encode($r, JSON_UNESCAPED_UNICODE);
        break;

    case 'material_history':
        $midRaw = $input['materialId'] ?? '';
        $materialId = is_string($midRaw) ? trim($midRaw) : trim((string) $midRaw);
        $limit = (int) ($input['limit'] ?? 200);
        if ($materialId === '') {
            echo json_encode(['success' => false, 'data' => [], 'message' => '缺少 materialId'], JSON_UNESCAPED_UNICODE);
            break;
        }
        $r = dashboard_get_material_history($pdo, $userId, $aadvid, $materialId, $limit);
        echo json_encode($r, JSON_UNESCAPED_UNICODE);
        break;

    case 'top20':
        $hours = (int) ($input['hours'] ?? 1);
        $r = dashboard_get_top20_by_cost($pdo, $userId, $aadvid, $hours);
        echo json_encode($r, JSON_UNESCAPED_UNICODE);
        break;

    case 'cost_sum':
        $hours = (int) ($input['hours'] ?? 1);
        $r = dashboard_get_latest_crawl_cost_sum($pdo, $userId, $aadvid, $hours);
        echo json_encode($r, JSON_UNESCAPED_UNICODE);
        break;

    case 'account_label_get':
        $label = $_SESSION['portal_dashboard_label'] ?? '';
        echo json_encode(['success' => true, 'label' => is_string($label) ? $label : ''], JSON_UNESCAPED_UNICODE);
        break;

    case 'account_label_set':
        $label = trim((string) ($input['label'] ?? ''));
        if (strlen($label) > 200) {
            $label = substr($label, 0, 200);
        }
        $_SESSION['portal_dashboard_label'] = $label;
        echo json_encode(['success' => true, 'label' => $label], JSON_UNESCAPED_UNICODE);
        break;

    default:
        http_response_code(400);
        echo json_encode(['success' => false, 'message' => '未知 action'], JSON_UNESCAPED_UNICODE);
}
