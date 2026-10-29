# xhs_search

这个插件通过本机只读服务搜索小红书。使用二维码登录一次后，登录 Cookie
保存在小红书服务的受限目录；QQ 消息中不接收、不保存小红书 Cookie 或账号
token。插件只从 root 管理的本地文件读取服务 API token。

## 一次性配置

1. 在服务器上部署只监听 `127.0.0.1` 的小红书服务，并完成其本地 API token
   和 Cookie 存储配置。服务必须提供 `search`、`login_qr`、`login_status` 和
   `logout` 接口。
2. 由 root 写入 `/etc/xhs/api-token`，目录设为 `root:xhs-secret 0750`，文件
   设为 `root:xhs-secret 0640`，仅 AstrBot 和小红书服务用户加入该专用组。
   不要把文件加入仓库、日志、Prompt 或任务正文。
3. 在 AstrBot 配置中精确填写允许的 QQ UMO。`login_umos` 必须是管理员的
   QQ 私聊 UMO，`login_senders` 必须是管理员发送者 ID；两者均为空时登录
   被拒绝。搜索会话由 `allowed_umos` 控制，`allowed_senders` 可选进一步收窄。

示例（没有任何真实凭据）：

```json
{
  "allowed_umos": ["qq_official:GroupMessage:approved-group"],
  "allowed_senders": [],
  "login_umos": ["qq_official:FriendMessage:operator"],
  "login_senders": ["operator"],
  "endpoint": "http://127.0.0.1:18060",
  "api_token_file": "/etc/xhs/api-token",
  "timeout_seconds": 90,
  "max_results": 5
}
```

## QQ 命令

管理员在白名单私聊中发送 `/xhs login`，机器人回传二维码；用小红书 App
扫码后，发送 `/xhs status` 确认登录。发送 `/xhs logout` 可删除服务侧登录态。
在允许的群中使用 `/xhs search <关键词>`。登录命令只在精确的管理员私聊 UMO
中有效；群聊、非白名单发送者和其他平台均静默拒绝。

旧的 `/xhs auth <token>` 不再保存或处理消息中的 token，只会提示使用二维码
登录。插件拒绝命令不能删除 QQ 历史或框架预先记录的日志，因此不要发送真实凭据。

开启 `intent_router` 的 `light` 模式后，模型可以识别没有固定句式的“小红书
搜索”请求。路由器确认的 `xhs_search` 任务直接执行；其他自然语言请求也可由
主模型通过仅绑定当前消息的搜索工具识别。两条路径都由本插件执行会话白名单
和服务端登录校验；它不会执行发布、评论、点赞或收藏操作。
