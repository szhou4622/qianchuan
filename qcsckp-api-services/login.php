<?php
declare(strict_types=1);

require_once __DIR__ . '/includes/bootstrap.php';
require_once __DIR__ . '/includes/portal_auth.php';

if (portal_user($pdo)) {
    if (portal_selected_aadvid() !== null) {
        header('Location: /dashboard.php');
    } else {
        header('Location: /aadvid.php');
    }
    exit;
}

$error = '';
if ($_SERVER['REQUEST_METHOD'] === 'POST') {
    if (!csrf_verify($_POST['_csrf'] ?? null)) {
        $error = '会话已过期，请刷新后重试。';
    } else {
        $username = trim((string) ($_POST['username'] ?? ''));
        $password = (string) ($_POST['password'] ?? '');
        $row = portal_try_login($pdo, $username, $password);
        if ($row) {
            session_regenerate_id(true);
            $_SESSION['portal_uid'] = (int) $row['id'];
            unset($_SESSION['portal_aadvid']);
            header('Location: /aadvid.php');
            exit;
        }
        $error = '账号或密码错误，或账号已过期/被禁用。';
    }
}
?>
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>登录 — 千川素材看盘工具</title>
  <style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body {
      min-height: 100vh; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: #f8f9fa; color: #111; display: flex; align-items: center; justify-content: center; padding: 24px;
    }
    .card {
      width: 100%; max-width: 400px; background: #fff; border: 1px solid rgba(0,0,0,0.08);
      border-radius: 12px; padding: 32px; box-shadow: 0 12px 40px rgba(0,0,0,0.04);
    }
    h1 { font-size: 18px; font-weight: 600; margin-bottom: 8px; text-align: center; }
    p.sub { font-size: 13px; color: #666; text-align: center; margin-bottom: 24px; }
    label { display: block; font-size: 13px; color: #444; margin-bottom: 6px; }
    input {
      width: 100%; border: 1px solid #ddd; border-radius: 8px; padding: 10px 12px; font-size: 14px; margin-bottom: 16px;
    }
    input:focus { outline: none; border-color: #000; }
    button {
      width: 100%; background: #000; color: #fff; border: none; border-radius: 8px; padding: 12px;
      font-size: 14px; font-weight: 600; cursor: pointer;
    }
    button:hover { opacity: 0.92; }
    .err { font-size: 13px; color: #b91c1c; background: #fef2f2; border: 1px solid #fecaca; border-radius: 8px; padding: 10px; margin-bottom: 16px; }
    .back { display: block; text-align: center; margin-top: 20px; font-size: 13px; color: #666; text-decoration: none; }
    .back:hover { color: #000; }
  </style>
</head>
<body>
  <div class="card">
    <h1>登录</h1>
    <?php if ($error !== ''): ?>
      <div class="err"><?= htmlspecialchars($error, ENT_QUOTES, 'UTF-8') ?></div>
    <?php endif; ?>
    <form method="post">
      <input type="hidden" name="_csrf" value="<?= htmlspecialchars(csrf_token(), ENT_QUOTES, 'UTF-8') ?>">
      <label for="u">用户名</label>
      <input id="u" name="username" required autocomplete="username" value="">
      <label for="p">密码</label>
      <input id="p" name="password" type="password" required autocomplete="current-password">
      <button type="submit">登录</button>
    </form>
    <a class="back" href="/">返回首页</a>
  </div>
</body>
</html>
