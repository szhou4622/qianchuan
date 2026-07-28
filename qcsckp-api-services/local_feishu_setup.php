<?php
declare(strict_types=1);

if ((string)getenv('QCSCKP_SERVER_TEST_MODE') !== '1') {
    http_response_code(404);
    exit('Not found');
}

$remote = (string)($_SERVER['REMOTE_ADDR'] ?? '');
if (!in_array($remote, ['127.0.0.1', '::1'], true)) {
    http_response_code(403);
    exit('This page is only available from this computer.');
}

session_start();
header('Cache-Control: no-store');
header("Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'");
header('X-Content-Type-Options: nosniff');
header('X-Frame-Options: DENY');
header('Referrer-Policy: no-referrer');

$secretPath = trim((string)(getenv('QCSCKP_LOCAL_SECRETS') ?: ''));
if ($secretPath === '' || !is_readable($secretPath)) {
    http_response_code(500);
    exit('本机测试配置文件不可用。');
}

function read_local_secrets(string $path): array
{
    $raw = file_get_contents($path);
    $decoded = is_string($raw) ? json_decode($raw, true) : null;
    if (!is_array($decoded) || !is_array($decoded['feishu_app'] ?? null)) {
        throw new RuntimeException('本机测试配置格式无效。');
    }
    return $decoded;
}

function split_ids(string $raw): array
{
    $parts = preg_split('/[\s,，;；]+/u', trim($raw)) ?: [];
    $seen = [];
    foreach ($parts as $part) {
        $part = trim($part);
        if ($part !== '') {
            $seen[$part] = true;
        }
    }
    return array_keys($seen);
}

function remove_mock_ids(array $ids): array
{
    return array_values(array_filter(
        $ids,
        static fn(string $id): bool => !str_starts_with(strtolower($id), 'mock_')
    ));
}

function h(string $value): string
{
    return htmlspecialchars($value, ENT_QUOTES | ENT_SUBSTITUTE, 'UTF-8');
}

try {
    $secrets = read_local_secrets($secretPath);
} catch (Throwable $e) {
    http_response_code(500);
    exit('无法读取本机测试配置。');
}

$notice = '';
$error = '';
if (empty($_SESSION['local_feishu_csrf'])) {
    $_SESSION['local_feishu_csrf'] = bin2hex(random_bytes(24));
}

if (($_SERVER['REQUEST_METHOD'] ?? 'GET') === 'POST') {
    $csrf = (string)($_POST['csrf'] ?? '');
    if (!hash_equals((string)$_SESSION['local_feishu_csrf'], $csrf)) {
        $error = '页面已过期，请刷新后重试。';
    } else {
        $appId = trim((string)($_POST['app_id'] ?? ''));
        $appSecret = trim((string)($_POST['app_secret'] ?? ''));
        $verificationToken = trim((string)($_POST['verification_token'] ?? ''));
        $encryptKey = trim((string)($_POST['encrypt_key'] ?? ''));
        $authorizedOpenId = trim((string)($_POST['authorized_open_id'] ?? ''));
        $openIds = remove_mock_ids(split_ids((string)($_POST['open_ids'] ?? '')));
        $chatIds = remove_mock_ids(split_ids((string)($_POST['chat_ids'] ?? '')));
        if (str_starts_with(strtolower($authorizedOpenId), 'mock_')) {
            $authorizedOpenId = '';
        }
        $existingAppSecret = trim((string)($secrets['feishu_app']['app_secret'] ?? ''));

        if ($appId === '' || !str_starts_with($appId, 'cli_')) {
            $error = '请填写正确的飞书 App ID。';
        } elseif ($appSecret === '' && $existingAppSecret === '') {
            $error = '首次连接真实飞书时必须填写 App Secret。';
        } else {
            $feishu = $secrets['feishu_app'];
            $feishu['enabled'] = true;
            $feishu['mock'] = false;
            $feishu['app_id'] = $appId;
            if ($appSecret !== '') {
                $feishu['app_secret'] = $appSecret;
            }
            if ($verificationToken !== '') {
                $feishu['verification_token'] = $verificationToken;
            }
            if ($encryptKey !== '') {
                $feishu['encrypt_key'] = $encryptKey;
            }
            $feishu['authorized_open_id'] = $authorizedOpenId;
            $feishu['open_ids'] = $openIds;
            $feishu['chat_ids'] = $chatIds;
            $secrets['feishu_app'] = $feishu;

            $json = json_encode(
                $secrets,
                JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES | JSON_PRETTY_PRINT
            );
            if (!is_string($json) || file_put_contents($secretPath, $json . PHP_EOL, LOCK_EX) === false) {
                $error = '保存失败，请检查本机配置文件权限。';
            } else {
                $notice = '已安全保存到本机。飞书真实模式已启用。';
                $secrets = read_local_secrets($secretPath);
            }
        }
    }
}

