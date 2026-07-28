# 测试1版本地联调

新版飞书确认追投默认使用桌面端本地长连接，不依赖本目录中的 PHP 卡片回调、临时隧道或 Cloudflare。用户应在桌面工具“飞书绑定”页面完成 App ID、App Secret、事件订阅和绑定码配置。

本目录中的 PHP 回调与 Cloudflare 脚本仅保留为旧模式兼容测试。运行隔离集成测试时仍会使用它们验证旧接口没有被新版改造破坏。

本目录只保存可提交的脚本和模板。PHP、MariaDB、临时隧道、数据库、Cookie、日志与飞书凭据全部安装或生成在：

`%LOCALAPPDATA%\qcsckp-test-runtime`

正式服务器配置不会被读取或修改。

## 使用顺序

1. `bootstrap-runtime.ps1` 下载并校验便携式 PHP、MariaDB 与 cloudflared。
2. `start-local.ps1` 启动 `127.0.0.1:8787` API、`127.0.0.1:3307` 数据库、回调白名单代理和本地过期任务。
3. `run-isolated-integration.ps1` 自动创建临时 `qcsckp_local_ci` 数据库和独立端口，在飞书模拟模式下验证签名、权限、幂等、过期、租约、卡片队列与版本接口；结束后自动清理。不要直接对正在使用的真实飞书测试库运行 `run-integration-tests.py`。
4. 需要接入真实飞书时，将 `%LOCALAPPDATA%\qcsckp-test-runtime\secrets.local.json` 的 `feishu_app` 改为真实测试应用配置，并把 `mock` 改为 `false`；随后使用 `start-local.ps1 -WithTunnel`。
5. 用 `set-test-target.ps1` 锁定唯一允许的 `aavid` 和素材 ID。
6. 普通联调用 `start-test-desktop.ps1`；脚本会先关闭上一份测试窗口，避免两个窗口抢同一任务。新版“规则化追投”页面会显示只读的“本地真实追投验收准备”清单。
7. 只有现场确认一次真实追投时才使用 `start-test-desktop.ps1 -ArmLiveRetarget`。脚本会先运行 `check-live-preflight.py`，白名单、策略、千川登录、设备令牌、广告 ID 映射或素材数据任一项不完整都会拒绝开启；授权领取一次后即被原子消费。
8. `stop-local.ps1` 只停止本地测试进程，不删除测试数据。

临时 HTTPS 隧道只转发 `/api/feishu/card_callback.php`。设备登录、任务创建、领取和结果回传接口仍只监听本机回环地址。
