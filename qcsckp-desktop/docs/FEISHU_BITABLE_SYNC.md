# 飞书多维表格数据写入：完整逻辑与实现说明

本文说明本项目中 **飞书多维表格（Bitable）** 的写入链路、依赖、数据映射与表头同步策略。  
**注意**：飞书**群机器人 Webhook** 整点推送（`services/feishu_webhook_push.py`、`hook/feishu_bot.py`）与多维表格 **不是同一条链路**，文末有简要区分。

---

## 1. 功能目标与实现状态

| 项目 | 说明 |
|------|------|
| **目标** | 在本地 SQLite 成功插入 `pmc_promotion_material` 行之后，将**同一批素材行**同步写入用户指定的飞书多维表格（新增记录）。 |
| **鉴权方式** | 飞书 **个人 Base Open**（`baseopensdk`）：`app_token` + `personal_base_token` + `table_id`。 |
| **是否阻塞采集** | **否**。飞书同步在 `asyncio.to_thread` 中执行；失败仅打日志，不影响千川抓取与 SQLite 写入。 |
| **是否全量字段同步** | **否**。仅同步映射表 `DEFAULT_PMC_MATERIAL_TO_FEISHU` 中的列（见第 5 节）；本地表 30 列不会全部推到飞书。 |

---

## 2. 端到端调用链（从配置到 API）

### 2.1 配置如何进入抓取进程

1. **前端**（`static/control.html`）在输入框中填写 `app_token`、`personal_base_token`、`table_id`，可存浏览器 `localStorage`，并调用 `api.setFeishuBitableConfig(app, pat, tbl)`。
2. **API**（`api/views.py`）转发到 `ServiceController.setFeishuBitableConfig`（`services/run_services.py`）。
3. **ServiceController** 在内存中保存三个字符串（线程锁保护）；**不落盘为独立配置文件**（与 Webhook 的 `data/feishu_webhook_push.json` 不同）。
4. 每次轮询调用 `fetcher.fetch(...)` 前，`run_services` 用 `_snapshot_feishu_bitable()` 取出当前快照，作为参数传入：

```text
run_services._run_async
  → fetcher.fetch(..., feishu_app_token=fa, feishu_personal_base_token=fp, feishu_table_id=ft)
```

5. **QianChuanFetcher**（`services/fetcher.py`）在 `fetch()` 开头若三者均非空则写入实例变量 `_feishu_*`，供本轮抓取内 `_save_to_database` 使用；任一为空白则清空，**本轮不写飞书**。

### 2.2 何时触发写入

仅在 **`_save_to_database`** 中：SQLite `insert` 成功后，对**本批新插入的行**构造飞书用数据并调用 `_sync_batch_to_feishu`。

流程摘要：

```text
_save_to_database(db)
  1. clean_pmc_promotion_data → 得到 new_data（英文列名）
  2. 每行补充 aadvid、stat_date
  3. for item in new_data: db.insert("pmc_promotion_material", item)
  4. feishu_rows = [{**item, "created_at": now_ts} for item in new_data]   # 飞书专用补「创建时间」
  5. await _sync_batch_to_feishu(feishu_rows)
```

要点：

- **`created_at`**：SQLite 插入依赖表 DEFAULT，**INSERT 字典不含 `created_at`**；同步飞书时用当前时刻 `YYYY-MM-DD HH:MM:SS` 显式填入，对应飞书列「创建时间」。
- **批次**：与「每页/每批新增入库」一致，飞书按**同一批 `new_data` 条数**批量创建记录。

### 2.3 异步与线程

`_sync_batch_to_feishu` 内使用：

```python
await asyncio.to_thread(_run)
```

其中 `_run()` 同步调用 `BitableTable(...).insert_pmc_material_rows(rows)`，避免阻塞 asyncio 事件循环（`baseopensdk` 为同步 HTTP）。

---

## 3. 门面层：`BitableTable`（`services/feishu_bitable/facade.py`）

对单张表绑定 `(app_token, personal_base_token, table_id)`，对外语义化方法：

