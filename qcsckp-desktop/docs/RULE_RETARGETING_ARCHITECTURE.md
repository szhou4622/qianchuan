# 规则化追投机制 — 流程与架构

本文描述「按 `rule_retargeting.json` 自动监测大屏指标、满足条件则发起千川调控追投」的**端到端架构**，与仓库实现一致。字符图便于在终端或任意 Markdown 预览中阅读。

更宏观的桌面壳层、采集与 SQLite 总览见 [SYSTEM_ARCHITECTURE.md](./SYSTEM_ARCHITECTURE.md)。追投流水表字段见 [schema_pmc_retargeting_run.sqlite.sql](./schema_pmc_retargeting_run.sqlite.sql)。

---

## 一、在系统中的位置

```
┌─────────────────────────────────────────────────────────────────────────┐
│  配置层  data/rule_retargeting.json                                      │
│  读写 / 校验 / 规范化  api/rule_retargeting_config.py                    │
│  前端「追投配置 / 追投记录」 static/rule_retargeting.html + components │
└───────────────────────────────┬─────────────────────────────────────────┘
                                │ load_rule_retargeting_config()
                                ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  调度层  services/retargeting_rule_runner.py                             │
│  · 固定间隔轮询（默认 180s，见下文常量）                                  │
│  · 每轮：拉大屏素材 → 按策略筛素材 → 限频 → Playwright 追投 → 写流水        │
└───────────────────────────────┬─────────────────────────────────────────┘
                                │
          ┌─────────────────────┼─────────────────────┐
          ▼                     ▼                     ▼
  DashboardApi          QianChuanRetargetingService   SQLiteStore
  get_table_data        services/retargeting_service  pmc_* 表
  （与看盘同源指标行）    （打开千川页、填表、提交/仅准备）
```

**与「即刻追投」的边界**：即刻追投由 GUI 调用 `api/retargeting_runs.run_immediate_retarget_prepare`，不跑触发条件，但复用同一套 `retargeting` 参数模板与限频表逻辑；流水里 `trigger_source=manual`。规则化追投由调度器写入，`trigger_source` 为调度侧约定值（如 `scheduler`），并带完整 `trigger_snapshot_json` / `query_snapshot_json`。

---

## 二、配置要点（`rule_retargeting.json`）

| 概念 | 说明 |
|------|------|
| 根级 `enabled` | `false` 时调度器每轮快速返回，不拉大屏、不执行策略。 |
| `strategies[]` | 多策略：每项含 `id`、`title`、`trigger`、`retargeting`。旧版仅根级 `trigger`/`retargeting` 会在加载时规范化为单条策略。 |
| `trigger` | 条件组与组间关系：`group_combine`（如 `or`）+ `groups[]`（组内 `join` + `conditions[]`），指标与运算符见 `rule_retargeting_config.py` 中 `ALLOWED_*`。 |
| `trigger_query_period` | 传给大屏的统计周期（如 `1h`），与 `DashboardApi.get_table_data(period=...)` 一致。 |
| 限频 `interval` | **全策略共用**：优先根级 `interval`，否则取首条策略的 `retargeting.interval`。窗口秒数 + 窗口内最大成功次数；状态在 `pmc_retargeting_rate_limit`（按 `material_id`）。 |
| `browser_headless` | 规则追投实际是否无头，与流水字段 `headless` / `browser_headless_rule` 对照。 |

持久化路径：`config.DATA_DIR` 下的 `rule_retargeting.json`（与 `control_panel.json` 独立）。

---

## 三、调度入口

```
  gui_app 启动
        │
        └──► start_retargeting_rule_runner_background_thread()
                  │
                  └── 守护线程 _gui_background_target()
                        └── asyncio.run(main_loop())
                              │
                              └── 循环：await run_one_cycle(db) → sleep(interval_sec)
```

- **GUI**：`gui_app.py` 在初始化流程中启动上述后台线程（与主窗口并存，不阻塞 UI）。
- **独立进程调试**：核心为 `asyncio.run(main_loop())`；可在项目根执行  
  `python -c "import asyncio; from services.retargeting_rule_runner import main_loop; asyncio.run(main_loop())"`  
  与 GUI 后台线程逻辑一致（当前模块未必提供 `__main__` 入口，以实际文件为准）。

---

## 四、单轮 `run_one_cycle` 逻辑（规则化）

```
  run_one_cycle(db)
        │
        ├── load_rule_retargeting_config()
        ├── 若 not enabled → return
        ├── 解析 strategies（无则退化为单条「策略 1」+ 根级 trigger）
        ├── rule_full_json = 整份配置快照（字符串）
        ├── ws, mc = 全局限频窗口与上限
        │
        ├── DashboardApi().get_table_data(period, sort_by=costDiff, page_size 极大)
        │         └── rows = 当前周期内素材行（与看盘表格同源）
        │
        └── asyncio.gather( process_strategy(st) for st in strategies )
                    │
                    └── 并行路数受 asyncio.Semaphore 限制（常量 `MAX_STRATEGY_PARALLEL`，默认 5）
```

