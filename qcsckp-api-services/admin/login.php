<?php
declare(strict_types=1);

require_once dirname(__DIR__) . '/includes/bootstrap.php';

if (current_user($pdo)) {
    header('Location: /admin/index.php');
    exit;
}

$error = '';
if ($_SERVER['REQUEST_METHOD'] === 'POST') {
    if (!csrf_verify($_POST['_csrf'] ?? null)) {
        $error = '会话已过期，请刷新后重试。';
    } else {
        $username = trim((string) ($_POST['username'] ?? ''));
        $password = (string) ($_POST['password'] ?? '');
        $row = login_user($pdo, $username, $password);
        if ($row) {
            session_regenerate_id(true);
            $_SESSION['user_id'] = (int) $row['id'];
            header('Location: /admin/index.php');
            exit;
        }
        $error = '账号或密码错误，或无权登录后台。';
    }
}
?>
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>后台登录</title>
  <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-slate-100 min-h-screen flex items-center justify-center p-4">
  <div class="w-full max-w-md bg-white rounded-xl shadow-lg p-8 border border-slate-200">
    <h1 class="text-xl font-bold text-slate-800 mb-6 text-center">账号管理后台</h1>
    <?php if ($error): ?>
      <div class="mb-4 text-sm text-red-600 bg-red-50 border border-red-100 rounded-lg px-3 py-2"><?= htmlspecialchars($error, ENT_QUOTES, 'UTF-8') ?></div>
    <?php endif; ?>
    <form method="post" class="space-y-4">
      <input type="hidden" name="_csrf" value="<?= htmlspecialchars(csrf_token(), ENT_QUOTES, 'UTF-8') ?>">
      <div>
        <label class="block text-sm font-medium text-slate-700 mb-1">用户名</label>
        <input name="username" required autocomplete="username" class="w-full border border-slate-300 rounded-lg px-3 py-2 focus:ring-2 focus:ring-amber-500 focus:border-amber-500 outline-none">
      </div>
      <div>
        <label class="block text-sm font-medium text-slate-700 mb-1">密码</label>
        <input name="password" type="password" required autocomplete="current-password" class="w-full border border-slate-300 rounded-lg px-3 py-2 focus:ring-2 focus:ring-amber-500 focus:border-amber-500 outline-none">
      </div>
      <button type="submit" class="w-full bg-slate-900 hover:bg-slate-800 text-white font-medium py-2.5 rounded-lg transition">登录</button>
    </form>
    <p class="mt-6 text-xs text-slate-500 text-center">仅限超级管理员与代理登录</p>
  </div>
</body>
</html>
