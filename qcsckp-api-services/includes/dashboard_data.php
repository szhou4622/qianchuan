<?php
declare(strict_types=1);

/**
 * 看板数据：与 cankao dashboard.py 逻辑对齐（MySQL + PHP 聚合）。
 */

function dashboard_parse_period(string $period): array
{
    $period = strtolower(trim($period));
    if (preg_match('/^(\d+)m$/', $period, $m)) {
        $n = max(1, min(720, (int) $m[1]));

        return ['minutes' => $n, 'label' => $n . '分钟'];
    }
    if (preg_match('/^(\d+)h$/', $period, $m)) {
        $n = max(1, min(168, (int) $m[1]));

        return ['hours' => $n, 'label' => $n . '小时'];
    }
    if ($period !== '' && ctype_digit($period)) {
        $n = max(1, min(168, (int) $period));

        return ['hours' => $n, 'label' => $n . '小时'];
    }

    return ['hours' => 1, 'label' => '1小时'];
}

function dashboard_mysql_time_expr(array $p): string
{
    if (isset($p['minutes'])) {
        return sprintf('DATE_SUB(NOW(), INTERVAL %d MINUTE)', (int) $p['minutes']);
    }
    if (isset($p['hours'])) {
        return sprintf('DATE_SUB(NOW(), INTERVAL %d HOUR)', (int) $p['hours']);
    }

    return 'DATE_SUB(NOW(), INTERVAL 1 HOUR)';
}

/** @param mixed $v */
function dashboard_rate_to_ratio($v): float
{
    $x = (float) $v;
    if ($x <= 0) {
        return 0.0;
    }
    if ($x > 1.0) {
        return $x / 100.0;
    }

    return $x;
}

function dashboard_table_has_column(PDO $pdo, string $table, string $column): bool
{
    $dbName = $pdo->query('SELECT DATABASE()')->fetchColumn();
    if (!is_string($dbName) || $dbName === '') {
        return false;
    }
    $st = $pdo->prepare(
        'SELECT COUNT(*) FROM information_schema.COLUMNS
         WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ? AND COLUMN_NAME = ?'
    );
    $st->execute([$dbName, $table, $column]);

    return (int) $st->fetchColumn() > 0;
}

/**
 * @param array<int, array<string,mixed>> $rows
 */
function dashboard_attach_estimated_ecpm(PDO $pdo, array &$rows, string $aadvid): void
{
    if ($rows === []) {
        return;
    }
    $totalGmv = 0.0;
    $totalOrders = 0;
    foreach ($rows as $r) {
        $totalGmv += (float) ($r['overallAmount'] ?? 0);
        $totalOrders += (int) ($r['overallOrderCount'] ?? 0);
    }
    $targetRoi = null;
    if (dashboard_table_has_column($pdo, 'pmc_ad_detail_basic', 'ecp_roi2_goal')) {
        $st = $pdo->prepare('SELECT ecp_roi2_goal FROM pmc_ad_detail_basic WHERE aadvid = ? LIMIT 1');
        $st->execute([$aadvid]);
        $raw = $st->fetchColumn();
        if ($raw !== false && $raw !== null) {
            $g = (float) $raw;
            if ($g > 0) {
                $targetRoi = $g;
            }
        }
    }
    $cpa = null;
    if ($totalOrders > 0 && $targetRoi !== null && $targetRoi > 0) {
        $cpa = ($totalGmv / (float) $totalOrders) / $targetRoi;
    }
    foreach ($rows as $i => $r) {
        if ($cpa === null || $cpa <= 0) {
            $rows[$i]['estimatedEcpm'] = null;
            continue;
        }
        $ctr = dashboard_rate_to_ratio($r['overallCtr'] ?? 0);
        $cvr = dashboard_rate_to_ratio($r['overallConversionRate'] ?? 0);
        $rows[$i]['estimatedEcpm'] = round($cvr * $ctr * $cpa * 1000.0, 4);
    }
}

/**
 * @param array<int, array<string,mixed>> $rows
 */
