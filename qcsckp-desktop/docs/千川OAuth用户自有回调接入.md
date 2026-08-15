# 千川 OAuth 用户自有回调接入

桌面工具只要求用户填写自己的 App ID 和 App Secret，不要求再次填写公网回调地址。

## 工作方式

1. 用户在巨量引擎开放平台为自己的应用登记一个公网 HTTPS 回调地址，例如 `https://example.workers.dev/callback`。
2. 工具打开官方授权页时只提交 App ID、随机 `state` 和授权范围。
3. 千川按 App ID 找到应用后台已经登记的回调地址，并把 `auth_code`、`state` 等参数发送过去。
4. 工具启动一个独立、可见的本机 Google Chrome 授权窗口，并监听这个窗口的导航。
5. 浏览器准备进入应用后台登记的回调地址时，工具从该次导航中取得 `auth_code` 和 `state`，校验随机 `state` 后在本机换取令牌并关闭授权窗口。

App Secret、access token 和 refresh token 都不会发给公网回调服务。

## 约束

- 授权必须在运行桌面工具的同一台 Windows 电脑及工具启动的 Chrome 窗口中完成。
- 开放平台仍登记用户自己应用的公网 HTTPS 回调地址；工具不读取、覆盖或写死它。
- 回调服务无需向本机端口转交参数，也无需提供 `/oauth/session` 或 `/oauth/result`。
- App Secret、access token、refresh token 和一次性 auth_code 都不发送给开发者服务。
