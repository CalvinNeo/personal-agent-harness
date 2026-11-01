# 小红书搜索与扫码登录

搜索适配 [xiaohongshu-mcp](https://github.com/xpzouying/xiaohongshu-mcp) 的 REST API。
服务器生成本机服务的 API token；用户用小红书 App 扫码授权账号。Cookie 留在
独立的 xhs 服务目录，重启可继续使用。平台使登录态过期时仍需重新扫码，没有永久登录保证。

## 部署

1. 创建不能登录的 `xhs` 系统账号和 `xhs-secret` 组，把 `xhs` 和 `astrbot` 加入此组；不要把 Codex 执行账号加入。
2. 从固定 release `v2.5.5` 源码按 `patches/xhs-mcp-v2.5.5-login.build.md` 构建累计修复版本，核对原始及补丁后 manifest；在 `/opt/xiaohongshu-mcp/` 安装 Linux amd64 二进制，属主 root，权限 0755。不使用浮动 latest 自动升级，也不以未含修复的 release 二进制替代。
3. 创建 `/etc/xhs`，root:xhs-secret 0750。服务器本地随机生成 `/etc/xhs/api-token`，root:xhs-secret 0640。不要把 token 放到终端命令参数、QQ 消息、模型配置或 Git。
4. 创建 `/var/lib/xhs/data`，xhs:xhs 0700。安装 `deploy/xhs-mcp-start` 到 `/usr/local/libexec/`，root 管理；安装对应 systemd 单元。首次启动会准备浏览器，必须确认浏览器启动和依赖均正常。Chromium 需要临时回环调试端口，不能只允许进程绑定 API 端口。
5. 安装 `plugins/xhs_search/`，复制默认拒绝的 `xhs_search_config.json.example` 到 AstrBot 配置目录。精确填写搜索会话及管理员私聊白名单；同时满足 AstrBot 主白名单。私聊 `user_openid` 与群聊 `member_openid` 不应未经核验就互换。
6. 重启相关服务以载入组成员，检查 `18060` 只绑定 `127.0.0.1`，未携带 token 的 `/api/v1/login/status` 返回 401。不得在云安全组放行此端口。

开放扫码前，按 `patches/README.md` 应用固定 AstrBot 累计补丁，移除 QQ 图片上传失败日志中的完整 payload。历史部署也可用 `scripts/redact_qq_image_upload_log.py --check` 检查该项；新累计补丁已包含它。修复避免失败时记录二维码，但不能清除旧日志。

服务 API 本身还包含发布等操作，不能直接整体注册到模型的 MCP 工具列表。
本插件只提供只读搜索、正文原图读取，以及仅授权管理员私聊可用的状态工具；获取二维码和注销仍仅限管理员私聊命令。没有向 Codex
暴露 Cookie、API token 或任意 URL 请求能力。已获准群里的搜索结果会发送到该群。

## 使用与维护

- 授权私聊：`/xhs login` 获取二维码；扫码并在 App 确认后用 `/xhs status` 检查真实登录态。等待期间不要重复生成二维码或重启服务；“已扫码”不等于 Cookie 已落盘。
- 已授权群：`@机器人 /xhs search 关键词`，或自然语言要求搜索小红书。
- 授权私聊：`/xhs logout` 删除服务器登录态。
- `systemctl start|stop|restart xhs-mcp.service` 控制独立服务；AstrBot 插件由 `astrbot.service` 管理。

## 正文与带图总结

`xhs_search` 搜索标题和笔记引用；`xhs_read_notes` 读取正文与校验后的原帖图片。若返回 `references_ready`，必须按 `next_arguments` 再次调用读取工具，不能用搜索标题编写正文总结。最多读 3 篇、附 4 张原图，默认请求 90 秒、正文整体预算 105 秒。图片以 QQ 原生图片组件发送；不拿外站图片替代原图，不用 Markdown 图片 URL 冒充发送成功。

查询引用按当前会话与发送者绑定，不能从其他用户复用访问参数。服务繁忙、限流、正文不可读、图片不可用均明确返回，不绕过验证。等待扫码时的“再看下”只查询原会话，检查失败不武断认定未扫码。不需要 Computer Use、截图权限或让 Codex 读取 Cookie。

本插件不要求、接收或保存用户通过 QQ 发送的 token。即使插件拒绝 `/xhs auth`，
QQ/框架也可能已经记录该消息，因此不要发送真实凭据。日志审计只分享脱敏后的
错误类别、服务状态和数量；上游原始日志也可能包含搜索词。

凭据与 Cookie 不进仓库。备份时保留 0700/0600 权限；撤销账号授权或 Cookie
失效后须重扫。修改 API token 后同步重启上游服务；插件每次请求重新读取文件。
