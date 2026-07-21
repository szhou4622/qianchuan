<?php
declare(strict_types=1);

require_once dirname(__DIR__) . '/includes/bootstrap.php';
require_once dirname(__DIR__) . '/includes/layout.php';
require_once dirname(__DIR__) . '/includes/desktop_release.php';

$me = require_super_admin($pdo);
$GLOBALS['layout_user'] = $me;

$uploadDir = desktop_release_upload_dir();
if (!is_dir($uploadDir)) {
    @mkdir($uploadDir, 0755, true);
}

$maxBytes = 150 * 1024 * 1024;

function dr_valid_platform(string $p): bool
{
    return $p === 'win' || $p === 'mac';
}

function dr_valid_kind(string $k): bool
{
    return $k === 'install' || $k === 'update';
}

/** @return 'zip'|'exe'|'dmg'|null */
function dr_allowed_package_ext(string $filename): ?string
{
    if (!preg_match('/\.([a-z0-9]+)$/i', $filename, $m)) {
        return null;
    }
    $ext = strtolower($m[1]);
    if (!in_array($ext, ['zip', 'exe', 'dmg'], true)) {
        return null;
    }
    return $ext;
}

$filterPlatform = isset($_GET['platform']) ? (string) $_GET['platform'] : '';
$filterKind = isset($_GET['kind']) ? (string) $_GET['kind'] : '';
$useFilter = $filterPlatform !== '' && $filterKind !== '';
if ($filterPlatform !== '' && !dr_valid_platform($filterPlatform)) {
    $filterPlatform = '';
}
if ($filterKind !== '' && !dr_valid_kind($filterKind)) {
    $filterKind = '';
}
if ($filterPlatform === '' || $filterKind === '') {
    $useFilter = false;
}

$redirectBase = '/admin/desktop_release.php';
if ($useFilter) {
    $redirectBase .= '?platform=' . rawurlencode($filterPlatform) . '&kind=' . rawurlencode($filterKind);
}

if ($_SERVER['REQUEST_METHOD'] === 'POST') {
    if (!csrf_verify($_POST['_csrf'] ?? null)) {
        flash_set('验证失败，请重试。');
        header('Location: ' . $redirectBase);
        exit;
    }
    $action = (string) ($_POST['action'] ?? '');

    if ($action === 'upload') {
        $version = trim((string) ($_POST['version'] ?? ''));
        $platform = (string) ($_POST['platform'] ?? 'win');
        $kind = (string) ($_POST['kind'] ?? 'install');
        if (!dr_valid_platform($platform) || !dr_valid_kind($kind)) {
            flash_set('平台或类型无效。');
        } elseif ($version === '') {
            flash_set('请填写版本号。');
        } elseif (!isset($_FILES['package']) || !is_array($_FILES['package'])) {
            flash_set('请选择要上传的文件。');
        } else {
            $err = (int) ($_FILES['package']['error'] ?? UPLOAD_ERR_NO_FILE);
            if ($err !== UPLOAD_ERR_OK) {
                flash_set('上传失败（错误码 ' . $err . '）。');
            } else {
                $size = (int) ($_FILES['package']['size'] ?? 0);
                if ($size <= 0 || $size > $maxBytes) {
                    flash_set('文件无效或超过 ' . (int) ($maxBytes / 1024 / 1024) . ' MB 限制。');
                } else {
                    $orig = (string) ($_FILES['package']['name'] ?? 'release.zip');
                    $orig = preg_replace('/[^\p{L}\p{N}._\-\s\(\)\[\]]/u', '_', $orig) ?: 'release.zip';
                    $ext = dr_allowed_package_ext($orig);
                    if ($ext === null) {
                        flash_set('仅支持 .zip、.exe、.dmg 格式。');
                    } else {
                        $storage = bin2hex(random_bytes(16)) . '.' . $ext;
                        $dest = $uploadDir . '/' . $storage;
                        if (!@move_uploaded_file((string) $_FILES['package']['tmp_name'], $dest)) {
                            flash_set('保存文件失败，请检查 uploads/desktop 目录是否可写。');
                        } else {
                            $ins = $pdo->prepare(
                                'INSERT INTO desktop_releases (platform, kind, version, storage_name, original_filename, file_size) VALUES (?, ?, ?, ?, ?, ?)'
                            );
                            $ins->execute([$platform, $kind, $version, $storage, $orig, $size]);
                            flash_set('已发布新版本。');
                        }
                    }
                }
            }
        }
    } elseif ($action === 'delete') {
        $id = (int) ($_POST['id'] ?? 0);
        $st = $pdo->prepare('SELECT id, storage_name FROM desktop_releases WHERE id = ?');
        $st->execute([$id]);
        $row = $st->fetch();
        if ($row) {
            $path = $uploadDir . '/' . $row['storage_name'];
            if (is_file($path)) {
                @unlink($path);
            }
            $pdo->prepare('DELETE FROM desktop_releases WHERE id = ?')->execute([$id]);
            flash_set('已删除该条发布记录与文件。');
        }
    } elseif ($action === 'update_version') {
        $id = (int) ($_POST['id'] ?? 0);
        $version = trim((string) ($_POST['version'] ?? ''));
        if ($version === '') {
            flash_set('版本号不能为空。');
        } else {
            $st = $pdo->prepare('UPDATE desktop_releases SET version = ? WHERE id = ?');
            $st->execute([$version, $id]);
            if ($st->rowCount() > 0) {
                flash_set('已更新版本号。');
            }
        }
    }

    header('Location: ' . $redirectBase);
    exit;
}

