<?php
declare(strict_types=1);

require_once dirname(__DIR__) . '/includes/bootstrap.php';
require_once dirname(__DIR__) . '/includes/layout.php';
require_once dirname(__DIR__) . '/includes/helpers.php';

$me = require_panel_login($pdo);
$GLOBALS['layout_user'] = $me;
$isSuper = $me['role'] === 'super_admin';

function user_row_allowed(PDO $pdo, array $me, int $userId): ?array
{
    $st = $pdo->prepare(
        "SELECT u.*, a.username AS agent_username FROM accounts u
         LEFT JOIN accounts a ON u.parent_id = a.id
         WHERE u.id = ? AND u.role = 'user'"
    );
    $st->execute([$userId]);
    $row = $st->fetch();
    if (!$row) {
        return null;
    }
    if ($me['role'] === 'super_admin') {
        return $row;
    }
    if ((int) $row['parent_id'] === (int) $me['id']) {
        return $row;
    }
    return null;
}

if ($_SERVER['REQUEST_METHOD'] === 'POST') {
    if (!csrf_verify($_POST['_csrf'] ?? null)) {
        flash_set('验证失败，请重试。');
        header('Location: /admin/users.php');
        exit;
    }
    $action = (string) ($_POST['action'] ?? '');

    if ($action === 'create') {
        $username = trim((string) ($_POST['username'] ?? ''));
        $password = (string) ($_POST['password'] ?? '');
        $vf = dt_from_input($_POST['valid_from'] ?? null);
        $vu = dt_from_input($_POST['valid_until'] ?? null);
        $agentId = $isSuper ? (int) ($_POST['parent_id'] ?? 0) : (int) $me['id'];

        if ($username === '' || strlen($password) < 6) {
            flash_set('用户名不能为空，密码至少 6 位。');
        } elseif ($vf === null || $vu === null) {
            flash_set('请填写有效期开始与结束时间。');
        } elseif (strtotime($vu) < strtotime($vf)) {
            flash_set('结束时间不能早于开始时间。');
        } else {
            if ($isSuper) {
                $ag = $pdo->prepare("SELECT id FROM accounts WHERE id = ? AND role = 'agent' AND is_disabled = 0");
                $ag->execute([$agentId]);
                if (!$ag->fetch()) {
                    flash_set('请选择有效的代理账户。');
                    header('Location: /admin/users.php');
                    exit;
                }
            }
            $chk = $pdo->prepare('SELECT id FROM accounts WHERE username = ?');
            $chk->execute([$username]);
            if ($chk->fetch()) {
                flash_set('用户名已存在。');
            } else {
                $hash = password_hash($password, PASSWORD_DEFAULT);
                $ins = $pdo->prepare(
                    'INSERT INTO accounts (username, password_hash, role, parent_id, valid_from, valid_until, is_disabled)
                     VALUES (?, ?, \'user\', ?, ?, ?, 0)'
                );
                $ins->execute([$username, $hash, $agentId, $vf, $vu]);
                flash_set('已创建普通用户。');
            }
        }
    } elseif ($action === 'update') {
        $uid = (int) ($_POST['id'] ?? 0);
        $row = user_row_allowed($pdo, $me, $uid);
        if (!$row) {
            flash_set('无权操作该用户。');
        } else {
            $vf = dt_from_input($_POST['valid_from'] ?? null);
            $vu = dt_from_input($_POST['valid_until'] ?? null);
            $dis = isset($_POST['is_disabled']) ? 1 : 0;
            if ($vf === null || $vu === null) {
                flash_set('请填写有效期开始与结束时间。');
            } elseif (strtotime($vu) < strtotime($vf)) {
                flash_set('结束时间不能早于开始时间。');
            } else {
                $pdo->prepare(
                    'UPDATE accounts SET valid_from = ?, valid_until = ?, is_disabled = ? WHERE id = ? AND role = \'user\''
                )->execute([$vf, $vu, $dis, $uid]);
                flash_set('已保存。');
            }
        }
    }
    header('Location: /admin/users.php');
    exit;
}

if ($isSuper) {
    $rows = $pdo->query(
        "SELECT u.id, u.username, u.valid_from, u.valid_until, u.is_disabled, u.created_at,
                a.username AS agent_username, u.parent_id
         FROM accounts u
         LEFT JOIN accounts a ON u.parent_id = a.id
         WHERE u.role = 'user'
         ORDER BY u.id DESC"
    )->fetchAll();
    $agents = $pdo->query("SELECT id, username FROM accounts WHERE role = 'agent' ORDER BY id DESC")->fetchAll();
} else {
    $st = $pdo->prepare(
        "SELECT u.id, u.username, u.valid_from, u.valid_until, u.is_disabled, u.created_at,
                a.username AS agent_username, u.parent_id
         FROM accounts u
         LEFT JOIN accounts a ON u.parent_id = a.id
         WHERE u.role = 'user' AND u.parent_id = ?
         ORDER BY u.id DESC"
    );
    $st->execute([(int) $me['id']]);
    $rows = $st->fetchAll();
    $agents = [];
}

[$createDefaultFrom, $createDefaultUntil] = default_create_validity_range();

layout_header('普通用户', 'users');
$m = flash_get();
if ($m) {
    echo '<div class="mb-4 text-sm text-emerald-700 bg-emerald-50 border border-emerald-200 rounded-lg px-4 py-3">' . htmlspecialchars($m, ENT_QUOTES, 'UTF-8') . '</div>';
}
?>
<div class="bg-white rounded-xl border border-slate-200 shadow-sm p-6 mb-8">
  <h2 class="text-lg font-semibold mb-4">新建普通用户</h2>
  <form method="post" class="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
    <input type="hidden" name="_csrf" value="<?= htmlspecialchars(csrf_token(), ENT_QUOTES, 'UTF-8') ?>">
    <input type="hidden" name="action" value="create">
    <?php if ($isSuper): ?>
      <div class="md:col-span-1">
        <label class="block text-sm text-slate-600 mb-1">所属代理</label>
        <select name="parent_id" required class="w-full border border-slate-300 rounded-lg px-3 py-2 text-sm">
          <option value="">请选择</option>
          <?php foreach ($agents as $ag): ?>
            <option value="<?= (int) $ag['id'] ?>"><?= htmlspecialchars((string) $ag['username'], ENT_QUOTES, 'UTF-8') ?></option>
          <?php endforeach; ?>
        </select>
      </div>
    <?php endif; ?>
    <div>
      <label class="block text-sm text-slate-600 mb-1">用户名</label>
      <input name="username" required class="w-full border border-slate-300 rounded-lg px-3 py-2 text-sm">
    </div>
    <div>
      <label class="block text-sm text-slate-600 mb-1">密码</label>
      <input name="password" type="password" required minlength="6" class="w-full border border-slate-300 rounded-lg px-3 py-2 text-sm">
    </div>
    <div class="md:col-span-2 lg:col-span-3">
      <div class="flex flex-col xl:flex-row gap-4 xl:items-end">
        <div class="grid gap-3 sm:grid-cols-2 flex-1 min-w-0">
          <div>
            <label class="block text-sm text-slate-600 mb-1">开始时间</label>
            <input id="create_valid_from" name="valid_from" type="datetime-local" required
              value="<?= htmlspecialchars($createDefaultFrom, ENT_QUOTES, 'UTF-8') ?>"
              class="w-full border border-slate-300 rounded-lg px-3 py-2 text-sm">
          </div>
          <div>
            <label class="block text-sm text-slate-600 mb-1">结束时间</label>
            <input id="create_valid_until" name="valid_until" type="datetime-local" required
              value="<?= htmlspecialchars($createDefaultUntil, ENT_QUOTES, 'UTF-8') ?>"
              class="w-full border border-slate-300 rounded-lg px-3 py-2 text-sm">
          </div>
        </div>
        <div class="shrink-0 xl:pb-0.5">
          <p class="text-xs text-slate-500 mb-1.5">快捷选择（以当前时刻为开始，自动填入结束时间）</p>
          <div class="flex flex-wrap gap-1.5" id="create_validity_shortcuts">
            <button type="button" data-months="1" class="create-shortcut px-2.5 py-1 text-xs font-medium rounded-md border border-slate-300 bg-white hover:bg-amber-50 hover:border-amber-400 text-slate-700">1个月</button>
            <button type="button" data-months="2" class="create-shortcut px-2.5 py-1 text-xs font-medium rounded-md border border-slate-300 bg-white hover:bg-amber-50 hover:border-amber-400 text-slate-700">2个月</button>
            <button type="button" data-months="3" class="create-shortcut px-2.5 py-1 text-xs font-medium rounded-md border border-slate-300 bg-white hover:bg-amber-50 hover:border-amber-400 text-slate-700">3个月</button>
            <button type="button" data-months="6" class="create-shortcut px-2.5 py-1 text-xs font-medium rounded-md border border-slate-300 bg-white hover:bg-amber-50 hover:border-amber-400 text-slate-700">6个月</button>
            <button type="button" data-years="1" class="create-shortcut px-2.5 py-1 text-xs font-medium rounded-md border border-slate-300 bg-white hover:bg-amber-50 hover:border-amber-400 text-slate-700">1年</button>
            <button type="button" data-years="2" class="create-shortcut px-2.5 py-1 text-xs font-medium rounded-md border border-slate-300 bg-white hover:bg-amber-50 hover:border-amber-400 text-slate-700">2年</button>
          </div>
        </div>
      </div>
    </div>
    <div class="flex items-end md:col-span-2 lg:col-span-1">
      <button type="submit" class="bg-amber-600 hover:bg-amber-700 text-white text-sm font-medium px-6 py-2 rounded-lg">创建</button>
    </div>
  </form>
  <script>
  (function () {
    var fromEl = document.getElementById('create_valid_from');
    var untilEl = document.getElementById('create_valid_until');
    if (!fromEl || !untilEl) return;
    function pad(n) { return String(n).padStart(2, '0'); }
    function toDatetimeLocal(d) {
      return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + 'T' + pad(d.getHours()) + ':' + pad(d.getMinutes());
    }
    function addMonths(base, m) {
      var d = new Date(base.getTime());
      var day = d.getDate();
      d.setMonth(d.getMonth() + m);
      if (d.getDate() < day) d.setDate(0);
      return d;
    }
    function addYears(base, y) {
      var d = new Date(base.getTime());
      d.setFullYear(d.getFullYear() + y);
      return d;
    }
    var bar = document.getElementById('create_validity_shortcuts');
    if (!bar) return;
    bar.addEventListener('click', function (e) {
      var btn = e.target.closest('button.create-shortcut');
      if (!btn) return;
      var start = new Date();
      fromEl.value = toDatetimeLocal(start);
      var end;
      if (btn.hasAttribute('data-years')) {
        end = addYears(start, parseInt(btn.getAttribute('data-years'), 10) || 0);
      } else {
        end = addMonths(start, parseInt(btn.getAttribute('data-months'), 10) || 0);
      }
      untilEl.value = toDatetimeLocal(end);
    });
  })();
  </script>
  <?php if ($isSuper && !$agents): ?>
    <p class="mt-4 text-sm text-amber-800">请先在「代理账户」中创建至少一个代理。</p>
  <?php endif; ?>
</div>

<div class="bg-white rounded-xl border border-slate-200 shadow-sm overflow-hidden">
  <div class="px-4 py-3 bg-slate-50 border-b border-slate-200 font-medium text-slate-800">用户列表</div>
  <?php if (!$rows): ?>
    <p class="px-4 py-8 text-center text-slate-500">暂无普通用户</p>
  <?php else: ?>
    <div class="overflow-x-auto">
      <table class="w-full min-w-[56rem] text-sm border-collapse">
        <thead>
          <tr class="bg-slate-100/90 text-slate-700 border-b border-slate-200">
            <th class="text-left font-medium px-4 py-3 w-14 align-bottom">ID</th>
            <th class="text-left font-medium px-4 py-3 min-w-[6.5rem] align-bottom">用户名</th>
            <?php if ($isSuper): ?>
              <th class="text-left font-medium px-4 py-3 min-w-[7rem] align-bottom">所属代理</th>
            <?php endif; ?>
            <th class="text-left font-medium px-4 py-3 min-w-[11rem] align-bottom">开始时间</th>
            <th class="text-left font-medium px-4 py-3 min-w-[11rem] align-bottom">结束时间</th>
            <th class="text-center font-medium px-3 py-3 w-20 align-bottom">禁用</th>
            <th class="text-left font-medium px-4 py-3 min-w-[5.5rem] align-bottom">状态</th>
            <th class="text-right font-medium px-4 py-3 w-24 align-bottom">操作</th>
          </tr>
        </thead>
        <tbody class="divide-y divide-slate-100">
          <?php foreach ($rows as $r): ?>
            <?php
            $uid = (int) $r['id'];
            $fid = 'user-update-' . $uid;
            ?>
            <tr class="hover:bg-slate-50/90 align-middle">
              <td class="px-4 py-4 text-slate-500 tabular-nums">#<?= $uid ?></td>
              <td class="px-4 py-4 font-semibold text-slate-900"><?= htmlspecialchars((string) $r['username'], ENT_QUOTES, 'UTF-8') ?></td>
              <?php if ($isSuper): ?>
                <td class="px-4 py-4 text-slate-600"><?= htmlspecialchars((string) ($r['agent_username'] ?? '—'), ENT_QUOTES, 'UTF-8') ?></td>
              <?php endif; ?>
              <td class="px-4 py-4">
                <input form="<?= htmlspecialchars($fid, ENT_QUOTES, 'UTF-8') ?>"
                  name="valid_from" type="datetime-local" required
                  value="<?= htmlspecialchars(dt_for_input($r['valid_from'] ?? null), ENT_QUOTES, 'UTF-8') ?>"
                  class="w-full min-w-[10.5rem] max-w-[12rem] border border-slate-300 rounded-md px-2.5 py-2 text-sm shadow-sm focus:ring-2 focus:ring-amber-500/30 focus:border-amber-500 outline-none">
              </td>
              <td class="px-4 py-4">
                <input form="<?= htmlspecialchars($fid, ENT_QUOTES, 'UTF-8') ?>"
                  name="valid_until" type="datetime-local" required
                  value="<?= htmlspecialchars(dt_for_input($r['valid_until'] ?? null), ENT_QUOTES, 'UTF-8') ?>"
                  class="w-full min-w-[10.5rem] max-w-[12rem] border border-slate-300 rounded-md px-2.5 py-2 text-sm shadow-sm focus:ring-2 focus:ring-amber-500/30 focus:border-amber-500 outline-none">
              </td>
              <td class="px-3 py-4 text-center">
                <label class="inline-flex flex-col items-center gap-1 text-xs text-slate-600 cursor-pointer">
                  <input form="<?= htmlspecialchars($fid, ENT_QUOTES, 'UTF-8') ?>" type="checkbox" name="is_disabled" value="1" <?= (int) $r['is_disabled'] === 1 ? 'checked' : '' ?> class="rounded border-slate-300 w-4 h-4">
                  <span>禁用</span>
                </label>
              </td>
              <td class="px-4 py-4 whitespace-nowrap">
                <?php if ((int) $r['is_disabled'] === 1): ?>
                  <span class="inline-flex items-center rounded-full bg-red-50 px-2.5 py-0.5 text-xs font-medium text-red-700 ring-1 ring-inset ring-red-200">已禁用</span>
                <?php else: ?>
                  <span class="inline-flex items-center rounded-full bg-emerald-50 px-2.5 py-0.5 text-xs font-medium text-emerald-800 ring-1 ring-inset ring-emerald-200">正常</span>
                <?php endif; ?>
              </td>
              <td class="px-4 py-4 text-right">
                <button type="submit" form="<?= htmlspecialchars($fid, ENT_QUOTES, 'UTF-8') ?>"
                  class="inline-flex items-center justify-center bg-slate-900 hover:bg-slate-800 text-white text-sm font-medium px-4 py-2 rounded-lg shadow-sm min-w-[4.5rem]">保存</button>
              </td>
            </tr>
          <?php endforeach; ?>
        </tbody>
      </table>
    </div>
    <div class="hidden" aria-hidden="true">
      <?php foreach ($rows as $r): ?>
        <?php $fid = 'user-update-' . (int) $r['id']; ?>
        <form id="<?= htmlspecialchars($fid, ENT_QUOTES, 'UTF-8') ?>" method="post">
          <input type="hidden" name="_csrf" value="<?= htmlspecialchars(csrf_token(), ENT_QUOTES, 'UTF-8') ?>">
          <input type="hidden" name="action" value="update">
          <input type="hidden" name="id" value="<?= (int) $r['id'] ?>">
        </form>
      <?php endforeach; ?>
    </div>
  <?php endif; ?>
</div>
<?php
layout_footer();
