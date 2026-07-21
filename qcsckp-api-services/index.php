<?php
declare(strict_types=1);

require_once __DIR__ . '/includes/bootstrap.php';
require_once __DIR__ . '/includes/portal_auth.php';
require_once __DIR__ . '/includes/desktop_release.php';

$portalMe = portal_user($pdo);
$portalAadvid = portal_selected_aadvid();

$winInstall = desktop_release_latest_for_homepage($pdo, 'win');
$macInstall = desktop_release_latest_for_homepage($pdo, 'mac');

$scheme = 'http';
if (!empty($_SERVER['HTTP_X_FORWARDED_PROTO']) && in_array($_SERVER['HTTP_X_FORWARDED_PROTO'], ['http', 'https'], true)) {
    $scheme = $_SERVER['HTTP_X_FORWARDED_PROTO'];
} elseif (!empty($_SERVER['HTTPS']) && $_SERVER['HTTPS'] !== 'off') {
    $scheme = 'https';
} elseif (isset($_SERVER['SERVER_PORT']) && (string) $_SERVER['SERVER_PORT'] === '443') {
    $scheme = 'https';
}
$host = $_SERVER['HTTP_HOST'] ?? 'localhost';
$canonicalUrl = $scheme . '://' . $host . '/';

$winDl = null;
if ($winInstall !== null) {
    $winDl = [
        'path' => desktop_release_public_path((string) $winInstall['storage_name']),
        'filename' => (string) $winInstall['original_filename'],
        'version' => (string) $winInstall['version'],
        'size_label' => desktop_release_format_size((int) $winInstall['file_size']),
    ];
}
$macDl = null;
if ($macInstall !== null) {
    $macDl = [
        'path' => desktop_release_public_path((string) $macInstall['storage_name']),
        'filename' => (string) $macInstall['original_filename'],
        'version' => (string) $macInstall['version'],
        'size_label' => desktop_release_format_size((int) $macInstall['file_size']),
    ];
}

$siteName = '千川素材看盘工具';
$pageTitle = $siteName . ' — 千川素材数据看盘与桌面端下载';
$pageDescription = '千川素材看盘工具，专为巨量千川素材经营设计：梯队对比、投放节奏对照与桌面端（Windows / macOS）安装包下载，界面克制、信息密度可控。';

function h(string $s): string
{
    return htmlspecialchars($s, ENT_QUOTES, 'UTF-8');
}

$jsonLdGraph = [
    [
        '@type' => 'WebSite',
        'name' => $siteName,
        'url' => $canonicalUrl,
        'description' => $pageDescription,
        'inLanguage' => 'zh-CN',
    ],
];
$offer = [
    '@type' => 'Offer',
    'price' => '0',
    'priceCurrency' => 'CNY',
];
if ($winDl !== null) {
    $app = [
        '@type' => 'SoftwareApplication',
        'name' => $siteName,
        'applicationCategory' => 'BusinessApplication',
        'operatingSystem' => 'Windows',
        'description' => $pageDescription,
        'offers' => $offer,
        'softwareVersion' => $winDl['version'],
        'downloadUrl' => $scheme . '://' . $host . $winDl['path'],
    ];
    $jsonLdGraph[] = $app;
}
if ($macDl !== null) {
    $app = [
        '@type' => 'SoftwareApplication',
        'name' => $siteName,
        'applicationCategory' => 'BusinessApplication',
        'operatingSystem' => 'macOS',
        'description' => $pageDescription,
        'offers' => $offer,
        'softwareVersion' => $macDl['version'],
        'downloadUrl' => $scheme . '://' . $host . $macDl['path'],
    ];
    $jsonLdGraph[] = $app;
}
$jsonLd = [
    '@context' => 'https://schema.org',
    '@graph' => $jsonLdGraph,
];