if ($useFilter) {
    $rows = desktop_releases_all($pdo, $filterPlatform, $filterKind);
    $best = desktop_release_latest($pdo, $filterPlatform, $filterKind);
} else {
    $rows = desktop_releases_all($pdo);
    $best = null;
}

$uploadPlatform = $useFilter ? $filterPlatform : 'win';
$uploadKind = $useFilter ? $filterKind : 'install';

$pfLabel = static function (string $p): string {
    return $p === 'mac' ? 'macOS' : 'Windows';
};
$kdLabel = static function (string $k): string {
    return $k === 'install' ? '安装包' : '更新包';
};

layout_header('桌面端版本发布', 'desktop');
$m = flash_get();
if ($m) {
    echo '<div class="mb-4 text-sm text-emerald-700 bg-emerald-50 border border-emerald-200 rounded-lg px-4 py-3">' . htmlspecialchars($m, ENT_QUOTES, 'UTF-8') . '</div>';
}
?>
<p class="text-sm text-slate-600 mb-4">按<strong>平台</strong>与<strong>类型</strong>管理发布文件（支持 .zip / .exe / .dmg）。首页 <code class="bg-slate-100 px-1 rounded text-xs">index.php</code> 使用 <strong>Windows / macOS 的「安装包」</strong>；<code class="bg-slate-100 px-1 rounded text-xs">/api/version.php</code> 使用 <strong>Windows 更新包</strong>；<code class="bg-slate-100 px-1 rounded text-xs">/api/version_mac.php</code> 使用 <strong>macOS 更新包</strong>。各分类下以 <code class="bg-slate-100 px-1 rounded text-xs">version_compare</code> 最大版本号为最新。</p>

<div class="flex flex-wrap gap-2 mb-6 text-sm">
  <a href="/admin/desktop_release.php" class="px-3 py-1.5 rounded-lg border <?= !$useFilter ? 'border-amber-500 bg-amber-50 text-amber-900' : 'border-slate-200 bg-white text-slate-700 hover:border-amber-300' ?>">全部</a>
  <?php
  $tabs = [
      ['win', 'install', 'Windows 安装'],
      ['win', 'update', 'Windows 更新'],
      ['mac', 'install', 'macOS 安装'],
      ['mac', 'update', 'macOS 更新'],
  ];
