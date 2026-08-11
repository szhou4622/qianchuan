# 千川官方 API 后端联调与切换说明

## 当前状态

- v0.1.46 前端、飞书、规则、任务中心、流水、日报和本地 SQLite 结构保持兼容。
- 官方 API 代码已接入，但开发环境默认仍为 `browser_legacy`。
- 真实 API 写入默认关闭；没有完成受控验收前不得打开。
- App ID、App Secret 和 OAuth 页面不在本阶段范围内，联调只通过可注入令牌提供器完成。

## 联调环境变量

开发联调进程可设置：

```text
QCSCKP_QIANCHUAN_BACKEND=official_api
QCSCKP_OE_ACCESS_TOKEN=<临时联调令牌>
QCSCKP_ALLOW_LIVE_API_WRITES=0
```

令牌不得写入代码、日志、测试、提交记录或诊断包。正式 OAuth 接入后，令牌只通过 Windows DPAPI 密文保存。

## 只读验收门

一次性切换前必须至少完成：

1. 三个真实 `aavid` 的授权链、账户名称和账户隔离对账。
2. 乘方推直播、乘方推商品、全域推直播、全域推商品四类计划对账。
3. 全部分页、计划详情、视频素材、商品关系、报表单位、调控任务和操作日志对账。
4. Token 过期、权限不足、429、网络中断、重复页和异常空页测试。
5. 飞书、CSV、日报和现有前端回归。

任一类别不完整时，目录标记为不完整并保留上次完整数据；不得切换正式运行。

## 受控写入验收

只在用户指定账户、计划、素材、预算和时长后，临时设置：

```text
QCSCKP_ALLOW_LIVE_API_WRITES=1
```

验证范围仅限素材追投调控任务：创建 `MATERIAL_ADD_BUDGET`、`PAUSE`、`DISABLE`、修改调控预算和投放时长。禁止 `DELETE`，也不提供主计划创建、编辑、启停、预算或 ROI 修改。

POST 不盲目重试。网络超时或 5xx 导致结果未知时，只能查询调控任务和操作日志对账；确认失败后由人工处理。

## 正式切换和回滚

正式切换只设置一次 `QCSCKP_QIANCHUAN_BACKEND=official_api`，不做单任务浏览器回退。官方模式不导入或启动 Playwright、Fetcher 和旧 Browser Worker。

API 改造前代码标签：

```text
生产版-V1A-API改造前-20260811
```

本机数据快照：

```text
D:\项目开发\召伟工具\qcsckp\qcsckp-desktop\data\rollback\official-api-pre-20260811-181948
```

切换失败时整体恢复代码标签和数据库快照，不能在运行中让同一任务走 API 与浏览器双通道。
