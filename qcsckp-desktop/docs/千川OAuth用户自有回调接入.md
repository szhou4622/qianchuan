# 千川 OAuth 用户自有回调接入

桌面工具只要求用户填写自己的 App ID 和 App Secret，不要求再次填写公网回调地址。

## 工作方式

1. 用户在巨量引擎开放平台为自己的应用登记一个公网 HTTPS 回调地址，例如 `https://example.workers.dev/callback`。
2. 工具打开官方授权页时只提交 App ID、随机 `state` 和授权范围。
3. 千川按 App ID 找到应用后台已经登记的回调地址，并把 `auth_code`、`state` 等参数发送过去。
4. 用户的公网回调服务保留原始查询参数，立即 302 跳转到 `http://127.0.0.1:17658/callback`。
5. 运行中的桌面工具只监听本机环回地址，校验随机 `state` 后，使用本机 DPAPI 加密保存的 App Secret 换取令牌。

App Secret、access token 和 refresh token 都不会发给公网回调服务。

## Cloudflare Worker 最小示例

```js
export default {
  async fetch(request) {
    const incoming = new URL(request.url);
    if (incoming.pathname !== "/callback") {
      return new Response("Not Found", { status: 404 });
    }

    const authCode = incoming.searchParams.get("auth_code") || "";
    const state = incoming.searchParams.get("state") || "";
    if (!authCode || !state) {
      return new Response("Missing auth_code or state", { status: 400 });
    }

    const local = new URL("http://127.0.0.1:17658/callback");
    for (const [key, value] of incoming.searchParams) {
      local.searchParams.append(key, value);
    }
    return Response.redirect(local.toString(), 302);
  },
};
```

## 约束

- 授权必须在运行桌面工具的同一台 Windows 电脑上完成。
- 开放平台中仍填写用户自己的公网 HTTPS 地址，不能把 `127.0.0.1` 直接登记为开放平台回调。
- 公网回调只负责转交查询参数，不交换令牌，不保存 App Secret，不需要 `/oauth/session` 或 `/oauth/result`。
- 回调服务不得修改、丢弃或自行生成 `state`。
- 如果本机 17658 端口被其他程序占用，工具会明确提示关闭占用程序后重试。