foreach ($tabs as $t) {
    $active = $useFilter && $filterPlatform === $t[0] && $filterKind === $t[1];
    $href = '/admin/desktop_release.php?platform=' . rawurlencode($t[0]) . '&kind=' . rawurlencode($t[1]);
    $cls = $active ? 'border-amber-500 bg-amber-50 text-amber-900' : 'border-slate-200 bg-white text-slate-700 hover:border-amber-300';
    echo '<a href="' . htmlspecialchars($href, ENT_QUOTES, 'UTF-8') . '" class="px-3 py-1.5 rounded-lg border ' . $cls . '">' . htmlspecialchars($t[2], ENT_QUOTES, 'UTF-8') . '</a>';
}
?>
</div>

<?php if ($useFilter && $best): ?>
  <div class="mb-6 rounded-lg border border-emerald-200 bg-emerald-50/60 px-4 py-3 text-sm text-emerald-900">
    当前分类「<?= htmlspecialchars($pfLabel($filterPlatform), ENT_QUOTES, 'UTF-8') ?> · <?= htmlspecialchars($kdLabel($filterKind), ENT_QUOTES, 'UTF-8') ?>」对外<strong>最新版本</strong>：
    <span class="font-semibold"><?= htmlspecialchars((string) $best['version'], ENT_QUOTES, 'UTF-8') ?></span>
    <span class="text-emerald-700">（文件：<?= htmlspecialchars((string) $best['original_filename'], ENT_QUOTES, 'UTF-8') ?>）</span>
  </div>
<?php endif; ?>

<div class="bg-white rounded-xl border border-slate-200 shadow-sm p-6 mb-8">
  <h2 class="text-lg font-semibold mb-4">发布新版本</h2>
  <form method="post" enctype="multipart/form-data" action="<?= htmlspecialchars($redirectBase, ENT_QUOTES, 'UTF-8') ?>" class="space-y-4 max-w-xl">
    <input type="hidden" name="_csrf" value="<?= htmlspecialchars(csrf_token(), ENT_QUOTES, 'UTF-8') ?>">
    <input type="hidden" name="action" value="upload">
    <div class="grid grid-cols-1 sm:grid-cols-2 gap-4">
      <div>
        <label class="block text-sm text-slate-600 mb-1">平台</label>
        <select name="platform" class="w-full border border-slate-300 rounded-lg px-3 py-2 text-sm">
          <option value="win" <?= $uploadPlatform === 'win' ? 'selected' : '' ?>>Windows</option>
          <option value="mac" <?= $uploadPlatform === 'mac' ? 'selected' : '' ?>>macOS</option>
        </select>
      </div>
      <div>
        <label class="block text-sm text-slate-600 mb-1">类型</label>
        <select name="kind" class="w-full border border-slate-300 rounded-lg px-3 py-2 text-sm">
          <option value="install" <?= $uploadKind === 'install' ? 'selected' : '' ?>>安装包（首页下载）</option>
          <option value="update" <?= $uploadKind === 'update' ? 'selected' : '' ?>>更新包（版本 API）</option>
        </select>
      </div>
    </div>
    <div>
      <label class="block text-sm text-slate-600 mb-1">版本号</label>
      <input name="version" required placeholder="例如 1.2.0" class="w-full border border-slate-300 rounded-lg px-3 py-2 text-sm" pattern="[^\s]+" title="非空版本号，建议语义化版本">
      <p class="mt-1 text-xs text-slate-500">建议使用 x.y.z 形式，便于与客户端比对。</p>
    </div>
    <div>
      <label class="block text-sm text-slate-600 mb-1">安装包 / 更新包文件</label>
      <input name="package" type="file" accept=".zip,.exe,.dmg,application/zip,application/x-msdownload,application/x-apple-diskimage" required class="block w-full text-sm text-slate-600 file:mr-3 file:rounded file:border-0 file:bg-slate-100 file:px-3 file:py-2">
      <p class="mt-1 text-xs text-slate-500">支持 <strong>.zip</strong>、<strong>.exe</strong>（Windows）、<strong>.dmg</strong>（macOS）。单文件最大 <?= (int) ($maxBytes / 1024 / 1024) ?> MB。</p>
    </div>
    <button type="submit" class="bg-amber-600 hover:bg-amber-700 text-white text-sm font-medium px-6 py-2 rounded-lg">上传并发布</button>
  </form>
