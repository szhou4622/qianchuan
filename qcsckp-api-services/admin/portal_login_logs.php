<?php
declare(strict_types=1);

require_once dirname(__DIR__) . '/includes/bootstrap.php';
require_once dirname(__DIR__) . '/includes/layout.php';

$me = require_panel_login($pdo);
$GLOBALS['layout_user'] = $me;
$isSuper = $me['role'] === 'super_admin';

$page = max(1, (int) ($_GET['page'] ?? 1));
$perPage = 50;
$offset = ($page - 1) * $perPage;

$q = trim((string) ($_GET['q'] ?? ''));
$qLike = '';
if ($q !== '') {
    $qLike = '%' . str_replace(['%', '_'], ['\\%', '\\_'], $q) . '%';
}

try {
    $pdo->query('SELECT 1 FROM portal_api_login_log LIMIT 1');
} catch (Throwable $e) {
    layout_header('看盘登录日志', 'portal_login');
    echo '<div class="rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-900">';
    echo '尚未创建数据表。请在数据库中执行 <code class="rounded bg-white px-1">sql/portal_api_login_log.sql</code> 后再访问本页。';
    echo '</div>';
    layout_footer();
    exit;
}

$where = [];
$params = [];

if (!$isSuper) {
    $agentId = (int) $me['id'];
    $where[] = '(
        (l.account_id IS NOT NULL AND EXISTS (SELECT 1 FROM accounts u WHERE u.id = l.account_id AND u.role = \'user\' AND u.parent_id = ?))
        OR (l.account_id IS NULL AND EXISTS (SELECT 1 FROM accounts u WHERE u.username = l.username AND u.role = \'user\' AND u.parent_id = ?))
    )';
    $params[] = $agentId;
    $params[] = $agentId;
}

if ($qLike !== '') {
    $where[] = 'l.username LIKE ?';
    $params[] = $qLike;
}

$whereSql = $where === [] ? '1=1' : implode(' AND ', $where);

$countSt = $pdo->prepare("SELECT COUNT(*) FROM portal_api_login_log l WHERE {$whereSql}");
$countSt->execute($params);
$total = (int) $countSt->fetchColumn();
$totalPages = max(1, (int) ceil($total / $perPage));
if ($page > $totalPages) {
    $page = $totalPages;
    $offset = ($page - 1) * $perPage;
}

$sql = "SELECT l.id, l.account_id, l.username, l.result_code, l.login_success, l.parent_id,
               l.valid_from_snapshot, l.valid_until_snapshot, l.account_disabled_snapshot,
               l.client_ip, l.forwarded_for, l.http_via, l.created_at,
               ag.username AS agent_username
        FROM portal_api_login_log l
        LEFT JOIN accounts ag ON ag.id = l.parent_id AND ag.role = 'agent'
        WHERE {$whereSql}
        ORDER BY l.id DESC
        LIMIT {$perPage} OFFSET {$offset}";

$st = $pdo->prepare($sql);
$st->execute($params);
$rows = $st->fetchAll(PDO::FETCH_ASSOC);

$codeLabels = [
    'agent_disabled' => '代理已禁用',
    'success' => '校验成功',
];

layout_header('看盘登录日志', 'portal_login');
?>
<p class="text-sm text-slate-600 mb-4">
  仅记录看盘用户在 <code class="rounded bg-slate-100 px-1.5 py-0.5 text-xs">POST /api/account.php</code> 上<strong class="font-medium">密码校验通过</strong>后的登录（失败尝试不落库）。超级管理员查看全部；代理仅查看名下普通用户。
</p>

<form method="get" class="mb-4 flex flex-wrap items-end gap-3">
  <input type="hidden" name="page" value="1">
  <label class="block">
    <span class="text-xs text-slate-500">用户名筛选</span>
    <input type="search" name="q" value="<?= htmlspecialchars($q, ENT_QUOTES, 'UTF-8') ?>" placeholder="模糊匹配"
           class="mt-1 block rounded-lg border border-slate-200 px-3 py-2 text-sm w-56 max-w-full">
  </label>
  <button type="submit" class="rounded-lg bg-slate-900 text-white px-4 py-2 text-sm hover:bg-slate-800">查询</button>
  <?php if ($q !== ''): ?>
    <a href="/admin/portal_login_logs.php" class="text-sm text-slate-500 hover:text-slate-800">清除筛选</a>
  <?php endif; ?>
</form>

<div class="overflow-x-auto rounded-xl border border-slate-200 bg-white shadow-sm">
  <table class="min-w-full text-left text-sm">
    <thead class="bg-slate-50 text-slate-600 text-xs uppercase tracking-wide">
      <tr>
        <th class="px-3 py-2 whitespace-nowrap">时间</th>
        <th class="px-3 py-2 whitespace-nowrap">结果</th>
        <th class="px-3 py-2 whitespace-nowrap">用户名</th>
        <th class="px-3 py-2 whitespace-nowrap">用户ID</th>
        <th class="px-3 py-2 whitespace-nowrap">代理</th>
        <th class="px-3 py-2 whitespace-nowrap">有效期快照</th>
        <th class="px-3 py-2 whitespace-nowrap">禁用</th>
        <th class="px-3 py-2 whitespace-nowrap">IP</th>
      </tr>
    </thead>
    <tbody class="divide-y divide-slate-100">
      <?php if ($rows === []): ?>
        <tr><td colspan="8" class="px-3 py-8 text-center text-slate-500">暂无记录</td></tr>
      <?php else: ?>
        <?php foreach ($rows as $r): ?>
          <tr class="hover:bg-slate-50/80 align-top">
            <td class="px-3 py-2 whitespace-nowrap text-slate-700"><?= htmlspecialchars((string) $r['created_at'], ENT_QUOTES, 'UTF-8') ?></td>
            <td class="px-3 py-2">
              <?php
                $code = (string) $r['result_code'];
                $label = $codeLabels[$code] ?? $code;
                $ok = (int) $r['login_success'] === 1;
              ?>
              <span class="inline-flex flex-col gap-0.5">
                <span class="rounded px-2 py-0.5 text-xs font-medium <?= $ok ? 'bg-emerald-100 text-emerald-800' : 'bg-rose-50 text-rose-700' ?>">
                  <?= $ok ? 'success' : 'fail' ?>
                </span>
                <span class="text-xs text-slate-600" title="<?= htmlspecialchars($code, ENT_QUOTES, 'UTF-8') ?>"><?= htmlspecialchars($label, ENT_QUOTES, 'UTF-8') ?></span>
              </span>
            </td>
            <td class="px-3 py-2 font-mono text-xs"><?= htmlspecialchars((string) $r['username'], ENT_QUOTES, 'UTF-8') ?></td>
            <td class="px-3 py-2 font-mono text-xs"><?= $r['account_id'] !== null ? (int) $r['account_id'] : '—' ?></td>
            <td class="px-3 py-2 text-xs">
              <?php if ($r['parent_id'] !== null): ?>
                <span class="text-slate-500">#<?= (int) $r['parent_id'] ?></span>
                <?php if (!empty($r['agent_username'])): ?>
                  <span class="text-slate-700"><?= htmlspecialchars((string) $r['agent_username'], ENT_QUOTES, 'UTF-8') ?></span>
                <?php endif; ?>
              <?php else: ?>
                —
              <?php endif; ?>
            </td>
            <td class="px-3 py-2 text-xs text-slate-600 max-w-[14rem]">
              <?php if ($r['valid_from_snapshot'] !== null || $r['valid_until_snapshot'] !== null): ?>
                <div>起 <?= htmlspecialchars((string) ($r['valid_from_snapshot'] ?? '—'), ENT_QUOTES, 'UTF-8') ?></div>
                <div>止 <?= htmlspecialchars((string) ($r['valid_until_snapshot'] ?? '—'), ENT_QUOTES, 'UTF-8') ?></div>
              <?php else: ?>
                —
              <?php endif; ?>
            </td>
            <td class="px-3 py-2 text-xs"><?= $r['account_disabled_snapshot'] !== null ? ((int) $r['account_disabled_snapshot'] === 1 ? '是' : '否') : '—' ?></td>
            <td class="px-3 py-2 text-xs font-mono whitespace-nowrap">
              <?= htmlspecialchars((string) $r['client_ip'], ENT_QUOTES, 'UTF-8') ?>
              <?php if (!empty($r['forwarded_for'])): ?>
                <div class="text-[10px] text-slate-400 max-w-[10rem] truncate" title="<?= htmlspecialchars((string) $r['forwarded_for'], ENT_QUOTES, 'UTF-8') ?>">XFF: <?= htmlspecialchars((string) $r['forwarded_for'], ENT_QUOTES, 'UTF-8') ?></div>
              <?php endif; ?>
              <?php if (!empty($r['http_via'])): ?>
                <div class="text-[10px] text-slate-400 max-w-[12rem] break-all" title="<?= htmlspecialchars((string) $r['http_via'], ENT_QUOTES, 'UTF-8') ?>">Via: <?= htmlspecialchars((string) $r['http_via'], ENT_QUOTES, 'UTF-8') ?></div>
              <?php endif; ?>
            </td>
          </tr>
        <?php endforeach; ?>
      <?php endif; ?>
    </tbody>
  </table>
</div>

<?php if ($total > 0): ?>
  <div class="mt-4 flex flex-wrap items-center justify-between gap-2 text-sm text-slate-600">
    <span>共 <?= $total ?> 条，第 <?= $page ?> / <?= $totalPages ?> 页</span>
    <div class="flex gap-2">
      <?php
        $base = '/admin/portal_login_logs.php';
        $qparam = $q !== '' ? '&q=' . rawurlencode($q) : '';
      ?>
      <?php if ($page > 1): ?>
        <a class="rounded border border-slate-200 px-3 py-1 hover:bg-white" href="<?= $base ?>?page=<?= $page - 1 ?><?= $qparam ?>">上一页</a>
      <?php endif; ?>
      <?php if ($page < $totalPages): ?>
        <a class="rounded border border-slate-200 px-3 py-1 hover:bg-white" href="<?= $base ?>?page=<?= $page + 1 ?><?= $qparam ?>">下一页</a>
      <?php endif; ?>
    </div>
  </div>
<?php endif; ?>

<?php
layout_footer();
