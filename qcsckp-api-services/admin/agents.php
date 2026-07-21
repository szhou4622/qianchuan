<?php
declare(strict_types=1);

require_once dirname(__DIR__) . '/includes/bootstrap.php';
require_once dirname(__DIR__) . '/includes/layout.php';

$me = require_super_admin($pdo);
$GLOBALS['layout_user'] = $me;

if ($_SERVER['REQUEST_METHOD'] === 'POST') {
    if (!csrf_verify($_POST['_csrf'] ?? null)) {
        flash_set('验证失败，请重试。');
        header('Location: /admin/agents.php');
        exit;
    }
    $action = (string) ($_POST['action'] ?? '');
    if ($action === 'create') {
        $username = trim((string) ($_POST['username'] ?? ''));
        $password = (string) ($_POST['password'] ?? '');
        if ($username === '' || strlen($password) < 6) {
            flash_set('用户名不能为空，且密码至少 6 位。');
        } else {
            $chk = $pdo->prepare('SELECT id FROM accounts WHERE username = ?');
            $chk->execute([$username]);
            if ($chk->fetch()) {
                flash_set('用户名已存在。');
            } else {
                $hash = password_hash($password, PASSWORD_DEFAULT);
                $ins = $pdo->prepare(
                    'INSERT INTO accounts (username, password_hash, role, parent_id, valid_from, valid_until, is_disabled)
                     VALUES (?, ?, \'agent\', NULL, NULL, NULL, 0)'
                );
                $ins->execute([$username, $hash]);
                flash_set('已创建代理账户。');
            }
        }
    } elseif ($action === 'toggle_disable') {
        $id = (int) ($_POST['id'] ?? 0);
        $st = $pdo->prepare("SELECT id, is_disabled FROM accounts WHERE id = ? AND role = 'agent'");
        $st->execute([$id]);
        $row = $st->fetch();
        if ($row) {
            $new = (int) $row['is_disabled'] === 1 ? 0 : 1;
            $pdo->prepare('UPDATE accounts SET is_disabled = ? WHERE id = ?')->execute([$new, $id]);
            flash_set($new ? '已禁用该代理。' : '已启用该代理。');
        }
    }
    header('Location: /admin/agents.php');
    exit;
}

$rows = $pdo->query(
    "SELECT a.id, a.username, a.is_disabled, a.created_at,
            (SELECT COUNT(*) FROM accounts u WHERE u.parent_id = a.id AND u.role = 'user') AS user_count
     FROM accounts a
     WHERE a.role = 'agent'
     ORDER BY a.id DESC"
)->fetchAll();

layout_header('代理账户', 'agents');
$m = flash_get();
if ($m) {
    echo '<div class="mb-4 text-sm text-emerald-700 bg-emerald-50 border border-emerald-200 rounded-lg px-4 py-3">' . htmlspecialchars($m, ENT_QUOTES, 'UTF-8') . '</div>';
}
?>
<div class="bg-white rounded-xl border border-slate-200 shadow-sm p-6 mb-8">
  <h2 class="text-lg font-semibold mb-4">新建代理</h2>
  <form method="post" class="grid gap-4 md:grid-cols-3 md:items-end">
    <input type="hidden" name="_csrf" value="<?= htmlspecialchars(csrf_token(), ENT_QUOTES, 'UTF-8') ?>">
    <input type="hidden" name="action" value="create">
    <div>
      <label class="block text-sm text-slate-600 mb-1">用户名</label>
      <input name="username" required class="w-full border border-slate-300 rounded-lg px-3 py-2 text-sm">
    </div>
    <div>
      <label class="block text-sm text-slate-600 mb-1">密码</label>
      <input name="password" type="password" required minlength="6" class="w-full border border-slate-300 rounded-lg px-3 py-2 text-sm">
    </div>
    <div>
      <button type="submit" class="w-full md:w-auto bg-amber-600 hover:bg-amber-700 text-white text-sm font-medium px-4 py-2 rounded-lg">创建</button>
    </div>
  </form>
</div>

<div class="bg-white rounded-xl border border-slate-200 shadow-sm overflow-hidden">
  <table class="min-w-full text-sm">
    <thead class="bg-slate-100 text-slate-700">
      <tr>
        <th class="text-left px-4 py-3 font-medium">ID</th>
        <th class="text-left px-4 py-3 font-medium">用户名</th>
        <th class="text-left px-4 py-3 font-medium">下属用户数</th>
        <th class="text-left px-4 py-3 font-medium">状态</th>
        <th class="text-left px-4 py-3 font-medium">创建时间</th>
        <th class="text-right px-4 py-3 font-medium">操作</th>
      </tr>
    </thead>
    <tbody class="divide-y divide-slate-100">
      <?php foreach ($rows as $r): ?>
        <tr class="hover:bg-slate-50">
          <td class="px-4 py-3"><?= (int) $r['id'] ?></td>
          <td class="px-4 py-3 font-medium"><?= htmlspecialchars((string) $r['username'], ENT_QUOTES, 'UTF-8') ?></td>
          <td class="px-4 py-3"><?= (int) $r['user_count'] ?></td>
          <td class="px-4 py-3">
            <?php if ((int) $r['is_disabled'] === 1): ?>
              <span class="text-red-600">已禁用</span>
            <?php else: ?>
              <span class="text-emerald-600">正常</span>
            <?php endif; ?>
          </td>
          <td class="px-4 py-3 text-slate-500"><?= htmlspecialchars((string) $r['created_at'], ENT_QUOTES, 'UTF-8') ?></td>
          <td class="px-4 py-3 text-right">
            <form method="post" class="inline" onsubmit="return confirm('确定要切换该代理的禁用状态吗？');">
              <input type="hidden" name="_csrf" value="<?= htmlspecialchars(csrf_token(), ENT_QUOTES, 'UTF-8') ?>">
              <input type="hidden" name="action" value="toggle_disable">
              <input type="hidden" name="id" value="<?= (int) $r['id'] ?>">
              <button type="submit" class="text-amber-700 hover:underline"><?= (int) $r['is_disabled'] === 1 ? '启用' : '禁用' ?></button>
            </form>
          </td>
        </tr>
      <?php endforeach; ?>
      <?php if (!$rows): ?>
        <tr><td colspan="6" class="px-4 py-8 text-center text-slate-500">暂无代理账户</td></tr>
      <?php endif; ?>
    </tbody>
  </table>
</div>
<?php
layout_footer();
