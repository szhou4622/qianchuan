# 千川素材数据云端备份 API 说明

供**桌面端**调用：将本地 SQLite（`pmc_promotion_material` 表）中已采集的素材行，批量 **INSERT** 到服务端 MySQL，实现云端备份。字段含义与本地库一致，详见仓库内 **`doc/DATABASE_FIELDS.md`**。

---

## 基本信息

| 项目 | 说明 |
|------|------|
| **Base URL** | `https://qcscjk.shanghaijiyue.com`（生产环境请使用 HTTPS；测试环境请替换域名） |
| **接口路径** | `/api/pmc_promotion_backup.php` |
| **完整 URL 示例** | `https://qcscjk.shanghaijiyue.com/api/pmc_promotion_backup.php` |
| **协议** | HTTP/HTTPS |
| **方法** | **仅支持 `POST`**，其它方法返回 **HTTP 405** |

---

## 用途与语义

1. **只做追加写入**：每条请求在服务端执行多条 `INSERT`；**不做**按 `material_id` 去重或更新。同一素材多次备份会产生多行，与本地采集「允许多次 INSERT」策略一致。  
2. **用户隔离**：服务端根据登录账号解析 **`accounts.id`**，写入列 **`user_id`**。客户端**不要**上传 `user_id`（上传也会被忽略；实现上仅服务端写入）。  
3. **云端主键**：服务端表主键为自增 **`id`**，与本地 SQLite 的 **`id` 无关**；备份时**不必**上传本地 `id`。  
4. **空数组**：`rows` 为空数组时，仅校验账号并返回 `inserted: 0`，不写库（可用于探活或鉴权探测）。

---

## 请求说明

### Content-Type

与账号查询接口相同，支持：

1. **JSON（推荐）**  
   - Header：`Content-Type: application/json; charset=utf-8`  
   - Body：UTF-8 JSON 对象  

2. **表单**  
   - `application/x-www-form-urlencoded` 或 `multipart/form-data`  
   - 注意：`rows` 在表单场景下难以表达复杂数组，**强烈建议桌面端统一用 JSON**。

### 根级参数

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `username` | string | 是 | 登录账号（**仅普通用户** `role = user`），首尾空格会被忽略 |
| `password` | string | 是 | 密码，原样传输（**请走 HTTPS**） |
| `rows` | array | 否 | 行对象数组；缺省或非数组时按 **空数组** 处理 |

### `rows[]` 每行对象：字段一览

以下字段名建议与本地 SQLite 写入列**同名同型**，便于直接从本地行字典序列化。

#### 每行必填

| 字段 | 类型 | 说明 |
|------|------|------|
| `aadvid` | string | 广告主 ID |
| `material_id` | string | 素材 ID |
| `stat_date` | string | 统计日，**必须为 `YYYY-MM-DD`** |

#### 每行可选（与 `DATABASE_FIELDS.md` 一致）

| 字段 | 类型 | 说明 |
|------|------|------|
| `video_name` | string \| null | |
| `material_status` | number \| null | 整数语义 |
| `show_status` | number \| null | |
| `show_status_reason` | string \| null | 若接口曾用字符串 **`"null"`** 表示无值，服务端会存为 SQL `NULL` |
| `upload_time` | string \| null | 若为 **`"-"`**，服务端存为 `NULL` |
| `video_type` | number \| null | |
| `video_id` | string \| null | |
| `aweme_item_id` | number \| null | |
| `cover_url` | string \| null | |
| `cover_width` | number \| null | |
| `cover_height` | number \| null | |
| `video_duration` | number \| null | |
| `video_title` | string \| null | |
| `lego_source` | number \| null | |
| `video_create_time` | string \| null | |
| `tag_list` | string \| null | 本地为逗号拼接字符串 |
| `stat_cost` | number \| null | |
| `order_settle_count_1h` | number \| null | |
| `order_settle_amount_1h` | number \| null | |
| `order_settle_rate_1h` | number \| null | |
| `prepay_pay_order_count` | number \| null | |
| `pay_gmv_include_coupon` | number \| null | |
| `prepay_pay_settle_1h` | number \| null | |
| `refund_rate_1h` | number \| null | |
| `overall_show_count` | number \| null | **整体展现次数**；未采集或不传则存 `NULL` |
| `overall_click_count` | number \| null | **整体点击次数**；同上 |
| `overall_ctr` | number \| null | **整体点击率**（小数，如接口为百分比请客户端先换算）；同上 |
| `overall_conversion_rate` | number \| null | **整体转化率**；同上 |

#### 可选：时间戳（保留本地 `created_at` / `updated_at` 时）

| 字段 | 类型 | 说明 |
|------|------|------|
| `created_at` | string | 若 JSON 中**出现**该键或 **`updated_at`** 任一键，则该行插入时**会写入** `created_at`、`updated_at`（可解析的日期时间字符串，如 `2026-03-21 12:00:00` 或 ISO 形式） |
| `updated_at` | string | 同上；若只提供其中一个，缺失侧会用另一侧或当前服务器时间补齐（见实现：至少一侧有值则两列都写入） |

若 **`created_at`、`updated_at` 两个键都不出现在 JSON 行对象中**，则插入**不写**这两列，由 MySQL 使用表默认值（一般为当前时间）。

**不要**上传云端字段：`id`、`user_id`（由服务端生成或绑定）。

---

## 限制

| 项目 | 值 |
|------|-----|
| 单次最多行数 | **2000**（常量 `PMC_BACKUP_MAX_ROWS`） |
| 超出时 | `success: false`，提示分批上传 |

