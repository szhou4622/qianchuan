# 桌面端版本检测 / 更新说明 API

供**桌面端**轮询使用：比对当前安装版本与服务器**最新发布版本**；若有更新，客户端可弹窗提示用户前往 **`download_url`** 下载 zip（**不做静默自动更新**，由用户手动下载安装）。

后台由**超级管理员**在「**桌面版本**」页上传 `.zip` 并填写版本号；服务端在所有发布记录中取 **版本号最大** 的一条（PHP `version_compare`）作为「当前最新」。

---

## 基本信息

| 项目 | 说明 |
|------|------|
| **Base URL** | `https://qcscjk.shanghaijiyue.com`（生产环境请使用 HTTPS） |
| **接口路径** | `/api/version.php` |
| **完整 URL** | `https://qcscjk.shanghaijiyue.com/api/version.php` |
| **方法** | **GET 或 POST 均可**（便于客户端任选） |

---

## 请求参数

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `current_version` | string | 建议填 | 客户端当前版本号，例如 `1.0.0`。**不传或空字符串**时按 `0` 与最新版比对，若服务器有发布则通常 **`has_update` 为 true**。 |

### 传参方式

- **GET**：查询串，例如 `?current_version=1.0.0`
- **POST + JSON**：`Content-Type: application/json`，body：`{"current_version":"1.0.0"}`
- **POST + 表单**：字段名 `current_version`

---

## 响应格式

- **Content-Type**：`application/json; charset=utf-8`
- **缓存**：响应头含 `Cache-Control: no-store`，请客户端也不要强缓存该接口。

### 根字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `success` | boolean | 固定为 `true`（请求被正常处理时） |
| `data` | object | 见下表 |

### `data` 对象

| 字段 | 类型 | 说明 |
|------|------|------|
| `latest_version` | string \| null | 服务器当前**最新版本号**；若尚未发布任何安装包则为 `null` |
| `has_update` | boolean | **`true`** 表示服务器最新版 **高于** 客户端 `current_version`（`version_compare`）；无发布记录时为 `false` |
| `download_url` | string \| null | 最新安装包 **可直接下载的 HTTPS URL**（指向站点 `uploads/desktop/` 下文件）；无发布时为 `null` |
| `file_size` | number \| null | 文件大小（字节）；无发布时为 `null` |
| `original_filename` | string \| null | 上传时的原始 zip 文件名，可用于保存对话框默认名；无发布时为 `null` |

### 版本比对规则

- 使用 PHP 语义：`version_compare($latest_version, $current_version, '>')`。
- **建议**客户端与后台统一使用 **x.y.z** 形式（如 `1.2.0`），避免 `1.10` 与 `1.2` 等混用导致误判。

---

## 响应示例

### 有更新

```json
{
  "success": true,
  "data": {
    "latest_version": "1.2.0",
    "has_update": true,
    "download_url": "https://qcscjk.shanghaijiyue.com/uploads/desktop/a1b2c3....zip",
    "file_size": 5242880,
    "original_filename": "MyApp-1.2.0-win-x64.zip"
  }
}
```

### 已是最新

```json
{
  "success": true,
  "data": {
    "latest_version": "1.2.0",
    "has_update": false,
    "download_url": "https://qcscjk.shanghaijiyue.com/uploads/desktop/a1b2c3....zip",
    "file_size": 5242880,
    "original_filename": "MyApp-1.2.0-win-x64.zip"
  }
}
```

### 服务器尚未发布任何安装包

```json
{
  "success": true,
  "data": {
    "latest_version": null,
    "has_update": false,
    "download_url": null,
    "file_size": null,
    "original_filename": null
  }
}
```

---

## 客户端建议流程

1. 应用启动后或定时器（例如每数小时 / 每天）请求本接口，传入当前程序版本 `current_version`。  
2. 若 `data.has_update === true`：弹窗提示有新版本，展示 `latest_version`，提供按钮使用系统浏览器打开 `download_url` 或使用下载组件拉取 zip。  
3. **不要**在后台静默覆盖安装；由用户下载后自行解压/覆盖。  
4. `download_url` 为公开直链，请通过 **HTTPS** 访问；无需携带账号密码。

---

## 后台管理（超级管理员）

- 路径：**账号管理后台 → 桌面版本**（`/admin/desktop_release.php`）。  
- 上传 **.zip**（单文件上限以页面提示为准，默认 150MB），填写版本号后发布。  
- 可删除历史记录（同时删除服务器上的对应文件）。  
- 「当前对外最新版本」由系统在全部记录中取 **版本号最大** 的一条，与上传顺序无关。
