# 数据库字段说明（采集入库完整对照）

本文档依据仓库源码逐项核对，用于搭建**线上 MySQL 等库**时的字段设计参考。

---

## 1. 结论概览

| 项目 | 说明 |
|------|------|
| **实际写入的 SQLite 表** | 仅 **`pmc_promotion_material`**（见 `utils/sqlite_store.py` 中 `TABLE_SCHEMAS`） |
| **写入入口** | `services/fetcher.py` → `_save_to_database()` → `SQLiteStore.insert(...)` |
| **行数据清洗** | `utils/clean_promotion.py` → `clean_pmc_promotion_row()` / `clean_pmc_promotion_data()` |
| **数据来源** | 浏览器拦截千川接口响应 JSON：`statsData.rows[]`，每条含 `dimensions`、`metrics`（见下文章节 3） |
| **未在运行时代码使用的定义** | `clean_basic_info()` 与 **`pmc_account_info` 表**仅在 `utils/clean_promotion.py` 注释示例中出现，**当前工程无任何调用与建表**，线上若需要账号维度可单独扩展 |

---

## 2. 表 `pmc_promotion_material`：列定义与写入方式

表结构以 `utils/sqlite_store.py` 中 `TABLE_SCHEMAS['pmc_promotion_material']` 为准。

### 2.1 列清单（完整）

| # | 列名（英文） | SQLite 类型 | 是否由采集 INSERT 显式写入 | 含义与备注 |
|---|--------------|-------------|---------------------------|------------|
| 1 | `id` | INTEGER PK AUTOINCREMENT | **否**（自增） | 本地主键 |
| 2 | `aadvid` | TEXT NOT NULL | **是** | 广告主 ID，来自抓取 URL 查询参数 `aavid`（`fetcher._current_aadvid`） |
| 3 | `material_id` | TEXT NOT NULL | **是** | 素材 ID，对应接口 `dimensions.materialId.value` |
| 4 | `video_name` | TEXT | **是** | `dimensions.roi2MaterialVideoName.value` |
| 5 | `material_status` | INTEGER | **是** | `dimensions.roi2MaterialStatus.value` |
| 6 | `show_status` | INTEGER | **是** | `dimensions.roi2MaterialShowStatus.value` |
| 7 | `show_status_reason` | TEXT | **是** | `dimensions.roi2MaterialShowStatusReason.value`；若为字符串 `"null"` 则存为 SQL NULL |
| 8 | `upload_time` | TEXT | **是** | `dimensions.roi2MaterialUploadTime.value`；`"-"` 视为 NULL |
| 9 | `video_type` | INTEGER | **是** | `dimensions.roi2MaterialVideoType.value`（飞书展示映射见 `pmc_row_mapping.VIDEO_TYPE_MAP`） |
| 10 | `video_id` | TEXT | **是** | 自 `roi2MaterialVideoPlayInfo` JSON 内 `VideoId` |
| 11 | `aweme_item_id` | INTEGER | **是** | 同上 JSON 内 `AwemeItemId` |
| 12 | `cover_url` | TEXT | **是** | `CoverImage.WebUrl` |
| 13 | `cover_width` | INTEGER | **是** | `CoverImage.Width` |
| 14 | `cover_height` | INTEGER | **是** | `CoverImage.Height` |
| 15 | `video_duration` | INTEGER | **是** | JSON `VideoDuration` |
| 16 | `video_title` | TEXT | **是** | JSON `Title` |
| 17 | `lego_source` | INTEGER | **是** | JSON `LegoSource` |
| 18 | `video_create_time` | TEXT | **是** | JSON `CreateTime`（无则 NULL） |
| 19 | `tag_list` | TEXT | **是** | `dimensions.materialTagList.value` 解析为 JSON 数组后，**逗号拼接**存成单字符串 |
| 20 | `stat_cost` | REAL | **是** | `metrics.statCostForRoi2.value` |
| 21 | `order_settle_count_1h` | INTEGER | **是** | `metrics.totalOrderSettleCountForRoi21H.value` |
| 22 | `order_settle_amount_1h` | REAL | **是** | `metrics.totalOrderSettleAmountForRoi21H.value` |
| 23 | `order_settle_rate_1h` | REAL | **是** | `metrics.totalOrderSettleAmountRateForRoi21H.value` |
| 24 | `prepay_pay_order_count` | REAL | **是** | `metrics.totalPrepayAndPayOrderRoi2.value` |
| 25 | `pay_gmv_include_coupon` | REAL | **是** | `metrics.totalPayOrderGmvIncludeCouponForRoi2.value` |
| 26 | `prepay_pay_settle_1h` | REAL | **是** | `metrics.totalPrepayAndPaySettleRoi21H.value` |
| 27 | `refund_rate_1h` | REAL | **是** | `metrics.totalRefundOrderGmvForRoi21HRate.value` |
| 28 | `stat_date` | TEXT NOT NULL | **是** | 抓取入库当日 **`YYYY-MM-DD`**（`fetcher` 使用 `datetime.now().strftime("%Y-%m-%d")`，**非** SQLite 默认值路径） |
| 29 | `created_at` | TEXT NOT NULL | **否**（默认） | SQLite：`DEFAULT (datetime('now', '+8 hours'))`；INSERT 语句**不包含**该列时由库写入 |
| 30 | `updated_at` | TEXT NOT NULL | **否**（默认） | 同上；INSERT 语句**不包含**该列时由库写入 |