| 方法 | 作用 |
|------|------|
| `insert(rows)` | `rows` 的 key 已是飞书**列显示名**，直接交给底层 `add_rows`。 |
| `insert_pmc_material_rows(rows, local_to_feishu=...)` | **PMC 业务入口**：先 `ensure_headers`，再逐行 `map_pmc_material_row_to_feishu` 后 `insert`。 |

`inner` 属性暴露底层 `FeishuBaseOperator`，用于表头重建、改名等高级操作。

---

## 4. 底层：`FeishuBaseOperator` + `baseopensdk`（`services/feishu_bitable/base_table.py`）

### 4.1 客户端

- `BaseClient.builder().app_token(...).personal_base_token(...).build()`
- 使用 `baseopensdk` 的 `base.v1` 接口：`app_table_field`、`app_table_record`。

### 4.2 写入记录：`add_rows`

1. 调用 `ListAppTableField` 拉取列 **显示名 → field_id**、主键列名、各列 **type**。
2. 对每行输入字典：**key 必须是列显示名**（与官方示例一致）；若用 `fldxxx` 作 key 会报错 `FieldNameNotFound`（代码注释已说明）。
3. 对每个单元格调用 `coerce_value_for_field_type(列 type, 值)`（`bitable_types.py`），按类型转为飞书易接受的形态（文本/数字/日期等）。
4. **`_fill_primary_field`**：若本行未包含主键列，会尝试用「用户ID、姓名、名称…」等常见列名兜底，否则用第一个非空列，最后才填 `"-"`，避免创建记录失败。
5. 使用自定义轻量 body 类 `_BatchCreateRecordBody`（仅含 `fields`），避免 SDK 对象序列化带入多余字段导致飞书校验失败。
6. 调用 **`batch_create`** 批量新增记录。

### 4.3 表头对齐：`ensure_headers` → `rebuild_headers(..., keep_original_fields=True)`

在 `insert_pmc_material_rows` **每次插入前**都会执行，目的：

1. **按顺序**将表格前几列的显示名**改名**为期望列表中的名称（解决默认主键列「文本」与目标「创建时间」等对齐问题）。
2. 对仍不存在的列名 **创建新字段**（默认 **type=1 单行文本**）。
3. **不删除**已有旧列（`keep_original_fields=True`）。

若需「替换模式」删多余列，可使用 `rebuild_headers(new_field_names, keep_original_fields=False)`（内部含两阶段临时重名防冲突逻辑）。

### 4.4 其它能力（非采集主路径）

- **更新 / 删除**：`update_rows`（需 `record_id`）、`delete_rows`（每批 ≤500 条，内部分片）。
- **查询**：`get_all_records` 分页 list；`get_record` / `get_records_by_ids` 多次 get。

---

## 5. 列映射：`pmc_row_mapping.py`

### 5.1 白名单映射 `DEFAULT_PMC_MATERIAL_TO_FEISHU`

本地 **英文列名** → 飞书 **列显示名**（须与多维表格表头**完全一致**）：

| 本地列名 | 飞书列显示名 |
|----------|----------------|
| `created_at` | 创建时间 |
| `material_id` | 素材ID |
| `video_name` | 视频名称 |
| `video_type` | 视频类型 |
| `tag_list` | 标签 |
| `upload_time` | 视频上传时间 |
| `stat_cost` | 整体消耗(元) |
| `prepay_pay_settle_1h` | 净成交ROI |
| `order_settle_amount_1h` | 净成交金额(元) |
| `refund_rate_1h` | 1小时内退款率 |
| `prepay_pay_order_count` | 整体成交ROI |
| `pay_gmv_include_coupon` | 整体成交金额(元) |
| `order_settle_rate_1h` | 净成交结算率 |
| `order_settle_count_1h` | 净成交订单数 |

**未出现在该表中的本地列**（如 `aadvid`、`cover_url`、`material_status` 等）**不会**出现在飞书写入 payload 中。

### 5.2 列顺序与主键