function dashboard_sort_table_rows(array &$rows, string $sortBy, string $sortOrder): void
{
    $desc = strtolower($sortOrder) !== 'asc';
    $get = static function (array $r, string $sortBy): array {
        switch ($sortBy) {
            case 'currentCost':
                return [0, (float) ($r['currentCost'] ?? 0)];
            case 'costDiff':
                return [0, (float) ($r['costDiff'] ?? 0)];
            case 'overallPayRoi':
                return [0, (float) ($r['overallPayRoi'] ?? 0)];
            case 'overallAmount':
                return [0, (float) ($r['overallAmount'] ?? 0)];
            case 'netRoi':
                return [0, (float) ($r['netRoi'] ?? 0)];
            case 'netAmount':
                return [0, (float) ($r['netAmount'] ?? 0)];
            case 'estimatedEcpm':
                $e = $r['estimatedEcpm'] ?? null;
                if ($e === null) {
                    return [1, 0.0];
                }

                return [0, (float) $e];
            default:
                return [0, (float) ($r['costDiff'] ?? 0)];
        }
    };
    usort($rows, static function (array $a, array $b) use ($sortBy, $desc, $get): int {
        [$na, $va] = $get($a, $sortBy);
        [$nb, $vb] = $get($b, $sortBy);
        if ($na !== $nb) {
            return $na <=> $nb;
        }
        if ($va === $vb) {
            return strcmp((string) ($a['id'] ?? ''), (string) ($b['id'] ?? ''));
        }
        $cmp = $va <=> $vb;

        return $desc ? -$cmp : $cmp;
    });
}

/**
 * @return array{success:bool,data?:array<int,array>,total?:int,period?:string,page?:int,pageSize?:int,totalPages?:int,message?:string}
 */
function dashboard_get_table_data(
    PDO $pdo,
    int $userId,
    string $aadvid,
    string $period,
    string $sortBy,
    string $sortOrder,
    int $page,
    int $pageSize
): array {
    try {
        $p = dashboard_parse_period($period);
        $timeExpr = dashboard_mysql_time_expr($p);
        $sql = "SELECT * FROM pmc_promotion_material
                WHERE user_id = ? AND aadvid = ? AND created_at >= $timeExpr";
        $st = $pdo->prepare($sql);
        $st->execute([$userId, $aadvid]);
        $all = $st->fetchAll(PDO::FETCH_ASSOC);
        $groups = [];
        foreach ($all as $row) {
            $mid = (string) $row['material_id'];
            if (!isset($groups[$mid])) {
                $groups[$mid] = [];
            }
            $groups[$mid][] = $row;
        }
        $rows = [];
        foreach ($groups as $mid => $grows) {
            usort($grows, static function (array $a, array $b): int {
                return strcmp((string) $a['created_at'], (string) $b['created_at']);
            });
            $first = $grows[0];
            $last = $grows[count($grows) - 1];
            $sCost = (float) ($first['stat_cost'] ?? 0);
            $eCost = (float) ($last['stat_cost'] ?? 0);
            $diff = round($eCost - $sCost, 2);
            $eOsc = $last['order_settle_count_1h'];
            $eOsa = $last['order_settle_amount_1h'];
            $eOsr = $last['order_settle_rate_1h'];
            $ePpoc = $last['prepay_pay_order_count'];
            $eGmv = $last['pay_gmv_include_coupon'];
            $ePps = $last['prepay_pay_settle_1h'];
            $eRr = $last['refund_rate_1h'];
            $overallOrder = $last['overall_order_count'] ?? null;

            $rows[] = [
                'id' => $mid,
                'aadvid' => $last['aadvid'],
                'title' => $last['video_name'] ?? '未命名',
                'materialStatus' => $last['material_status'],
                'showStatus' => $last['show_status'],
                'videoType' => $last['video_type'],
                'videoId' => $last['video_id'],
                'awemeItemId' => $last['aweme_item_id'],
                'cover' => $last['cover_url'] ?? '',
                'duration' => $last['video_duration'],
                'createTime' => $last['video_create_time'],
                'currentCost' => (float) ($last['stat_cost'] ?? 0),
                'costDiff' => $diff,
                'netRoi' => (float) ($ePps ?? 0),
                'netAmount' => (float) ($eOsa ?? 0),
                'hourRefundRate' => (float) ($eRr ?? 0),
                'overallPayRoi' => (float) ($ePpoc ?? 0),
                'overallAmount' => (float) ($eGmv ?? 0),
                'netSettleRate' => (float) ($eOsr ?? 0),
                'netOrderCount' => (int) ($eOsc ?? 0),
                'overallOrderCount' => (int) ($overallOrder ?? 0),
                'overallShowCount' => (int) ($last['overall_show_count'] ?? 0),
                'overallClickCount' => (int) ($last['overall_click_count'] ?? 0),
                'overallCtr' => (float) ($last['overall_ctr'] ?? 0),
                'overallConversionRate' => (float) ($last['overall_conversion_rate'] ?? 0),
                'periodStartTime' => $first['created_at'],
                'periodEndTime' => $last['created_at'],
            ];
        }
        dashboard_attach_estimated_ecpm($pdo, $rows, $aadvid);
        dashboard_sort_table_rows($rows, $sortBy, $sortOrder);
        $total = count($rows);
        $pageSize = max(1, min(200, $pageSize));
        $page = max(1, $page);
        $totalPages = $total > 0 ? (int) ceil($total / $pageSize) : 1;
        $offset = ($page - 1) * $pageSize;
        $pageRows = array_slice($rows, $offset, $pageSize);
        $out = [];
        foreach ($pageRows as $r) {
            $out[] = [
                // 必须字符串，避免 json_encode 成 JSON 数字导致前端 Number 精度丢失
                'id' => (string) $r['id'],
                'aadvid' => $r['aadvid'],
                'title' => $r['title'],
                'materialStatus' => $r['materialStatus'],
                'showStatus' => $r['showStatus'],
                'videoType' => $r['videoType'],
                'videoId' => $r['videoId'],
                'awemeItemId' => $r['awemeItemId'],
                'cover' => $r['cover'],
                'duration' => $r['duration'],
                'createTime' => $r['createTime'],
                'currentCost' => $r['currentCost'],
                'costDiff' => $r['costDiff'],
                'netRoi' => $r['netRoi'],
                'netAmount' => $r['netAmount'],
                'hourRefundRate' => $r['hourRefundRate'],
                'overallPayRoi' => $r['overallPayRoi'],
                'overallAmount' => $r['overallAmount'],
                'netSettleRate' => $r['netSettleRate'],
                'netOrderCount' => $r['netOrderCount'],
                'overallOrderCount' => $r['overallOrderCount'],
                'overallShowCount' => $r['overallShowCount'],
                'overallClickCount' => $r['overallClickCount'],
                'overallCtr' => $r['overallCtr'],
                'overallConversionRate' => $r['overallConversionRate'],
                'estimatedEcpm' => $r['estimatedEcpm'],
                'periodStartTime' => $r['periodStartTime'],
                'periodEndTime' => $r['periodEndTime'],
            ];
        }

        return [
            'success' => true,
            'data' => $out,
            'total' => $total,
            'period' => $p['label'],
            'page' => $page,
            'pageSize' => $pageSize,
            'totalPages' => $totalPages,
        ];
    } catch (Throwable $e) {
        return ['success' => false, 'data' => [], 'total' => 0, 'message' => $e->getMessage()];
    }
}

