# 千川工具生产版 V1A

V1A 是独立于 v0.1.46 的生产重构开发线，当前边界是“可信只读采集 + 规则模拟”。开发分支为 `生产版-V1A`，开发前基线标签为 `生产版-V1A-开发前-v0.1.46`。

## 安全边界

- 不注册任何真实千川追投、暂停、预算或时长写接口。
- 平台请求经过只读白名单守卫；越权请求会落关键安全事件并立即失败。四类适配器自身也会直接拒绝写方法。
- `execution_task` 数据库触发器只允许 `dry_run_*` 或 `archived_readonly` 状态。
- 运行数据使用 `%LOCALAPPDATA%\QCSCKP\production-v1a`，不会直接读写旧版运行目录。

## 已实现的生产底座

- pywebview 桌面壳、React + Vite 主流程、后台作业服务和唯一 Chrome Browser Worker。
- 随机本机端口、单次启动令牌、管理员会话与 SSE 任务进度。
- Windows 用户范围全局互斥量；第二份安装目录只能唤醒已运行实例。
- 当前开发阶段继续使用本机管理员门禁；中心工具账号登录按用户要求暂停，远程模式默认不启用。
- 本机管理员、PBKDF2 密码、一次性离线恢复码、DPAPI 千川会话、飞书 App ID 和 App Secret。
- 带版本的 SQLite schema、WAL、外键、busy timeout、单写入线程、短事务、持久化作业、租约和 fencing token。
- 四类显式适配器、分页完整性、异常空保护、视频正向证据、结构指纹和 Scene 1/2/3 只读任务。
- 业务导航拆分为运行健康、千川账户、飞书绑定、追投策略、停投策略、候选与任务中心、账户操作流水、诊断与恢复。
- 账户页完成账户启停、账户级飞书路由、四类计划选择、原子保存与单计划立即采集；不再设置重复的“监控计划”主导航。
- 追投支持素材级/商品级今日累计规则；停投只针对 Scene 2 素材追投任务。两类策略均支持草稿、优先级仲裁和启停。
- 冻结候选支持单条、多选、全部一组、逐条分别、多组与跨组重复，每组最多20条。
- 飞书长连接五段健康、持久化 Inbox/Outbox、事件去重、绑定码、分页模拟卡和同步更新。
- 平台操作流水、账户/操作人/动作/结果/关键词筛选、中文 CSV、月度历史库、真实/模拟双段日报；默认只显示平台日志，浏览器轨迹不计入日报。
- 关闭主窗口时进入系统托盘；“完全退出”才停止后台采集和飞书连接。
- 人工选择单一 v0.1.46 数据源、迁移前快照、迁移报告和启动时一键恢复。

## 本地接口

查询接口包括 `/api/v1/health`、`accounts`、`plans`、`collections`、`control-tasks`、`strategies`、`candidates`、`adjustment-candidates`、`feishu/status`、`operation-events`、`migrations`、`capabilities` 与 `adapter-evidence`。除管理员建号/登录和本机健康外，命令均进入持久化后台作业并返回 `job_uid`。

## 开发运行

```powershell
cd D:\项目开发\召伟工具\qcsckp\qcsckp-desktop
.\.venv\Scripts\python.exe .\desktop_v1a.py
```

React 页面位于 `production_v1a_frontend`。修改页面后运行：

```powershell
cd .\production_v1a_frontend
npm install
npm run build
```

## 验收边界

自动化测试和开发运行不得产生真实千川写操作。当前 269 项自动化与旧版回归已经通过，但以下 M3/M6 现场验收仍需用户指定样本，不能用模拟结果冒充：

- 全域推商品、全域推直播、乘方推商品、乘方推直播各一条真实计划的请求契约与分页证据。
- 至少三个真实 `aavid` 的只读轮询、昨天数据回补契约与平台限流验证。
- Windows 睡眠恢复、跨日和 72 小时长稳数据。

这些证据通过前，全域适配器和全部真实写能力继续保持 `unobserved`、`blocked_by_evidence` 或 `dry_run_ready`，不创建完成标签，也不制作安装包。
