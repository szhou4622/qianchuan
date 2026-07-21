# 账号信息查询 API 说明

供**桌面端**调用：登录校验、以及周期性拉取账号最新有效期与禁用状态（例如约每 1 分钟一次）。

---

## 基本信息

| 项目 | 说明 |
|------|------|
| **Base URL** | `https://qcscjk.shanghaijiyue.com`（生产环境请使用 HTTPS） |
| **接口路径** | `/api/account.php` |
| **完整 URL** | `https://qcscjk.shanghaijiyue.com/api/account.php` |
| **协议** | HTTP/HTTPS |
| **方法** | **仅支持 `POST`**，其它方法返回 `405` |

---

## 请求说明

### Content-Type

支持两种常见方式（服务端会优先读 **JSON 请求体**，否则读 **表单**）：

1. **JSON（推荐）**  
   - Header：`Content-Type: application/json; charset=utf-8`  
   - Body：UTF-8 JSON 对象  

2. **表单**  
   - Header：`Content-Type: application/x-www-form-urlencoded`（或 `multipart/form-data`）  
   - 字段同下表  

### 参数

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `username` | string | 是 | 登录账号（普通用户用户名），首尾空格会被忽略 |
| `password` | string | 是 | 密码，**原样传输**（请走 HTTPS） |

**注意：** 本接口仅校验 **`role = 普通用户(user)`** 的账号。代理、超级管理员账号即使密码正确，也会返回与「账号或密码错误」相同失败语义（不单独区分「角色不允许」）。

---

## 响应格式

- 统一为 **JSON**  
- 字符编码：**UTF-8**  
- 根节点字段：  

| 字段 | 类型 | 说明 |
|------|------|------|
| `success` | boolean | 是否校验通过且返回业务数据 |
| `message` | string | 仅失败时出现，人类可读说明 |
| `data` | object | 仅成功时出现，见下表 |

### `data` 对象（`success === true` 时）

| 字段 | 类型 | 说明 |
|------|------|------|
| `valid_from` | string \| null | 有效期开始时间，格式一般为 `YYYY-MM-DD HH:MM:SS`（与数据库一致） |
| `valid_until` | string \| null | 有效期结束时间，同上 |
| `is_disabled` | number | **0 或 1**。`1` 表示账号被禁用或**因所属代理被禁用而视为不可用**（见下文） |

**桌面端建议逻辑：**

1. **是否允许使用：** `is_disabled === 1` → 不允许。  
2. **是否在有效期内：** 用本机当前时间与 `valid_until`（及必要时 `valid_from`）比较；具体时区与边界建议与产品一致（通常按服务器存储的时间字符串解析为本地或 UTC 后比较）。  
3. **周期性同步：** 同一组 `username` / `password` 定时调用本接口，用最新返回的 `valid_*` 与 `is_disabled` 刷新本地缓存。

---

## 成功示例

**HTTP 200**，Body：

```json
{
  "success": true,
  "data": {
    "valid_from": "2026-03-20 18:26:00",
    "valid_until": "2027-03-20 18:26:00",
    "is_disabled": 0
  }
}
```

---

## 失败示例

### 1. 未使用 POST

- **HTTP 405**  
- Body：

```json
{
  "success": false,
  "message": "请使用 POST"
}
```

### 2. 缺少账号或密码

- **HTTP 200**（业务失败，非 HTTP 4xx）  
- Body：

```json
{
  "success": false,
  "message": "请提供账号和密码"
}
```

### 3. 账号不存在、密码错误、或非普通用户账号

- **HTTP 200**  
- Body：

```json
{
  "success": false,
  "message": "账号或密码错误"
}
```

---

## 特殊说明：所属代理被禁用

若该普通用户**所属代理**在后台被禁用，接口仍返回 **`success: true`**，但 **`data.is_disabled` 固定为 `1`**，`valid_from` / `valid_until` 仍为该用户当前库中记录。  

桌面端只需按 **`is_disabled === 1` 拒绝使用**即可，无需额外判断代理状态。

---

## 调用示例

### cURL（JSON）

```bash
curl -sS -X POST "https://qcscjk.shanghaijiyue.com/api/account.php" \
  -H "Content-Type: application/json; charset=utf-8" \
  -d "{\"username\":\"你的用户名\",\"password\":\"你的密码\"}"
```

### cURL（表单）

```bash
curl -sS -X POST "https://qcscjk.shanghaijiyue.com/api/account.php" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "username=你的用户名&password=你的密码"
```

---

## 安全与实现建议

1. **务必使用 HTTPS**，避免密码在传输中泄露。  
2. 客户端**不要**在日志、崩溃上报中输出明文密码。  
3. 若需长期轮询，可对密码做安全存储（系统钥匙串等），避免明文落盘。  
4. 接口未在文档中约定额外 Header（如 API Key）；若后续增加鉴权，以服务端通知为准。

---

## 变更记录

| 日期 | 说明 |
|------|------|
| 初版 | 与当前 `api/account.php` 行为一致 |

如有字段或语义调整，请以后台实际部署版本为准，并同步更新本文档。