**每条策略 `process_strategy` 内部**（顺序对理解很重要）：

```
  对 rows 逐行 evaluate_trigger(trigger, row)
        └── 得到 hit_rows（本策略命中素材）

  若 hit_rows 为空 → return

  为本策略创建 QianChuanRetargetingService.from_rule_file_dict(cfg)

  对 hit_rows 中每个 row（素材）：
        │
        ├── 构建 trigger_snapshot_json（含 strategy_id/title、trigger_config、evaluation）
        ├── 构建 query_snapshot_json（周期、query_at、material_row 等）
        │
        ├── 按 material_id 取 asyncio.Lock（多策略并行时串行化「限频 → 执行 → 记次」）
        │
        ├── rate_limit_should_skip(...) → true 则跳过（不写成功流水、不记成功次数）
        │
        ├── resolve_ad_id_for_aavid(db, aavid)  ← pmc_ad_detail_basic
        │         └── 失败则 _insert_run 失败流水并 continue
        │
        ├── aavid/ad_id 合法则 await svc.run(...)
        │         └── 成功/失败均 _insert_run → pmc_retargeting_run（含 strategy_name）
        │
        └── 仅当 result.success：rate_limit_record_success（更新限频表）
```

**要点**：

- **指标口径**：触发判断与「素材数据」行字段一致，来自 `get_table_data`，与前端看盘对齐。
- **多策略**：策略间并行、策略内对命中素材顺序执行；同一 `material_id` 用锁避免竞态超频。
- **流水**：`pmc_retargeting_run` 记录每次尝试（成功/失败），`strategy_name` 等见表结构说明。

---

## 五、执行层（Playwright）

`QianChuanRetargetingService`（`services/retargeting_service.py`）负责：

- 使用本地登录态（如 `qcookie.json` 等，与采集服务一致思路）打开千川投放页；
- 按 `retargeting.method`（`volume` / `cost_control`）及子参数填表并提交；
- 返回 `RetargetingRunResult`（步骤、消息、调控任务 id 等），由 `retargeting_rule_runner` 转为 `pmc_retargeting_run` 行。

规则化追投在 `svc.run(..., strategy_title=st_label)` 中传入策略标题，便于日志与平台侧展示。

---

## 六、数据表与职责

| 表 | 用途 |
|----|------|
| `pmc_promotion_material` | 采集入库的素材行；大屏行由此 + 窗口 SQL 聚合而来。 |
| `pmc_ad_detail_basic` | `aadvid` → `ad_id` 映射，追投前必须能解析。 |
| `pmc_retargeting_run` | 每次规则/手动追投尝试一条流水；JSON 快照字段用于复盘。 |
| `pmc_retargeting_rate_limit` | 每素材一行：窗口起点、当前成功次数；与根 `interval` 解释一致。 |

---

## 七、前端与 API（pywebview）

- **配置页**：`static/rule_retargeting.html` 编辑并保存 `rule_retargeting.json`（经 `api/rule_retargeting_config` 与校验）。
- **追投记录**：`static/components/rule_retargeting_runs.html` 列表/详情；`api/retargeting_runs.py` 提供分页查询与单条详情、`getRetargetingRunDetail` 等供 `api/views.py` 暴露给 `window.pywebview.api`。

---

## 八、常量（实现以代码为准）

| 项 | 说明 |
|----|------|
| `DEFAULT_INTERVAL_SEC`（默认 180） | `main_loop` 两轮之间 `sleep` 的秒数。 |
| `MAX_STRATEGY_PARALLEL`（默认 5） | 同轮内多策略并行上限（`asyncio.Semaphore`）。 |

模块注释中可能提到环境变量名；**是否读取 `os.environ` 以 `retargeting_rule_runner.py` 实际代码为准**（未实现则仅常量生效）。

---

## 九、源码索引

| 模块 | 路径 |
|------|------|
| 规则调度主循环 | `services/retargeting_rule_runner.py` |
| 配置加载与触发求值 | `api/rule_retargeting_config.py` |
| 大屏数据 | `api/dashboard.py`（`DashboardApi.get_table_data`） |
| 浏览器追投 | `services/retargeting_service.py` |
| 即刻追投与流水查询 | `api/retargeting_runs.py` |
| SQLite 表结构 | `utils/sqlite_store.py`（`TABLE_SCHEMAS`） |
| GUI 入口 | `gui_app.py`（`start_retargeting_rule_runner_background_thread`） |

---

*若模块或环境变量名变更，请同步更新本文件与 [SYSTEM_ARCHITECTURE.md](./SYSTEM_ARCHITECTURE.md)。*