/**
 * @return array{success:bool,data?:array,total?:int,message?:string}
 */
function dashboard_get_material_history(PDO $pdo, int $userId, string $aadvid, string $materialId, int $limit): array
{
    try {
        $limit = max(1, min(200, $limit));
        $tz = new DateTimeZone('Asia/Shanghai');
        $start = new DateTimeImmutable('today', $tz);
        $startStr = $start->format('Y-m-d H:i:s');
        $lim = (int) $limit;
        $sql = 'SELECT created_at, stat_cost, prepay_pay_order_count, pay_gmv_include_coupon
             FROM pmc_promotion_material
             WHERE user_id = ? AND aadvid = ? AND material_id = ? AND created_at > ?
             ORDER BY created_at ASC
             LIMIT ' . $lim;
        $st = $pdo->prepare($sql);
        $st->execute([$userId, $aadvid, $materialId, $startStr]);
        $dbRows = $st->fetchAll(PDO::FETCH_ASSOC);
        $result = [];
        foreach ($dbRows as $r) {
            $createdAt = $r['created_at'] ?? null;
            $tsMs = null;
            $tLabel = null;
            if ($createdAt) {
                try {
                    $dt = new DateTimeImmutable((string) $createdAt);
                    $tsMs = (int) ($dt->format('U') * 1000);
                    $tLabel = $dt->format('m-d H:i');
                } catch (Throwable $e) {
                    $tLabel = (string) $createdAt;
                }
            }
            $result[] = [
                'time' => $tLabel,
                'timestamp' => $tsMs,
                'cost' => (float) ($r['stat_cost'] ?? 0),
                'roi' => (float) ($r['prepay_pay_order_count'] ?? 0),
                'amount' => (float) ($r['pay_gmv_include_coupon'] ?? 0),
            ];
        }

        return ['success' => true, 'data' => $result, 'total' => count($result)];
    } catch (Throwable $e) {
        return ['success' => false, 'data' => [], 'message' => $e->getMessage()];
    }
}

