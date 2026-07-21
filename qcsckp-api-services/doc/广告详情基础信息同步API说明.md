# 广告详情基础信息同步 API 说明

供**桌面端**调用：将本地采集的**广告详情基础信息**批量同步到服务端 MySQL 表 **`pmc_ad_detail_basic`**。按广告主 **`aadvid` 唯一**；若该 `aadvid` 已存在则**更新**该行，否则**插入**（`INSERT ... ON DUPLICATE KEY UPDATE`）。

---

## 基本信息

| 项目 | 说明 |
|------|------|
| **Base URL** | `https://qcscjk.shanghaijiyue.com`（生产环境请使用 HTTPS；测试环境请替换域名） |
| **接口路径** | `/api/pmc_ad_detail_basic.php` |
| **完整 URL 示例** | `https://qcscjk.shanghaijiyue.com/api/pmc_ad_detail_basic.php` |
| **协议** | HTTP/HTTPS |
| **方法** | **仅支持 `POST`**，其它方法返回 **HTTP 405** |

---

## 用途与语义

1. **按 aadvid 幂等同步**：表上 **`aadvid` 有唯一索引**；同一广告主多次提交会**覆盖更新**除主键外的业务字段，不会产生多行重复。  
2. **表须预先存在**：服务端**不会**自动 `CREATE TABLE`。部署前请在数据库中创建 **`pmc_ad_detail_basic`**（结构与线上一致即可）。若表不存在，接口返回 **HTTP 500** 及明确错误文案。  
3. **与素材备份的关系（权限）**：若数据库中存在 **`pmc_promotion_material`** 表，则仅允许同步「**当前登录用户在该表中已出现过的 `aadvid`**」，防止误写他人广告主。若不存在素材表（或尚未有备份库），则不做强校验。  
4. **空数组**：`rows` 为空数组时，仅校验账号与表存在性，返回 `upserted: 0`，不写业务行（可用于探活）。

---

## 请求说明

### Content-Type

与素材备份接口相同，支持：

1. **JSON（推荐）**  
   - Header：`Content-Type: application/json; charset=utf-8`  
   - Body：UTF-8 JSON 对象  

2. **表单**  
   - `application/x-www-form-urlencoded` 或 `multipart/form-data`  
   - `rows` 在表单下难以表达复杂数组，**强烈建议桌面端统一用 JSON**。

### 根级参数

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `username` | string | 是 | 登录账号（**仅普通用户** `role = user`），首尾空格会被忽略 |
| `password` | string | 是 | 密码，原样传输（**请走 HTTPS**） |
| `rows` | array | 否 | 行对象数组；缺省或非数组时按 **空数组** 处理 |

### `rows[]` 每行对象：字段一览

#### 每行必填

| 字段 | 类型 | 说明 |
|------|------|------|
| `aadvid` | string | 广告主唯一标识（与表 `uk_pmc_ad_detail_basic_aadvid` 对应） |
| `ad_id` | string | 广告 ID |

#### 每行可选

| 字段 | 类型 | 说明 |
|------|------|------|
| `budget` | string \| null | 预算 |
| `audience_coverage_count` | string \| null | 受众覆盖数 |
| `compensation_convert` | string \| null | 补偿转化 |
| `ecp_roi2_goal` | number \| null | ROI2 目标值（看板预估 ECPM 等会读取该字段） |
| `creative_type` | number \| null | 创意类型（整数语义） |
| `user_info_id` | string \| null | 用户信息 ID |
| `user_info_name` | string \| null | 用户信息名称 |
| `user_info_unique_id` | string \| null | 用户唯一标识 |

字符串字段若传 **`"null"`**（不区分大小写）或空串，服务端会规范为 SQL `NULL`（与素材备份接口习惯一致）。

#### 可选：时间戳（保留本地 `created_at` / `updated_at` 时）

| 字段 | 类型 | 说明 |
|------|------|------|
| `created_at` | string | 若 JSON 中**出现**该键或 **`updated_at`** 任一键，则该行使用**带时间戳**的写入分支，并写入 `created_at`、`updated_at`（可解析的日期时间字符串） |
| `updated_at` | string | 同上；若只提供其中一个，缺失侧会用另一侧或当前服务器时间补齐 |

