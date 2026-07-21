<?php
declare(strict_types=1);

function dt_from_input(?string $s): ?string
{
    if ($s === null) {
        return null;
    }
    $s = trim($s);
    if ($s === '') {
        return null;
    }
    $s = str_replace('T', ' ', $s);
    if (preg_match('/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/', $s)) {
        $s .= ':00';
    }
    $t = strtotime($s);
    if ($t === false) {
        return null;
    }
    return date('Y-m-d H:i:s', $t);
}

function dt_for_input(?string $db): string
{
    if ($db === null || $db === '') {
        return '';
    }
    return str_replace(' ', 'T', substr($db, 0, 16));
}

/** 新建用户默认有效期：当前时刻起一年（用于 datetime-local） */
function default_create_validity_range(): array
{
    $now = time();
    $from = date('Y-m-d\TH:i', $now);
    $until = date('Y-m-d\TH:i', strtotime('+1 year', $now));
    return [$from, $until];
}