/**
 * @return array{success:bool,data?:array<int,array>,total?:int,message?:string}
 */
function dashboard_get_top20_by_cost(PDO $pdo, int $userId, string $aadvid, int $hours): array
{
    try {
        $hours = max(1, min(168, $hours));
        $sql = "
            SELECT * FROM (
                SELECT t.*,
                    ROW_NUMBER() OVER (PARTITION BY material_id ORDER BY created_at DESC) AS rn
                FROM pmc_promotion_material t
                WHERE user_id = ? AND aadvid = ? AND created_at >= DATE_SUB(NOW(), INTERVAL {$hours} HOUR)
            ) x
            WHERE x.rn = 1
            ORDER BY x.stat_cost DESC
            LIMIT 20
        ";
        $st = $pdo->prepare($sql);
        $st->execute([$userId, $aadvid]);
        $dbRows = $st->fetchAll(PDO::FETCH_ASSOC);
        $data = [];
        foreach ($dbRows as $row) {
            $data[] = [
                'id' => (string) $row['material_id'],
                'aadvid' => $row['aadvid'],
                'title' => $row['video_name'] ?? '未命名',
                'materialStatus' => $row['material_status'],
                'showStatus' => $row['show_status'],
                'videoType' => $row['video_type'],
                'videoId' => $row['video_id'],
                'awemeItemId' => $row['aweme_item_id'],
                'cover' => $row['cover_url'] ?? '',
                'duration' => $row['video_duration'],
                'createTime' => $row['video_create_time'],
                'currentCost' => (float) ($row['stat_cost'] ?? 0),
                'createdAt' => $row['created_at'],
            ];
        }

        return ['success' => true, 'data' => $data, 'total' => count($data)];
    } catch (Throwable $e) {
        // MySQL 5.7 无窗口函数时回退 PHP
        return dashboard_get_top20_fallback($pdo, $userId, $aadvid, $hours);
    }
}

/**
 * @return array{success:bool,data?:array<int,array>,total?:int,message?:string}
 */
function dashboard_get_top20_fallback(PDO $pdo, int $userId, string $aadvid, int $hours): array
{
    try {
        $st = $pdo->prepare(
            'SELECT * FROM pmc_promotion_material
             WHERE user_id = ? AND aadvid = ? AND created_at >= DATE_SUB(NOW(), INTERVAL ? HOUR)
             ORDER BY material_id ASC, created_at ASC'
        );
        $st->execute([$userId, $aadvid, $hours]);
        $all = $st->fetchAll(PDO::FETCH_ASSOC);
        $latest = [];
        foreach ($all as $row) {
            $mid = (string) $row['material_id'];
            $latest[$mid] = $row;
        }
        $list = array_values($latest);
        usort($list, static function (array $a, array $b): int {
            return ((float) ($b['stat_cost'] ?? 0)) <=> ((float) ($a['stat_cost'] ?? 0));
        });
        $list = array_slice($list, 0, 20);
        $data = [];
        foreach ($list as $row) {
            $data[] = [
                'id' => (string) $row['material_id'],
                'aadvid' => $row['aadvid'],
                'title' => $row['video_name'] ?? '未命名',
                'materialStatus' => $row['material_status'],
                'showStatus' => $row['show_status'],
                'videoType' => $row['video_type'],
                'videoId' => $row['video_id'],
                'awemeItemId' => $row['aweme_item_id'],
                'cover' => $row['cover_url'] ?? '',
                'duration' => $row['video_duration'],
                'createTime' => $row['video_create_time'],
                'currentCost' => (float) ($row['stat_cost'] ?? 0),
                'createdAt' => $row['created_at'],
            ];
        }

        return ['success' => true, 'data' => $data, 'total' => count($data)];
    } catch (Throwable $e) {
        return ['success' => false, 'data' => [], 'message' => $e->getMessage()];
    }
}

