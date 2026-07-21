<?php
declare(strict_types=1);

function layout_header(string $title, string $active = ''): void
{
    ?>
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title><?= htmlspecialchars($title, ENT_QUOTES, 'UTF-8') ?></title>
  <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-slate-50 text-slate-900 min-h-screen">
  <nav class="bg-slate-900 text-white shadow">
    <div class="max-w-6xl mx-auto px-4 py-3 flex flex-wrap items-center gap-4 justify-between">
      <a href="/admin/index.php" class="font-semibold tracking-tight">账号管理后台</a>
      <div class="flex flex-wrap gap-4 text-sm items-center">
        <?php if (!empty($GLOBALS['layout_user'])): ?>
          <span class="text-slate-300"><?= htmlspecialchars($GLOBALS['layout_user']['username'], ENT_QUOTES, 'UTF-8') ?>
            <span class="text-slate-500">（<?= $GLOBALS['layout_user']['role'] === 'super_admin' ? '超级管理员' : '代理' ?>）</span>
          </span>
          <?php if ($GLOBALS['layout_user']['role'] === 'super_admin'): ?>
            <a href="/admin/agents.php" class="<?= $active === 'agents' ? 'text-amber-300' : 'hover:text-amber-200' ?>">代理账户</a>
            <a href="/admin/desktop_release.php" class="<?= $active === 'desktop' ? 'text-amber-300' : 'hover:text-amber-200' ?>">桌面版本</a>
          <?php endif; ?>
          <a href="/admin/users.php" class="<?= $active === 'users' ? 'text-amber-300' : 'hover:text-amber-200' ?>">普通用户</a>
          <a href="/admin/portal_login_logs.php" class="<?= $active === 'portal_login' ? 'text-amber-300' : 'hover:text-amber-200' ?>">看盘登录日志</a>
          <a href="/admin/logout.php" class="text-slate-400 hover:text-white">退出</a>
        <?php endif; ?>
      </div>
    </div>
  </nav>
  <main class="max-w-6xl mx-auto px-4 py-8">
<?php
}

function layout_footer(): void
{
    ?>
  </main>
</body>
</html>
<?php
}

function flash_get(): ?string
{
    if (empty($_SESSION['_flash'])) {
        return null;
    }
    $m = $_SESSION['_flash'];
    unset($_SESSION['_flash']);
    return $m;
}

function flash_set(string $message): void
{
    $_SESSION['_flash'] = $message;
}