$steps = [
    ['t' => '取包与安装', 'd' => '在本页下载对应系统的安装包（Windows / macOS，若已上架），解压后按说明完成安装。'],
    ['t' => '登录与拉数', 'd' => '使用已开通的账号登录，按引导完成同步；权限与可见范围以账号为准。'],
    ['t' => '固定看盘习惯', 'd' => '约定每日/每周固定时间看梯队与异常，再回业务侧做决策与改创意。'],
];
?>
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title><?= h($pageTitle) ?></title>
  <meta name="description" content="<?= h($pageDescription) ?>">
  <meta name="keywords" content="千川,素材看盘,千川投放,广告投放,素材分析,桌面客户端">
  <meta name="robots" content="index,follow,max-image-preview:large">
  <meta name="author" content="<?= h($siteName) ?>">
  <link rel="canonical" href="<?= h($canonicalUrl) ?>">
  <meta name="theme-color" content="#ffffff">

  <meta property="og:type" content="website">
  <meta property="og:site_name" content="<?= h($siteName) ?>">
  <meta property="og:title" content="<?= h($pageTitle) ?>">
  <meta property="og:description" content="<?= h($pageDescription) ?>">
  <meta property="og:url" content="<?= h($canonicalUrl) ?>">
  <meta property="og:locale" content="zh_CN">

  <meta name="twitter:card" content="summary">
  <meta name="twitter:title" content="<?= h($pageTitle) ?>">
  <meta name="twitter:description" content="<?= h($pageDescription) ?>">

  <script type="application/ld+json"><?= json_encode($jsonLd, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES) ?></script>
  <style>
    :root {
      --bg-base: #ffffff;
      --surface-1: #f8f9fa;
      --surface-2: #f1f3f5;
      --text-high: #000000;
      --text-med: #555555;
      --text-low: #888888;
      --line-light: rgba(0, 0, 0, 0.1);
      --line-dim: rgba(0, 0, 0, 0.04);
      --shadow-soft: 0 12px 40px rgba(0,0,0,0.04);
      --font-mono: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, Courier, monospace;
    }
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body {
      background-color: var(--bg-base);
      color: var(--text-high);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
      line-height: 1.6;
      overflow-x: hidden;
      -webkit-font-smoothing: antialiased;
    }
    .skip-link {
      position: absolute; top: -100px; left: 0;
      background: #000; color: #fff; padding: 8px 16px;
      font-family: var(--font-mono); font-size: 12px; z-index: 9999;
    }
    .skip-link:focus { top: 0; outline: 2px solid #fff; outline-offset: 2px; }
    .container { max-width: 1240px; margin: 0 auto; padding: 0 5vw; }
    .tech-grid {
      position: fixed; top: 0; left: 0; width: 100vw; height: 100vh;
      background-size: 40px 40px;
      background-image:
        linear-gradient(to right, var(--line-dim) 1px, transparent 1px),
        linear-gradient(to bottom, var(--line-dim) 1px, transparent 1px);
      mask-image: radial-gradient(ellipse at 50% 20%, black 20%, transparent 80%);
      -webkit-mask-image: radial-gradient(ellipse at 50% 20%, black 20%, transparent 80%);
      z-index: -1; pointer-events: none;
    }
    @keyframes fadeUp {
      0% { opacity: 0; transform: translateY(30px); filter: blur(4px); }
      100% { opacity: 1; transform: translateY(0); filter: blur(0); }
    }
    .anim-up { opacity: 0; animation: fadeUp 1s cubic-bezier(0.2, 0.8, 0.2, 1) forwards; }
    .d-1 { animation-delay: 0.1s; } .d-2 { animation-delay: 0.2s; }
    .d-3 { animation-delay: 0.3s; } .d-4 { animation-delay: 0.4s; }
    @media (prefers-reduced-motion: reduce) {
      .anim-up { animation: none; opacity: 1; }
      .nav-btn { transition: none; }
      .nav-btn--primary:hover,
      .nav-btn--primary:active { transform: none; }
    }
    header {
      display: flex; justify-content: space-between; align-items: center;
      gap: 24px;
      padding: 30px 0; border-bottom: 1px solid var(--line-light);
    }
    .header-actions {
      display: flex;
      align-items: center;
      gap: 10px;
      flex-shrink: 0;
    }
    .logo-box { display: flex; align-items: center; gap: 12px; }
    .logo-mark { width: 12px; height: 12px; background: var(--text-high); }
    .logo-text { font-weight: 600; font-size: 14px; letter-spacing: 2px;}
    .nav-btn {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      font-family: inherit;
      font-size: 13px;
      font-weight: 500;
      letter-spacing: 0.02em;
      text-decoration: none;
      border-radius: 8px;
      padding: 9px 18px;
      min-height: 40px;
      border: 1px solid transparent;
      transition: background 0.2s ease, color 0.2s ease, border-color 0.2s ease, box-shadow 0.2s ease, transform 0.2s ease;
    }
    .nav-btn:focus-visible {
      outline: 2px solid var(--text-high);
      outline-offset: 2px;
    }
    .nav-btn--primary {
      background: var(--text-high);
      color: var(--bg-base);
      box-shadow: 0 1px 2px rgba(0, 0, 0, 0.06);
    }
    .nav-btn--primary:hover {
      background: #2a2a2a;
      box-shadow: 0 4px 14px rgba(0, 0, 0, 0.12);
      transform: translateY(-1px);
    }
    .nav-btn--primary:active { transform: translateY(0); box-shadow: 0 1px 2px rgba(0, 0, 0, 0.08); }
    .nav-btn--secondary {
      background: var(--surface-1);
      color: var(--text-high);
      border-color: rgba(0, 0, 0, 0.1);
    }
    .nav-btn--secondary:hover {
      background: var(--surface-2);
      border-color: rgba(0, 0, 0, 0.18);
    }
    .hero { padding: 80px 0 120px; text-align: center; position: relative; }
    .system-badge {
      display: inline-block; font-family: var(--font-mono); font-size: 11px;
      color: var(--text-med); border: 1px solid var(--line-light);
      padding: 4px 12px; border-radius: 20px; margin-bottom: 30px;
      background: var(--bg-base);
    }
    .hero h1 {
      font-size: clamp(40px, 6vw, 72px); font-weight: 700;
      letter-spacing: -2px; margin-bottom: 24px; line-height: 1.1;
      background: linear-gradient(180deg, #000 0%, #666 100%);
      -webkit-background-clip: text; -webkit-text-fill-color: transparent;
      background-clip: text;
    }
    .hero-desc {
      color: var(--text-med); font-size: clamp(14px, 2vw, 18px); max-width: 700px;
      margin: 0 auto 50px; line-height: 1.8; font-weight: 400;
    }
    .cta-group { display: flex; justify-content: center; align-items: center; gap: 20px; flex-wrap: wrap; }
    .btn-core {
      position: relative; overflow: hidden; display: inline-flex; align-items: center;
      background: var(--text-high); color: var(--bg-base);
      padding: 16px 32px; font-weight: 600; font-size: 14px; text-decoration: none;
      transition: 0.3s; box-shadow: var(--shadow-soft);
    }
    .btn-core:hover { transform: translateY(-2px); box-shadow: 0 12px 30px rgba(0,0,0,0.15); }
    .btn-core::after {
      content: ''; position: absolute; top: -50%; left: -50%; width: 200%; height: 200%;
      background: linear-gradient(45deg, transparent, rgba(255,255,255,0.2), transparent);
      transform: rotate(45deg); transition: 0.5s; opacity: 0;
    }
    .btn-core:hover::after { opacity: 1; left: 100%; }
    .data-tag {
      font-family: var(--font-mono); font-size: 12px; color: var(--text-low);
      display: flex; flex-direction: column; text-align: left; border-left: 1px solid var(--line-light); padding-left: 16px;
    }
    .data-tag span:first-child { color: var(--text-high); font-weight: bold; }
    .hero-wait { color: var(--text-med); font-size: 15px; max-width: 520px; margin: 0 auto; }
    .hud-wrapper {
      margin-top: 80px; position: relative;
      border-top: 1px solid var(--line-light);
      background: linear-gradient(180deg, rgba(0,0,0,0.01) 0%, transparent 100%);
      padding: 40px 20px; display: flex; flex-direction: column; align-items: center;
    }
    .hud-wrapper::before {
      content: ''; position: absolute; top: -1px; left: 50%; transform: translateX(-50%);
      width: 30%; height: 2px; background: var(--text-high);
    }
    .hud-panel {
      width: 100%; max-width: 900px; border: 1px solid var(--line-light);
      background: var(--bg-base); box-shadow: var(--shadow-soft);
      position: relative; text-align: left;
    }
    .hud-header {
      display: flex; justify-content: space-between; border-bottom: 1px solid var(--line-light); padding: 12px 20px;
      font-family: var(--font-mono); font-size: 11px; color: var(--text-low); text-transform: uppercase;
    }
    .hud-body { padding: 30px; }
    .hud-title-row { display: flex; justify-content: space-between; margin-bottom: 30px; }
    .hud-title { font-size: 16px; font-weight: 500; }
    .hud-env { font-family: var(--font-mono); font-size: 10px; border: 1px solid var(--line-light); padding: 2px 8px; color: var(--text-med);}
    .hud-data-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 20px; }
    .hud-bar { height: 4px; background: var(--line-dim); position: relative; overflow: hidden; }
    .hud-bar::after { content: ''; position: absolute; left: 0; top: 0; height: 100%; width: 60%; background: var(--text-high); }
    .hud-bar.b2::after { width: 30%; background: var(--text-med); }
    .hud-bar.b3::after { width: 85%; }
    .hud-caption { margin-top: 20px; font-family: var(--font-mono); font-size: 11px; color: var(--text-low); text-align: center; }
    .slogan-section { padding: 100px 0; display: flex; flex-direction: column; align-items: center; text-align: center; }
    .section-label {
      font-family: var(--font-mono); font-size: 12px; color: var(--text-low);
      text-transform: uppercase; letter-spacing: 4px; margin-bottom: 24px;
      display: flex; align-items: center; gap: 12px;
    }
    .section-label::before, .section-label::after { content: ''; width: 20px; height: 1px; background: var(--line-light); }
    .slogan-text {
      font-size: clamp(20px, 3vw, 32px); max-width: 900px; line-height: 1.6; font-weight: 400; color: var(--text-med);
    }
    .slogan-text strong { color: var(--text-high); font-weight: 600; display: block; margin-top: 10px;}
    .bento-section { padding: 80px 0; }
    .section-title { font-size: 32px; font-weight: 600; margin-bottom: 16px; letter-spacing: -1px; }
    .section-desc { color: var(--text-med); margin-bottom: 60px; font-size: 15px; }
    .bento-grid {
      display: grid; grid-template-columns: repeat(12, 1fr); grid-auto-rows: minmax(240px, auto); gap: 20px;
    }
    .bento-card {
      background: var(--surface-1); border: 1px solid var(--line-light); padding: 40px;
      position: relative; overflow: hidden; transition: 0.4s;
    }
    .bento-card:hover { border-color: #000; background: var(--surface-2); box-shadow: var(--shadow-soft); }
    .bento-card::before {
      content: ''; position: absolute; top: 0; left: 0; width: 100%; height: 2px;
      background: linear-gradient(90deg, transparent, rgba(0,0,0,0.8), transparent);
      transform: translateX(-100%); transition: 0.6s ease-in-out; opacity: 0;
    }
    .bento-card:hover::before { transform: translateX(100%); opacity: 1; }
    .bento-tag { font-family: var(--font-mono); font-size: 11px; color: var(--text-med); border: 1px solid var(--line-light); display: inline-block; padding: 4px 10px; margin-bottom: 24px; background: #fff;}
    .bento-card h3 { font-size: 22px; font-weight: 600; margin-bottom: 16px; color: var(--text-high); }
    .bento-card p { color: var(--text-med); font-size: 14px; line-height: 1.7; }
    .bento-1 { grid-column: span 12; display: flex; flex-direction: column; justify-content: center; background: var(--surface-1) url('data:image/svg+xml;utf8,<svg width="100" height="100" xmlns="http://www.w3.org/2000/svg"><defs><pattern id="grid" width="20" height="20" patternUnits="userSpaceOnUse"><path d="M 20 0 L 0 0 0 20" fill="none" stroke="rgba(0,0,0,0.03)" stroke-width="1"/></pattern></defs><rect width="100%" height="100%" fill="url(%23grid)" /></svg>'); }
    .bento-2 { grid-column: span 6; }
    .bento-3 { grid-column: span 6; }
    @media (max-width: 768px) {
      .bento-2, .bento-3 { grid-column: span 12; }
    }
    .pipeline-section { padding: 100px 0; border-top: 1px solid var(--line-light); margin-top: 40px;}
    .pipeline-layout { display: flex; gap: 80px; align-items: flex-start; }
    .pipeline-info { flex: 1; position: sticky; top: 100px; }
    .pipeline-track { flex: 1.5; position: relative; padding-left: 40px; }
    .pipeline-track::before {
      content: ''; position: absolute; left: 0; top: 10px; bottom: 0; width: 1px;
      background: linear-gradient(to bottom, var(--text-high) 0%, var(--line-light) 50%, transparent 100%);
    }
    .step-node { position: relative; margin-bottom: 60px; }
    .step-node::before {
      content: ''; position: absolute; left: -44px; top: 4px; width: 9px; height: 9px;
      background: var(--bg-base); border: 2px solid var(--text-high); border-radius: 50%;
    }
    .step-num-tech { font-family: var(--font-mono); font-size: 12px; color: var(--text-low); margin-bottom: 8px; display: block; }
    .step-node h4 { font-size: 18px; font-weight: 600; margin-bottom: 12px; }
    .step-node p { color: var(--text-med); font-size: 14px; line-height: 1.6; }
    @media (max-width: 900px) {
      .pipeline-layout { flex-direction: column; gap: 40px; }
      .pipeline-info { position: static; }
    }
    .tech-banner {
      margin: 80px 0; padding: 1px;
      background: linear-gradient(90deg, transparent, var(--line-light), transparent);
    }
    .tech-banner-inner {
      background: var(--surface-1); padding: 50px 60px; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 30px;
    }
    .banner-text h3 { font-size: 20px; font-weight: 600; margin-bottom: 8px; }
    .banner-text p { color: var(--text-med); font-size: 14px; max-width: 500px; }
    .banner-action { display: flex; align-items: center; gap: 20px; }
    .btn-outline-tech {
      font-family: var(--font-mono); font-size: 13px; color: var(--text-high); text-decoration: none;
      border: 1px solid var(--text-high); padding: 12px 24px; transition: 0.3s;
      display: inline-flex; align-items: center; gap: 10px;
    }
    .btn-outline-tech:hover { background: var(--text-high); color: var(--bg-base); }
    .btn-outline-tech::before { content: '↓'; }
    .faq-section { padding: 80px 0 120px; border-top: 1px dashed var(--line-light); }
    .faq-header { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 40px; border-bottom: 1px solid var(--line-light); padding-bottom: 20px;}
    details { border-bottom: 1px solid var(--line-light); transition: 0.3s; background: transparent; }
    details:hover { background: rgba(0,0,0,0.01); padding-left: 10px; border-color: #000; }
    summary {
      padding: 24px 0; font-size: 16px; font-weight: 500; cursor: pointer; list-style: none;
      display: flex; justify-content: space-between; align-items: center; color: var(--text-high);
    }
    summary::-webkit-details-marker { display: none; }
    summary::after { content: '+'; font-family: var(--font-mono); color: var(--text-med); transition: 0.3s; }
    details[open] summary::after { transform: rotate(45deg); color: var(--text-high); }
    .faq-content { padding: 0 0 24px 0; color: var(--text-med); font-size: 14px; font-family: var(--font-mono); line-height: 1.6;}
    .faq-content::before { content: '> '; color: var(--text-low); }
    footer { border-top: 1px solid var(--line-light); padding: 60px 0; }
    .footer-grid { display: flex; justify-content: space-between; align-items: flex-end; font-family: var(--font-mono); font-size: 11px; color: var(--text-low); flex-wrap: wrap; gap: 20px;}
    .footer-brand { color: var(--text-high); font-size: 14px; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; font-weight: 600; margin-bottom: 8px;}
  </style>
</head>
<body>

  <a href="#main" class="skip-link">跳到主要内容</a>

  <div class="tech-grid"></div>

  <div class="container">
    <header class="anim-up">
      <a href="<?= h($canonicalUrl) ?>" class="logo-box" style="text-decoration:none;color:inherit;">
        <div class="logo-mark"></div>
        <div class="logo-text"><?= h($siteName) ?></div>
      </a>
      <?php if ($portalMe !== null): ?>
      <div class="header-actions">
        <?php if ($portalAadvid !== null): ?>
        <a href="/dashboard.php" class="nav-btn nav-btn--primary" aria-label="进入看板">看板</a>
        <?php else: ?>
        <a href="/aadvid.php" class="nav-btn nav-btn--primary" aria-label="选择广告主">选择广告主</a>
        <?php endif; ?>
        <a href="/logout.php" class="nav-btn nav-btn--secondary" aria-label="退出登录">退出</a>
      </div>
      <?php else: ?>
      <div class="header-actions">
        <a href="/login.php" class="nav-btn nav-btn--primary" aria-label="登录">登录</a>
      </div>
      <?php endif; ?>
    </header>

    <main id="main">
      <section class="hero">
        <div class="system-badge anim-up d-1">SYS // 巨量千川 · 素材经营</div>
        <h1 class="anim-up d-2"><?= h($siteName) ?></h1>
        <p class="hero-desc anim-up d-3">把「哪条素材在跑、跑得怎样、和计划是否同频」放在同一视线里。少翻页、少脑补，把精力放在判断与调优上。</p>

        <div class="cta-group anim-up d-4" style="flex-direction: column; align-items: center; gap: 24px;">
          <?php if ($winDl !== null || $macDl !== null): ?>
            <?php if ($winDl !== null): ?>
            <div style="display: flex; justify-content: center; align-items: center; gap: 20px; flex-wrap: wrap;">
              <a href="<?= h($winDl['path']) ?>" download="<?= h($winDl['filename']) ?>" class="btn-core" aria-label="下载 Windows 桌面端安装包，当前版本 <?= h($winDl['version']) ?>">获取 Windows 客户端</a>
              <div class="data-tag">
                <span>Windows v<?= h($winDl['version']) ?></span>
                <span>SIZE: <?= h($winDl['size_label']) ?></span>
              </div>
            </div>
            <?php endif; ?>
            <?php if ($macDl !== null): ?>
            <div style="display: flex; justify-content: center; align-items: center; gap: 20px; flex-wrap: wrap;">
              <a href="<?= h($macDl['path']) ?>" download="<?= h($macDl['filename']) ?>" class="btn-core" aria-label="下载 macOS 桌面端安装包，当前版本 <?= h($macDl['version']) ?>" style="background:#1d1d1f;">获取 macOS 客户端</a>
              <div class="data-tag">
                <span>macOS v<?= h($macDl['version']) ?></span>
                <span>SIZE: <?= h($macDl['size_label']) ?></span>
              </div>
            </div>
            <?php endif; ?>
          <?php else: ?>
          <p class="hero-wait" role="status">安装包上架后会显示在本页，请稍后再来。</p>
          <?php endif; ?>
        </div>

        <div class="hud-wrapper anim-up d-4">
          <div class="hud-panel">
            <div class="hud-header">
              <span>// 示意 //</span>
              <span>DATALINK ACTIVE</span>
            </div>
            <div class="hud-body">
              <div class="hud-title-row">
                <div class="hud-title">素材梯队与消耗结构</div>
                <div class="hud-env">桌面端</div>
              </div>
              <div class="hud-data-grid">
                <div class="hud-bar b1"></div><div class="hud-bar b2"></div>
                <div class="hud-bar b3"></div><div class="hud-bar b1"></div>
              </div>
              <div class="hud-data-grid" style="grid-template-columns: repeat(2, 1fr); opacity: 0.5;">
                <div class="hud-bar b2"></div><div class="hud-bar b3"></div>
              </div>
            </div>
          </div>
          <p class="hud-caption">上图仅为版式示意，不代表真实数据。实际指标与维度以客户端为准。</p>
        </div>
      </section>
    </main>
  </div>

  <section class="slogan-section">
    <div class="container">
      <div class="section-label anim-up">产品定位</div>
      <p class="slogan-text anim-up d-1">
        不是又一个「大而全」后台，而是为盯素材、比梯队、对节奏准备的独立视图——
        <strong>用桌面客户端打开，专注一块屏。</strong>
      </p>
    </div>
  </section>

  <section class="bento-section">
    <div class="container">
      <h2 class="section-title anim-up">能力一览</h2>
      <p class="section-desc anim-up">按使用场景拆成四块，方便你对照自己是否刚需。</p>

      <div class="bento-grid">
        <div class="bento-card bento-1 anim-up d-1">
          <span class="bento-tag">01. 对比</span>
          <h3>梯队与差异，一眼扫过</h3>
          <p style="max-width: 600px;">把头部、腰部、尾部素材放在同一尺度下看，减少「单条盯很清、整体说不清」的情况。适合测新素材、筛留强素材。</p>
        </div>
        <div class="bento-card bento-2 anim-up d-2">
          <span class="bento-tag">02. 节奏</span>
          <h3>和投放日历对齐</h3>
          <p>按日、按计划回看素材走势，方便对照「今天该不该换创意、该不该加压」。</p>
        </div>
        <div class="bento-card bento-3 anim-up d-3">
          <span class="bento-tag">03. 专注</span>
          <h3>信息密度可控</h3>
          <p>界面留白与层级偏克制，长时间盯盘时减轻视觉负担；细节能力放在客户端内逐步展开。</p>
        </div>
      </div>
    </div>
  </section>

  <section class="pipeline-section">
    <div class="container pipeline-layout">
      <div class="pipeline-info anim-up">
        <h2 class="section-title">从零到日常看盘</h2>
        <p class="section-desc">三步走完安装与上手；之后以客户端内的流程为准。</p>
      </div>
      <div class="pipeline-track anim-up d-1">
        <?php foreach ($steps as $i => $step): ?>
        <div class="step-node">
          <span class="step-num-tech">STEP _<?= sprintf('%02d', $i + 1) ?></span>
          <h4><?= h($step['t']) ?></h4>
          <p><?= h($step['d']) ?></p>
        </div>
        <?php endforeach; ?>
      </div>
    </div>
  </section>

  <?php if ($winDl !== null || $macDl !== null): ?>
  <div class="container">
    <?php if ($winDl !== null): ?>
    <div class="tech-banner anim-up">
      <div class="tech-banner-inner">
        <div class="banner-text">
          <h3>Windows 离线安装包</h3>
          <p>与首屏「获取 Windows 客户端」相同来源。压缩包请完整下载后再解压。</p>
        </div>
        <div class="banner-action">
          <div class="data-tag" style="border-left: none; text-align: right; border-right: 1px solid var(--line-light); padding-right: 16px; padding-left: 0;">
            <span>v<?= h($winDl['version']) ?></span>
            <span><?= h($winDl['size_label']) ?></span>
          </div>
          <a href="<?= h($winDl['path']) ?>" download="<?= h($winDl['filename']) ?>" class="btn-outline-tech" aria-label="下载 Windows 安装包，版本 <?= h($winDl['version']) ?>">下载 Windows 包</a>
        </div>
      </div>
    </div>
    <?php endif; ?>
    <?php if ($macDl !== null): ?>
    <div class="tech-banner anim-up" style="margin-top:24px;">
      <div class="tech-banner-inner">
        <div class="banner-text">
          <h3>macOS 离线安装包</h3>
          <p>与首屏「获取 macOS 客户端」相同来源。请完整下载后再按说明安装。</p>
        </div>
        <div class="banner-action">
          <div class="data-tag" style="border-left: none; text-align: right; border-right: 1px solid var(--line-light); padding-right: 16px; padding-left: 0;">
            <span>v<?= h($macDl['version']) ?></span>
            <span><?= h($macDl['size_label']) ?></span>
          </div>
          <a href="<?= h($macDl['path']) ?>" download="<?= h($macDl['filename']) ?>" class="btn-outline-tech" aria-label="下载 macOS 安装包，版本 <?= h($macDl['version']) ?>">下载 macOS 包</a>
        </div>
      </div>
    </div>
    <?php endif; ?>
  </div>
  <?php endif; ?>

  <section class="faq-section">
    <div class="container">
      <div class="faq-header anim-up">
        <h2 class="section-title" style="margin:0;">常见问题</h2>
        <span style="font-family: var(--font-mono); font-size: 11px; color: var(--text-low);">若与客户端内说明冲突，以软件内文案为准。</span>
      </div>

      <div class="anim-up d-1">
        <details>
          <summary>支持哪些系统？</summary>
          <div class="faq-content">若后台已上架对应安装包，本页会提供 Windows 与 macOS 下载入口；未显示的系统表示暂未提供安装包，请以本页实际按钮为准。</div>
        </details>
        <details>
          <summary>解压或安装失败？</summary>
          <div class="faq-content">先确认下载完整、磁盘空间充足，并尝试关闭拦截可执行文件的安全软件。若仍失败，请联系为你开通账号的服务方。</div>
        </details>
        <details>
          <summary>数据和后台完全一致吗？</summary>
          <div class="faq-content">同步频率、统计口径与权限范围以客户端为准；网络波动时可能出现短暂不一致。</div>
        </details>
      </div>
    </div>
  </section>

  <footer>
    <div class="container footer-grid">
      <div>
        <div class="footer-brand"><?= h($siteName) ?></div>
        <div>请从本官方页面获取安装包，勿安装来历不明的文件。</div>
      </div>
      <div>STATUS: ONLINE // <?= h((string) date('Y')) ?> · 保留所有权利</div>
    </div>
  </footer>

</body>
</html>