/**
 * @return array{success:bool,totalCost?:float,rowCount?:int,batchMinuteKey?:null,latestCreatedAt?:?string,message?:string}
 */
function dashboard_get_latest_crawl_cost_sum(PDO $pdo, int $userId, string $aadvid, int $hours): array
{
    try {
        $hours = max(1, min(168, $hours));
        $sql = "
            SELECT
                COALESCE(SUM(COALESCE(stat_cost, 0)), 0) AS total_cost,
                COUNT(*) AS row_count,
                MAX(created_at) AS latest_created_at
            FROM (
                SELECT material_id, stat_cost, created_at,
                    ROW_NUMBER() OVER (PARTITION BY material_id ORDER BY created_at DESC) AS rn
                FROM pmc_promotion_material
                WHERE user_id = ? AND aadvid = ? AND created_at >= DATE_SUB(NOW(), INTERVAL {$hours} HOUR)
            ) t
            WHERE t.rn = 1
        ";
        $st = $pdo->prepare($sql);
        $st->execute([$userId, $aadvid]);
        $row = $st->fetch(PDO::FETCH_ASSOC);
        if (!$row) {
            return [
                'success' => true,
                'totalCost' => 0.0,
                'rowCount' => 0,
                'batchMinuteKey' => null,
                'latestCreatedAt' => null,
            ];
        }

        return [
            'success' => true,
            'totalCost' => round((float) ($row['total_cost'] ?? 0), 2),
            'rowCount' => (int) ($row['row_count'] ?? 0),
            'batchMinuteKey' => null,
            'latestCreatedAt' => $row['latest_created_at'] !== null ? (string) $row['latest_created_at'] : null,
        ];
    } catch (Throwable $e) {
        return dashboard_get_latest_crawl_cost_sum_fallback($pdo, $userId, $aadvid, $hours);
    }
}

/**
 * @return array{success:bool,totalCost?:float,rowCount?:int,batchMinuteKey?:null,latestCreatedAt?:?string,message?:string}
 */
function dashboard_get_latest_crawl_cost_sum_fallback(PDO $pdo, int $userId, string $aadvid, int $hours): array
{
    try {
        $st = $pdo->prepare(
            'SELECT * FROM pmc_promotion_material
             WHERE user_id = ? AND aadvid = ? AND created_at >= DATE_SUB(NOW(), INTERVAL ? HOUR)'
        );
        $st->execute([$userId, $aadvid, $hours]);
        $all = $st->fetchAll(PDO::FETCH_ASSOC);
        $latest = [];
        $maxCa = null;
        foreach ($all as $row) {
            $mid = (string) $row['material_id'];
            $latest[$mid] = $row;
        }
        $sum = 0.0;
        foreach ($latest as $row) {
            $sum += (float) ($row['stat_cost'] ?? 0);
            $ca = $row['created_at'] ?? null;
            if ($ca !== null && ($maxCa === null || (string) $ca > (string) $maxCa)) {
                $maxCa = $ca;
            }
        }

        return [
            'success' => true,
            'totalCost' => round($sum, 2),
            'rowCount' => count($latest),
            'batchMinuteKey' => null,
            'latestCreatedAt' => $maxCa !== null ? (string) $maxCa : null,
        ];
    } catch (Throwable $e) {
        return [
            'success' => false,
            'totalCost' => 0.0,
            'rowCount' => 0,
            'batchMinuteKey' => null,
            'latestCreatedAt' => null,
            'message' => $e->getMessage(),
        ];
    }
}

/**
 * @return array<int, string>
 */
function dashboard_distinct_aadvids(PDO $pdo, int $userId): array
{
    $st = $pdo->prepare(
        'SELECT DISTINCT aadvid FROM pmc_promotion_material WHERE user_id = ? ORDER BY aadvid ASC'
    );
    $st->execute([$userId]);
    $rows = $st->fetchAll(PDO::FETCH_COLUMN);

    return array_map('strval', $rows);
}

function portal_user_has_aadvid(PDO $pdo, int $userId, string $aadvid): bool
{
    $st = $pdo->prepare(
        'SELECT 1 FROM pmc_promotion_material WHERE user_id = ? AND aadvid = ? LIMIT 1'
    );
    $st->execute([$userId, $aadvid]);

    return (bool) $st->fetchColumn();
}
