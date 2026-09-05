# 两轮审查修复记录（2026-09-05）

基线：`2c2615a7f088cab558f0a7c457e00df90110249d`。本次只修改本地源码并进行隔离验收，不构建、不发布、不替换正在运行的正式版。

## 修复与验收对应

| # | 问题 | 修改后的行为 | 主要回归证据 |
|---|---|---|---|
| 1 | 缺失指标沿用旧值并伪装为新鲜数据 | 本轮缺失保持 NULL；禁止跨统计日制造新鲜值；单素材表格、曲线、详情保留空值；无有效基线不伪造流速0 | `test_official_collection_freshness`、`test_dashboard_optimization`、`test_dashboard_null_metrics` |
| 2 | 停投限流等待后沿用旧授权重复提交 | 每次确认仅提交一次；慢速只读预检后、POST前再复核策略、归属、指标与周期 | `test_stop_pre_submit_safety`、`test_stop_rate_limit_does_not_retry_without_new_confirmation` |
| 3 | 停投策略变化后旧卡阻挡新卡 | 当前账号 pending/approved_queued 旧卡按策略哈希失效并释放卡片去重；已领取任务提交前仍复核；不清共享执行意图 | `test_execution_recovery`、`test_stop_pre_submit_safety` |
| 4 | 数据库锁使操作日志线程退出 | 工作器按轮隔离异常、恢复领取/进度/完成流程；并发恢复有互斥保护 | `test_official_operation_logs` |
| 5 | 操作日志无界重试、重复扫全天、失败被隐藏 | 单窗口参数/分页异常最多3次、网络异常最多8次；不重置退避；按对象水位重叠采集；无效对象不阻塞；完整覆盖后消解旧失败并保留证据 | `test_official_operation_logs` |
| 6 | 昨日补采失败后不再重试 | 独立持久化待补日期；今日成功不清除失败补采；补齐后只移除成功日期 | `test_official_collection_freshness` |
| 7 | 单组重试成功仍算整体失败 | 按逻辑分组聚合有效尝试，明确失败被安全重试替代后不污染成功结果 | `test_execution_recovery` |
| 8 | 对账早于流水/卡片回报，结果永久卡住 | 对账终态附带可重放本地投影；晚插流水、晚回报主动补齐，后台兜底；旧停投投影不覆盖更新的恢复观测 | `test_execution_recovery` |
| 9 | 停投自然到期、未知结果无法正确结束 | 完整支持 naturally_expired、unknown_requires_review、invalidated；中文状态、对应颜色、移除执行按钮 | `test_execution_recovery`、`test_stop_strategy_account_scope` |
| 10 | 飞书旧队列覆盖成功卡 | 直发/排队重试按消息串行，重建最新卡并淘汰旧更新；自动停投通知按执行和接收目标排序，终态抑制旧提交通知 | `test_execution_recovery` |
| 11 | Windows更新脚本变量、中文路径与ready判断 | 避免只读 HOME 变量、JSON显式UTF-8；校验本次启动时间/PID/版本/渠道/构建号；失败恢复旧文件 | `test_windows_updater_ready`、`test_release_channels` |
| 12 | 日志初始化、轮转与脱敏诊断缺口 | 已有handler不再抑制落盘；真实保留30份轮转；凭据/目录脱敏，栈保留模块与行号；诊断异常不遮蔽启动错误 | `test_runtime_logging`、`test_startup_bootstrap`、`test_failure_report` |

## 验证方式

```powershell
python tools/run_isolated_tests.py
```

入口在导入业务代码前设置临时 `QCSCKP_HOME`、`QCSCKP_DATA_DIR`，默认禁止真实写入，并从 socket 层阻断非回环连接。测试可使用本机回环假服务器；千川写入和飞书发送使用 mock。运行结束关闭日志句柄并清理该入口创建的临时目录。

前端测试使用 Node.js 执行当前 HTML 的实际函数及语法校验，DOM/ECharts 为测试替身。Windows更新测试在临时中文路径使用原生 PowerShell 5.1，所有进程操作均替换为测试桩，不启动或结束用户软件。

最终全量结果：**886项测试全部通过**，耗时194.809秒；退出码0，无跳过。验证日期为2026-09-05。

- 173个Python文件通过AST语法检查，`git diff --check`通过。
- 实际Node前端执行回归和原生PowerShell 5.1中文路径/回滚回归包含在上述全量中。
- 191个Python/PowerShell/HTML文件在测试开始和结束时的联合SHA256一致：`b4555b44439d4e98a5a3340afae5ba05e9c0e379ad14d31f0c57516392d24a07`。
- 先前并行开发中的首轮全量曾因进程加载了轮转修复前的模块而失败；最终验收使用冻结后的全部代码重新运行，未用专项结果替代全量。

## 边界与兼容

- 不修改数据库表结构、策略阈值或300秒调度周期；不清除账号、授权、策略和历史操作流水。
- 重试上限按持久化日志窗口计算；限流继续遵守退避，不声称整个账户此后不再请求。后续窗口仍可增量采集；未覆盖失败保持可见。
- 元数据保留/覆盖整理只针对调度窗口，不删除官方操作事件；聚合总计口径不变。
- 可重放证据从本次代码产生的终态起保存；不补造旧证据，不主动重发已结束的历史飞书卡片。
- 手动试验留下的少量隔离数据目录，因工具策略拒绝清理而保留；不含生产凭据，不属于源码或分发产物。统一测试入口产生的目录正常自动清理。