- 字典定义顺序即推送顺序；**「创建时间」在「素材ID」前**，以便 `ensure_headers` 将**第一列**对齐为「创建时间」（飞书首列常为文本主键）。
- `PMC_FEISHU_COLUMN_HEADERS` = 上述飞书列名按顺序组成的列表，供 `ensure_headers` 使用。

### 5.3 `map_pmc_material_row_to_feishu` 行为

- 仅输出映射表中的键；支持 `local_to_feishu` **合并覆盖**列名。
- `drop_none=True`（默认）时跳过值为 `None` 的列。
- **`video_type`**：通过 `VIDEO_TYPE_MAP` 转为中文（如 `2` →「自选投放视频」），再写入「视频类型」列。

### 5.4 字段类型转换：`bitable_types.coerce_value_for_field_type`

根据飞书 `ListAppTableField` 返回的 **type 数值**（如 TEXT=1、NUMBER=2、DATE=5…）对 Python 值做转换，避免类型不匹配。未知类型则原样透传。

---

## 6. 字符流程图（逻辑总览）

```text
[前端填写 app_token / personal_base_token / table_id]
              │
              ▼
[setFeishuBitableConfig → ServiceController 内存]
              │
              ▼
[每轮 fetch 快照传入 Fetcher._feishu_*]
              │
              ▼
[SQLite insert 成功]
              │
              ├─ 附带 created_at 组装 feishu_rows
              │
              ▼
[asyncio.to_thread → BitableTable.insert_pmc_material_rows]
              │
              ├─ ensure_headers：列名对齐 + 补缺列（文本类型）
              │
              ├─ map_pmc_material_row_to_feishu：英 → 中、video_type 中文
              │
              └─ FeishuBaseOperator.add_rows → batch_create
```

---

## 7. 错误处理与运维注意

- 飞书任一步失败：`logger.warning` / `logger.error`，**不抛回**给抓取主流程。
- `ensure_headers` 会改列名、建新列；若与人工在飞书后台调整的列类型不一致，可能导致 `coerce_value_for_field_type` 仍无法通过服务端校验——需保证列类型与数据含义一致（例如数字列应为数字类型）。
- 新建列默认为 **单行文本**；若需数字/日期等，应在飞书中手动改列类型，或扩展 `rebuild_headers` 中 `CreateAppTableField` 的 type（当前代码创建新字段时写死 `type(1)`）。

---

## 8. 与「飞书 Webhook 推送」的区别

| 维度 | 多维表格（本文） | 群机器人 Webhook |
|------|------------------|------------------|
| 配置位置 | 前端输入 → 内存；经 `setFeishuBitableConfig` | `data/feishu_webhook_push.json` |
| 触发时机 | SQLite 写入成功后，同批素材行 | 整点定时任务，读库拼表格 Markdown |
| 依赖 | `baseopensdk`、个人 Base token | `hook.feishu_bot.FeishuWebhook`，仅 HTTPS POST |
| 数据形态 | 结构化列 → Bitable 记录 | 文本/Markdown 消息 |

两者可并行启用，互不替代。

---

## 9. 相关源文件索引

| 路径 | 职责 |
|------|------|
| `services/fetcher.py` | `_save_to_database`、`_sync_batch_to_feishu`、token 注入 |
| `services/run_services.py` | `setFeishuBitableConfig`、`_snapshot_feishu_bitable`、传入 `fetch()` |
| `services/feishu_bitable/facade.py` | `BitableTable` |
| `services/feishu_bitable/base_table.py` | `FeishuBaseOperator`，字段/记录 API |
| `services/feishu_bitable/pmc_row_mapping.py` | 列名映射与 `VIDEO_TYPE_MAP` |
| `services/feishu_bitable/bitable_types.py` | 字段类型枚举与值转换 |
| `static/control.html` | 飞书多维表配置 UI 与 `setFeishuBitableConfig` 调用 |
| `pyproject.toml` | `baseopensdk` 依赖（wheel URL） |

---

*若后续调整映射表或表头策略，请同步更新本文与 `pmc_row_mapping.py` 注释。*