**说明**：`fetcher._save_to_database()` 对每个 `item` 仅设置 `aadvid`、`stat_date` 及清洗结果中的列；**不包含** `id`、`created_at`、`updated_at`，后三者依赖 SQLite 表定义的 `DEFAULT`。

---

## 3. 千川接口 JSON → 清洗函数字段映射

拦截的 URL 前缀见 `services/fetcher.py` 中 `API_PREFIXES`：

- `https://qianchuan.jinritemai.com/ad/api/pmc/v1/uni-promotion/material/list-required`

响应中路径：`data.statsData.rows[]`，每行结构为 `{ "dimensions": { "字段名": { "value": ... } }, "metrics": { ... } }`。

### 3.1 dimensions（`clean_pmc_promotion_row` 中 `dv()`）

| 接口 dimension 键 | 入库列名 |
|-------------------|----------|
| `materialId` | `material_id`（空行跳过；**`materialId == "-2"` 的聚合行在清洗前即被跳过**） |
| `roi2MaterialVideoName` | `video_name` |
| `roi2MaterialStatus` | `material_status` |
| `roi2MaterialShowStatus` | `show_status` |
| `roi2MaterialShowStatusReason` | `show_status_reason` |
| `roi2MaterialUploadTime` | `upload_time` |
| `roi2MaterialVideoType` | `video_type` |
| `materialTagList` | `tag_list`（JSON 数组 → 逗号拼接字符串） |
| `roi2MaterialVideoPlayInfo` | 解析为 JSON 后映射 `video_id`、`aweme_item_id`、`cover_*`、`video_duration`、`video_title`、`lego_source`、`video_create_time`（见 3.3） |

### 3.2 metrics（`mv()`）

| 接口 metric 键 | 入库列名 |
|----------------|----------|
| `statCostForRoi2` | `stat_cost` |
| `totalOrderSettleCountForRoi21H` | `order_settle_count_1h` |
| `totalOrderSettleAmountForRoi21H` | `order_settle_amount_1h` |
| `totalOrderSettleAmountRateForRoi21H` | `order_settle_rate_1h` |
| `totalPrepayAndPayOrderRoi2` | `prepay_pay_order_count` |
| `totalPayOrderGmvIncludeCouponForRoi2` | `pay_gmv_include_coupon` |
| `totalPrepayAndPaySettleRoi21H` | `prepay_pay_settle_1h` |
| `totalRefundOrderGmvForRoi21HRate` | `refund_rate_1h` |

### 3.3 `roi2MaterialVideoPlayInfo` 解析后的 JSON 子字段

| JSON 路径 | 入库列名 |
|-----------|----------|
| `VideoId` | `video_id` |
| `AwemeItemId` | `aweme_item_id` |
| `CoverImage.WebUrl` | `cover_url` |
| `CoverImage.Width` | `cover_width` |
| `CoverImage.Height` | `cover_height` |
| `VideoDuration` | `video_duration` |
| `Title` | `video_title` |
| `LegoSource` | `lego_source` |
| `CreateTime` | `video_create_time` |

---

## 4. 入库策略（与线上库设计相关）

- **插入方式**：每条素材每轮抓取为 **`INSERT` 新行**（`fetcher` 注释：不更新，**允许重复**），即同一素材在不同时间多次抓取会产生多行，靠 `id`、`created_at` 区分。
- **索引**（SQLite 中与线上一致即可）：`aadvid`、`stat_date`、`material_id`、`video_type`、`material_status`、`created_at`，以及联合 `(created_at, material_id)`（见 `TABLE_SCHEMAS` 内 `indexes`）。

---

## 5. 飞书同步使用的列子集（非 SQLite 写入逻辑）

`services/feishu_bitable/pmc_row_mapping.py` 中 **`DEFAULT_PMC_MATERIAL_TO_FEISHU`** 仅映射部分列到飞书多维表，**不影响**本地表全量字段；线上若只做同步备份，可按该映射建宽表；若要与本地 SQLite 一致，请以本文第 2、3 节全量字段为准。

---

## 6. 相关源文件索引

| 文件 | 作用 |
|------|------|
| `utils/sqlite_store.py` | 表结构、`insert`、`CREATE TABLE` |
| `utils/clean_promotion.py` | 接口 JSON → 行字典 |
| `services/fetcher.py` | 拦截响应、`aadvid`/`stat_date`、调用 `insert` |
| `services/feishu_bitable/pmc_row_mapping.py` | 本地列 → 飞书列名（可选） |

---

*若源码中表结构变更，请同步更新本文件与 `schema_pmc_promotion_material.mysql.sql`。*
