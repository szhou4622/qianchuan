<?php
declare(strict_types=1);

require_once __DIR__ . '/includes/bootstrap.php';
require_once __DIR__ . '/includes/portal_auth.php';
require_once __DIR__ . '/includes/dashboard_data.php';

$me = portal_require_login($pdo);
$userId = (int) $me['id'];

$error = '';
if ($_SERVER['REQUEST_METHOD'] === 'POST') {
    if (!csrf_verify($_POST['_csrf'] ?? null)) {
        $error = '验证失败，请重试。';
    } else {
        $aid = trim((string) ($_POST['aadvid'] ?? ''));
        if ($aid === '') {
            $error = '请选择广告主。';
        } elseif (!portal_user_has_aadvid($pdo, $userId, $aid)) {
            $error = '无效的广告主或暂无该账号的同步数据。';
        } else {
            $_SESSION['portal_aadvid'] = $aid;
            header('Location: /dashboard.php');
            exit;
        }
    }
}

$aadvids = dashboard_distinct_aadvids($pdo, $userId);
?>
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>选择广告主 — 千川素材看盘工具</title>
  <style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body {
      min-height: 100vh; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: #f8f9fa; color: #111; display: flex; align-items: center; justify-content: center; padding: 24px;
    }
    .card {
      width: 100%; max-width: 440px; background: #fff; border: 1px solid rgba(0,0,0,0.08);
      border-radius: 12px; padding: 32px; box-shadow: 0 12px 40px rgba(0,0,0,0.04);
    }
    h1 { font-size: 18px; font-weight: 600; margin-bottom: 8px; }
    p.sub { font-size: 13px; color: #666; margin-bottom: 20px; line-height: 1.5; }
    label { display: block; font-size: 13px; color: #444; margin-bottom: 6px; }
    select {
      width: 100%; border: 1px solid #ddd; border-radius: 8px; padding: 10px 12px; font-size: 14px; margin-bottom: 16px;
      background: #fff;
    }
    button {
      width: 100%; background: #000; color: #fff; border: none; border-radius: 8px; padding: 12px;
      font-size: 14px; font-weight: 600; cursor: pointer;
    }
    button:hover { opacity: 0.92; }
    button:disabled { opacity: 0.5; cursor: not-allowed; }
    .err { font-size: 13px; color: #b91c1c; background: #fef2f2; border: 1px solid #fecaca; border-radius: 8px; padding: 10px; margin-bottom: 16px; }
    .hint { font-size: 12px; color: #888; margin-top: 16px; line-height: 1.5; }
    .nav { margin-top: 20px; font-size: 13px; }
    .nav a { color: #666; text-decoration: none; margin-right: 16px; }
    .nav a:hover { color: #000; }
  </style>
</head>
<body>
  <div class="card">
    <h1>选择广告主（aadvid）</h1>
    <p class="sub">列表来自您账号已同步到云端的素材数据。请选择要查看看板的广告主 ID。</p>
    <?php if ($error !== ''): ?>
      <div class="err"><?= htmlspecialchars($error, ENT_QUOTES, 'UTF-8') ?></div>
    <?php endif; ?>
    <?php if ($aadvids === []): ?>
      <p class="hint">暂无可用广告主。请先在桌面客户端登录并同步数据，或联系管理员。</p>
    <?php else: ?>
      <form method="post">
        <input type="hidden" name="_csrf" value="<?= htmlspecialchars(csrf_token(), ENT_QUOTES, 'UTF-8') ?>">
        <label for="aadvid">广告主 ID</label>
        <select id="aadvid" name="aadvid" required>
          <?php foreach ($aadvids as $a): ?>
            <option value="<?= htmlspecialchars($a, ENT_QUOTES, 'UTF-8') ?>"><?= htmlspecialchars($a, ENT_QUOTES, 'UTF-8') ?></option>
          <?php endforeach; ?>
        </select>
        <button type="submit">进入看板</button>
      </form>
    <?php endif; ?>
    <div class="nav">
      <a href="/">首页</a>
      <a href="/logout.php">退出登录</a>
    </div>
  </div>
</body>
</html>
