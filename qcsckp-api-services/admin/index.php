<?php
declare(strict_types=1);

require_once dirname(__DIR__) . '/includes/bootstrap.php';
require_once dirname(__DIR__) . '/includes/layout.php';

$me = require_panel_login($pdo);
$GLOBALS['layout_user'] = $me;

$counts = ['agents' => 0, 'users' => 0];
if ($me['role'] === 'super_admin') {
    $counts['agents'] = (int) $pdo->query("SELECT COUNT(*) FROM accounts WHERE role = 'agent'")->fetchColumn();
    $counts['users'] = (int) $pdo->query("SELECT COUNT(*) FROM accounts WHERE role = 'user'")->fetchColumn();
} else {
    $st = $pdo->prepare("SELECT COUNT(*) FROM accounts WHERE role = 'user' AND parent_id = ?");
    $st->execute([(int) $me['id']]);
    $counts['users'] = (int) $st->fetchColumn();
}

layout_header('控制台', '');
$m = flash_get();
if ($m) {
    echo '<div class="mb-4 text-sm text-emerald-700 bg-emerald-50 border border-emerald-200 rounded-lg px-4 py-3">' . htmlspecialchars($m, ENT_QUOTES, 'UTF-8') . '</div>';
}
?>
<div class="grid gap-6 <?= $me['role'] === 'super_admin' ? 'md:grid-cols-2 lg:grid-cols-3' : 'md:grid-cols-1 max-w-xl' ?>">
  <?php if ($me['role'] === 'super_admin'): ?>
    <a href="/admin/agents.php" class="block rounded-xl border border-slate-200 bg-white p-6 shadow-sm hover:border-amber-300 transition">
      <h2 class="text-lg font-semibold text-slate-800">代理账户</h2>
      <p class="text-3xl font-bold text-slate-900 mt-2"><?= $counts['agents'] ?></p>
      <p class="text-sm text-slate-500 mt-1">管理代理与禁用状态</p>
    </a>
    <a href="/admin/desktop_release.php" class="block rounded-xl border border-slate-200 bg-white p-6 shadow-sm hover:border-amber-300 transition">
      <h2 class="text-lg font-semibold text-slate-800">桌面版本</h2>
      <p class="text-3xl font-bold text-slate-900 mt-2">发布</p>
      <p class="text-sm text-slate-500 mt-1">安装/更新包（zip、exe、dmg）与版本号</p>
    </a>
  <?php endif; ?>
  <a href="/admin/users.php" class="block rounded-xl border border-slate-200 bg-white p-6 shadow-sm hover:border-amber-300 transition">
    <h2 class="text-lg font-semibold text-slate-800">普通用户</h2>
    <p class="text-3xl font-bold text-slate-900 mt-2"><?= $counts['users'] ?></p>
    <p class="text-sm text-slate-500 mt-1">有效期与禁用管理</p>
  </a>
  <a href="/admin/portal_login_logs.php" class="block rounded-xl border border-slate-200 bg-white p-6 shadow-sm hover:border-amber-300 transition">
    <h2 class="text-lg font-semibold text-slate-800">看盘登录日志</h2>
    <p class="text-3xl font-bold text-slate-900 mt-2">审计</p>
    <p class="text-sm text-slate-500 mt-1">看盘用户密码校验成功后的记录</p>
  </a>
</div>
<?php if ($me['role'] === 'super_admin'): ?>
  <p class="mt-8 text-sm text-slate-600">桌面端：<code class="bg-slate-100 px-1 rounded">POST /api/account.php</code> 账号校验；Windows 更新检测 <code class="bg-slate-100 px-1 rounded">GET/POST /api/version.php</code>；macOS 更新检测 <code class="bg-slate-100 px-1 rounded">GET/POST /api/version_mac.php</code>（详见 <a href="/doc/" class="text-amber-700 hover:underline font-medium">/doc/</a> 或仓库 <code class="bg-slate-100 px-1 rounded">doc/</code> 目录）。</p>
<?php endif; ?>
<?php
layout_footer();