</div>

<div class="bg-white rounded-xl border border-slate-200 shadow-sm overflow-hidden">
  <div class="px-4 py-3 bg-slate-50 border-b border-slate-200 font-medium text-slate-800">发布记录<?= $useFilter ? '（已筛选）' : '（全部）' ?></div>
  <div class="overflow-x-auto">
    <table class="w-full min-w-[56rem] text-sm">
      <thead class="bg-slate-100 text-slate-700">
        <tr>
          <th class="text-left px-4 py-3 font-medium">ID</th>
          <th class="text-left px-4 py-3 font-medium">平台</th>
          <th class="text-left px-4 py-3 font-medium">类型</th>
          <th class="text-left px-4 py-3 font-medium">版本号</th>
          <th class="text-left px-4 py-3 font-medium">原始文件名</th>
          <th class="text-left px-4 py-3 font-medium">大小</th>
          <th class="text-left px-4 py-3 font-medium">上传时间</th>
          <th class="text-right px-4 py-3 font-medium">操作</th>
        </tr>
      </thead>
      <tbody class="divide-y divide-slate-100">
        <?php foreach ($rows as $r): ?>
          <tr class="hover:bg-slate-50">
            <td class="px-4 py-3"><?= (int) $r['id'] ?></td>
            <td class="px-4 py-3"><?= htmlspecialchars($pfLabel((string) $r['platform']), ENT_QUOTES, 'UTF-8') ?></td>
            <td class="px-4 py-3"><?= htmlspecialchars($kdLabel((string) $r['kind']), ENT_QUOTES, 'UTF-8') ?></td>
            <td class="px-4 py-3 align-top">
              <form method="post" action="<?= htmlspecialchars($redirectBase, ENT_QUOTES, 'UTF-8') ?>" class="flex flex-wrap items-end gap-2">
                <input type="hidden" name="_csrf" value="<?= htmlspecialchars(csrf_token(), ENT_QUOTES, 'UTF-8') ?>">
                <input type="hidden" name="action" value="update_version">
                <input type="hidden" name="id" value="<?= (int) $r['id'] ?>">
                <input name="version" value="<?= htmlspecialchars((string) $r['version'], ENT_QUOTES, 'UTF-8') ?>" class="border border-slate-300 rounded px-2 py-1 text-xs w-36" required title="版本号">
                <button type="submit" class="text-amber-700 hover:underline text-xs whitespace-nowrap">保存</button>
              </form>
            </td>
            <td class="px-4 py-3 text-slate-700"><?= htmlspecialchars((string) $r['original_filename'], ENT_QUOTES, 'UTF-8') ?></td>
            <td class="px-4 py-3 text-slate-600"><?= number_format((int) $r['file_size'] / 1024, 1) ?> KB</td>
            <td class="px-4 py-3 text-slate-500"><?= htmlspecialchars((string) $r['created_at'], ENT_QUOTES, 'UTF-8') ?></td>
            <td class="px-4 py-3 text-right">
              <form method="post" action="<?= htmlspecialchars($redirectBase, ENT_QUOTES, 'UTF-8') ?>" class="inline" onsubmit="return confirm('确定删除该版本记录并删除服务器上的文件？');">
                <input type="hidden" name="_csrf" value="<?= htmlspecialchars(csrf_token(), ENT_QUOTES, 'UTF-8') ?>">
                <input type="hidden" name="action" value="delete">
                <input type="hidden" name="id" value="<?= (int) $r['id'] ?>">
                <button type="submit" class="text-red-600 hover:underline text-sm">删除</button>
              </form>
            </td>
          </tr>
        <?php endforeach; ?>
        <?php if (!$rows): ?>
          <tr><td colspan="8" class="px-4 py-8 text-center text-slate-500">暂无发布记录</td></tr>
        <?php endif; ?>
      </tbody>
    </table>
  </div>
</div>
<?php
layout_footer();