---

## 鉴权与账号校验

1. 仅 **`role = user`（普通用户）** 可成功备份；代理、超级管理员即使用户名密码形式正确，也会返回与「账号或密码错误」相同的失败语义（与 `api/account.php` 一致）。  
2. 账号 **`is_disabled = 1`**：失败，提示 **`账号已禁用`**。  
3. 若用户有所属代理（`parent_id` 非空）且该代理被禁用：失败，提示 **`代理已禁用，无法备份`**（与账号查询接口「仍返回 success 但 is_disabled=1」的策略不同，此处直接拒绝写入）。

---

## 响应格式

- 统一为 **JSON**，**UTF-8**  
- 根节点字段：

| 字段 | 类型 | 说明 |
|------|------|------|
| `success` | boolean | 是否成功 |
| `message` | string | 失败时人类可读原因 |
| `data` | object | 成功时出现 |

### `data` 对象（`success === true`）

| 字段 | 类型 | 说明 |
|------|------|------|
| `inserted` | number | 本次成功插入的行数 |
| `user_id` | number | 当前登录用户在 `accounts` 表中的 ID，便于客户端对账 |

---

## 成功示例

**HTTP 200**，Body：

```json
{
  "success": true,
  "data": {
    "inserted": 2,
    "user_id": 15
  }
}
```

### `rows` 为空（仅校验账号）

```json
{
  "success": true,
  "data": {
    "inserted": 0,
    "user_id": 15
  }
}
```

---

## 失败示例

### 1. 未使用 POST

- **HTTP 405**  
- Body：`{"success":false,"message":"请使用 POST"}`

### 2. 缺少账号或密码

- **HTTP 200**  
- `{"success":false,"message":"请提供账号和密码"}`

### 3. 账号或密码错误、或非普通用户

- **HTTP 200**  
- `{"success":false,"message":"账号或密码错误"}`

### 4. 账号已禁用

- **HTTP 200**  
- `{"success":false,"message":"账号已禁用"}`

### 5. 所属代理已禁用

- **HTTP 200**  
- `{"success":false,"message":"代理已禁用，无法备份"}`

### 6. 超过单次行数上限

- **HTTP 200**  
- `{"success":false,"message":"单次最多提交 2000 条，请分批上传"}`

### 7. 某一行参数不合法（整批回滚）

任一行不是对象、缺必填字段、`stat_date` 格式不是 `YYYY-MM-DD` 等，**整次请求不插入任何行**（事务回滚）。

- **HTTP 200**  
- `{"success":false,"message":"rows[0] 缺少 aadvid、material_id 或 stat_date"}`（序号随出错行变化）

### 8. 数据表初始化失败（如数据库账号无建表权限）

- **HTTP 500**  
- `{"success":false,"message":"数据表初始化失败"}`  

> 服务端会在**首次需要时**自动 `CREATE TABLE`；若线上已是旧表结构，会在连接后**自动 `ALTER TABLE` 补齐**「整体展现/点击/点击率/转化率」四列（可空）。若希望由 DBA 预先建表，可执行 **`doc/schema_pmc_promotion_material.mysql.sql`**，与线上一致即可。

### 9. 其它写入失败

- **HTTP 500**  
- `{"success":false,"message":"写入失败"}`

---

## 调用示例

### cURL（JSON）

```bash
curl -sS -X POST "https://qcscjk.shanghaijiyue.com/api/pmc_promotion_backup.php" \
  -H "Content-Type: application/json; charset=utf-8" \
  -d '{
    "username": "普通用户名",
    "password": "密码",
    "rows": [
      {
        "aadvid": "1234567890",
        "material_id": "987654321",
        "stat_date": "2026-03-21",
        "video_name": "示例素材",
        "stat_cost": 12.34,
        "overall_show_count": 1000,
        "overall_click_count": 50,
        "overall_ctr": 0.05,
        "overall_conversion_rate": 0.02
      }
    ]
  }'
```

未采集到整体展现/点击/点击率/转化率时，行对象里可**整段省略**上述四个键（不必传 `null`），与其它可选列行为一致。

---

## 桌面端实现建议

1. **分批**：单次接近 2000 行时拆成多请求。  
2. **失败重试**：`500` 或网络错误可退避重试；`4xx/业务 message` 需先修正参数或账号状态。  
3. **安全**：密码仅走 HTTPS，不落日志。  
4. **与本地 SQLite 对齐**：可直接将本地插入前的行字典（去掉本地 `id`）序列化为 `rows` 元素；详见 **`doc/DATABASE_FIELDS.md`**。

---

## 相关文件

| 文件 | 说明 |
|------|------|
| `api/pmc_promotion_backup.php` | 接口实现 |
| `doc/DATABASE_FIELDS.md` | 字段与采集来源说明 |
| `doc/schema_pmc_promotion_material.mysql.sql` | 服务端 MySQL 表结构（含 `user_id`） |

---

## 变更记录

| 日期 | 说明 |
|------|------|
| 初版 | 与当前 `api/pmc_promotion_backup.php` 行为一致 |
| 增补 | 增加可选字段 `overall_show_count`、`overall_click_count`、`overall_ctr`、`overall_conversion_rate`（整体展现/点击/点击率/转化率）；可不传，与 `doc/DATABASE_FIELDS.md` 一致 |

如有字段或语义调整，请以后台实际部署版本为准，并同步更新本文档。