$feishu = $secrets['feishu_app'];
$statePath = dirname($secretPath) . DIRECTORY_SEPARATOR . 'state.json';
$state = [];
if (is_readable($statePath)) {
    $decodedState = json_decode((string)file_get_contents($statePath), true);
    if (is_array($decodedState)) {
        $state = $decodedState;
    }
}
$callbackUrl = trim((string)($state['callback_https_url'] ?? ''));
$secretReady = trim((string)($feishu['app_secret'] ?? '')) !== '';
$tokenReady = trim((string)($feishu['verification_token'] ?? '')) !== '';
$encryptReady = trim((string)($feishu['encrypt_key'] ?? '')) !== '';
$authorizedOpenId = trim((string)($feishu['authorized_open_id'] ?? ''));
$openIds = implode("\n", is_array($feishu['open_ids'] ?? null) ? $feishu['open_ids'] : []);
$chatIds = implode("\n", is_array($feishu['chat_ids'] ?? null) ? $feishu['chat_ids'] : []);
?>
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>千川工具测试 · 飞书配置</title>
  <style>
    :root { color-scheme: light; font-family: "Microsoft YaHei UI", system-ui, sans-serif; }
    body { margin: 0; background: #f4f7fb; color: #172033; }
    main { width: min(860px, calc(100% - 32px)); margin: 32px auto; }
    .card { background: #fff; border: 1px solid #dfe6f1; border-radius: 16px; padding: 24px; box-shadow: 0 10px 32px rgba(30,55,90,.08); }
    h1 { font-size: 24px; margin: 0 0 8px; }
    .sub { color: #65728a; margin: 0 0 22px; line-height: 1.7; }
    .callback { padding: 14px; border-radius: 10px; background: #eef5ff; border: 1px solid #cfe0ff; word-break: break-all; margin-bottom: 20px; }
    .callback strong { display: block; margin-bottom: 6px; }
    label { display: block; font-weight: 650; margin: 16px 0 7px; }
    input, textarea { box-sizing: border-box; width: 100%; border: 1px solid #cbd5e1; border-radius: 9px; padding: 11px 12px; font: inherit; background: #fff; }
    input:focus, textarea:focus { outline: 2px solid #93b8ff; border-color: #5b8ff9; }
    textarea { min-height: 82px; resize: vertical; }
    small { display: block; color: #718096; margin-top: 5px; line-height: 1.55; }
    .status { display: flex; gap: 8px; flex-wrap: wrap; margin: 14px 0 4px; }
    .pill { background: #edf2f7; color: #526078; border-radius: 999px; padding: 6px 10px; font-size: 13px; }
    .ok { background: #e8f8ef; color: #18794e; }
    .notice, .error { border-radius: 9px; padding: 12px 14px; margin-bottom: 16px; }
    .notice { background: #e9f9ef; color: #176b43; }
    .error { background: #fff0f0; color: #b42318; }
    button { margin-top: 22px; border: 0; border-radius: 9px; padding: 12px 20px; font: inherit; font-weight: 700; color: #fff; background: #3370ff; cursor: pointer; }
    button:hover { background: #245ee8; }
  </style>
</head>
<body>
<main>
  <section class="card">
    <h1>飞书本机测试配置</h1>
    <p class="sub">本页只能从这台电脑访问。所有凭据只保存在本机测试目录，不会写入项目代码、Git 或聊天记录。</p>

    <?php if ($notice !== ''): ?><div class="notice"><?= h($notice) ?></div><?php endif; ?>
    <?php if ($error !== ''): ?><div class="error"><?= h($error) ?></div><?php endif; ?>

    <div class="callback">
      <strong>飞书“事件与回调”请求地址</strong>
      <?= $callbackUrl !== '' ? h($callbackUrl) : 'HTTPS 隧道尚未启动' ?>
    </div>

    <div class="status">
      <span class="pill <?= $secretReady ? 'ok' : '' ?>">App Secret：<?= $secretReady ? '已保存' : '未保存' ?></span>
      <span class="pill <?= $tokenReady ? 'ok' : '' ?>">Verification Token：<?= $tokenReady ? '本机已有值' : '未保存' ?></span>
      <span class="pill <?= $encryptReady ? 'ok' : '' ?>">Encrypt Key：<?= $encryptReady ? '本机已有值' : '未保存' ?></span>
    </div>
    <small>首次连接真实飞书时，即使显示“本机已有值”，也必须用飞书后台当前显示的 Verification Token 和 Encrypt Key 覆盖一次。</small>

    <form method="post" autocomplete="off">
      <input type="hidden" name="csrf" value="<?= h((string)$_SESSION['local_feishu_csrf']) ?>">

      <label for="app_id">App ID</label>
      <input id="app_id" name="app_id" required value="<?= h((string)($feishu['app_id'] ?? '')) ?>" placeholder="cli_xxx">

      <label for="app_secret">App Secret</label>
      <input id="app_secret" name="app_secret" type="password" placeholder="<?= $secretReady ? '已保存；留空表示不修改' : '从飞书“凭证与基础信息”复制' ?>">

      <label for="verification_token">Verification Token</label>
      <input id="verification_token" name="verification_token" type="password" placeholder="从飞书“事件与回调”复制；留空表示不修改">

      <label for="encrypt_key">Encrypt Key</label>
      <input id="encrypt_key" name="encrypt_key" type="password" placeholder="从飞书“事件与回调”复制；留空表示不修改">

      <label for="authorized_open_id">唯一授权人的 Open ID</label>
      <input id="authorized_open_id" name="authorized_open_id" value="<?= h($authorizedOpenId) ?>" placeholder="ou_xxx">
      <small>只有这个飞书用户可以点击“确认追投”或“暂不追投”。还没有 Open ID 时可以先留空。</small>

      <label for="open_ids">接收个人卡片的 Open ID</label>
      <textarea id="open_ids" name="open_ids" placeholder="每行一个；第一轮只填你自己的 Open ID"><?= h($openIds) ?></textarea>

      <label for="chat_ids">接收群卡片的群 ID</label>
      <textarea id="chat_ids" name="chat_ids" placeholder="每行一个 oc_...；第一轮个人测试可以留空"><?= h($chatIds) ?></textarea>

      <button type="submit">保存到本机并启用真实飞书</button>
    </form>
  </section>
</main>
</body>
</html>