若 **`created_at`、`updated_at` 两个键都不出现在 JSON 行对象中**，则使用**不带时间列的 INSERT**，`created_at` / `updated_at` 由 MySQL 默认值与 `ON UPDATE` 维护；更新时 `updated_at` 会刷新为当前时间。

**不要**上传云端自增主键：`id`（由数据库生成）。

---

## 限制

| 项目 | 值 |
|------|-----|
| 单次最多行数 | **500**（常量 `PAD_BACKUP_MAX_ROWS`） |
| 超出时 | `success: false`，提示分批上传 |

---

## 鉴权与账号校验

1. 仅 **`role = user`（普通用户）** 可成功调用；代理、超级管理员即使用户名密码形式正确，也会返回 **`账号或密码错误`**。  
2. 账号 **`is_disabled = 1`**：失败，提示 **`账号已禁用`**。  
3. 若用户有所属代理（`parent_id` 非空）且该代理被禁用：失败，提示 **`代理已禁用，无法同步`**。

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
| `upserted` | number | 本次成功处理（插入或更新）的行数 |
| `user_id` | number | 当前登录用户在 `accounts` 表中的 ID，便于客户端对账 |

---

## 成功示例

**HTTP 200**，Body：

```json
{
  "success": true,
  "data": {
    "upserted": 1,
    "user_id": 15
  }
}
```

### `rows` 为空（仅校验账号与表）

```json
{
  "success": true,
  "data": {
    "upserted": 0,
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
- `{"success":false,"message":"代理已禁用，无法同步"}`

### 6. 超过单次行数上限

- **HTTP 200**  
- `{"success":false,"message":"单次最多提交 500 条，请分批上传"}`

### 7. 某一行参数不合法（整批回滚）

任一行不是对象、缺 `aadvid` / `ad_id` 等，**整次请求不写入任何行**（事务回滚）。

- **HTTP 200**  
- `{"success":false,"message":"rows[0] 缺少 aadvid 或 ad_id"}`（序号随出错行变化）

### 8. aadvid 未通过素材备份校验

当存在 **`pmc_promotion_material`** 表，且当前用户在该表中**没有**该 `aadvid` 的备份记录时：

- **HTTP 200**  
- `{"success":false,"message":"rows[0] 广告主 xxxx 与当前账号素材备份不一致，请先同步该广告主的素材数据"}`

### 9. 数据表不存在

- **HTTP 500**  
- `{"success":false,"message":"数据库中不存在表 pmc_ad_detail_basic，请先在库中创建该表"}`

### 10. 其它写入失败

- **HTTP 500**  
- `{"success":false,"message":"写入失败"}`

---

## 调用示例

### cURL（JSON）

```bash
curl -sS -X POST "https://qcscjk.shanghaijiyue.com/api/pmc_ad_detail_basic.php" \
  -H "Content-Type: application/json; charset=utf-8" \
  -d '{
    "username": "普通用户名",
    "password": "密码",
    "rows": [
      {
        "aadvid": "1234567890",
        "ad_id": "9876543210",
        "ecp_roi2_goal": 2.5,
        "budget": "1000",
        "creative_type": 1
      }
    ]
  }'
```

---

## 桌面端实现建议

1. **与素材备份顺序**：若线上开启了「素材表存在则校验 aadvid」，请先保证 **`/api/pmc_promotion_backup.php`** 已同步过该广告主的素材行，再同步本接口。  
2. **分批**：单次接近 500 条时拆成多请求。  
3. **失败重试**：`500` 或网络错误可退避重试；业务 `message` 需先修正参数或账号状态。  
4. **安全**：密码仅走 HTTPS，不落日志。

---

## 相关文件

| 文件 | 说明 |
|------|------|
| `api/pmc_ad_detail_basic.php` | 接口实现 |
| `doc/千川素材数据云端备份API说明.md` | 素材备份接口说明（鉴权风格一致） |

---

## 变更记录

| 日期 | 说明 |
|------|------|
| 初版 | 与当前 `api/pmc_ad_detail_basic.php` 行为一致；表需预先创建，不自动建表 |

如有字段或语义调整，请以后台实际部署版本为准，并同步更新本文档。
