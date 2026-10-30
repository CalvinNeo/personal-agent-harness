# Personal Agent Harness

基于 AstrBot 的私人 QQ 智能体定制集合：官方 QQ 接入、自定义 OpenAI 兼容模型、公开网页搜索与读取、隔离 Codex 任务、长期记忆、自然语言提醒，以及跨消息图片引用。

本仓库保存可复用源码、补丁、配置模板、测试和运维文档。部署地址、QQ 身份、模型提供商选择、密钥、聊天记录和运行数据均不在仓库中。模板默认关闭 QQ 和模型、拒绝所有会话；需要在自己的服务器本地填写配置并明确放行。

## 已整理的修改

| 内容 | 目录 | 关键行为 |
| --- | --- | --- |
| QQ → Codex 桥接 | `plugins/codex_bridge/` | 显式 `/codex`、自然语言和复杂任务路由、群/发送者白名单、写权限单独授权、持久状态看板 |
| 独立任务执行和模型代理 | `services/` | Unix socket 调用方 UID 检查、专用账号、Codex 沙箱、任务超时、真实模型密钥只由代理持有 |
| Codex 会话续接 | `services/conversations.py`、`plugins/codex_bridge/` | 引用已发送的 Codex 回复可续接同一个原生 session，并保留原图；失败时不会静默新建会话 |
| Codex 公开网页工具 | `services/public_web.py`、`services/public_web_mcp.py` | stdio MCP 搜索及 GET 读取公开网页，校验 DNS 与跳转；另已开放任务命令联网 |
| 长期记忆 | `plugins/long_term_memory/` | 明确自然语言增删改查、SQLite 持久化、同群同发送者隔离；相关条目供普通聊天和 Codex 参考 |
| 自然语言提醒 | `plugins/reminder/` | “两分钟后提醒我喝水”、北京时间、原子持久化、一次性调度、Markdown 真正 `@`、重试、列表/取消 |
| 轻量意图路由 | `plugins/intent_router/` | `regex` 保持现有路径；`light` 用可配置的兼容模型以 `reasoning_effort=low` 先做静态路由，失败回退 |
| 小红书只读搜索 | `plugins/xhs_search/` | 白名单群搜索、light 路由和模型工具；管理员私聊扫码一次，登录态保留在独立服务目录 |
| 图片引用 | `plugins/image_context/` | “上一张图”“刚发的那几张图”、同群同发送者、30 分钟/12 张上限、缺图提示重发 |
| AstrBot 小补丁 | `patches/` | AnySearch `global → intl`；限流日志不再输出 API Key 前缀 |
| 部署与访问限制 | `deploy/`、`scripts/` | 管理端仅 loopback、nftables、systemd 限权、初始随机密码、环境变量密钥、白名单验证 |

详细清单见 [CUSTOMIZATIONS](docs/CUSTOMIZATIONS.md)，启停和审计见 [OPERATIONS](docs/OPERATIONS.md)。提醒与图片功能直接由 AstrBot 执行；复杂任务默认保留显式 `/codex`，切到 `light` 后也可由路由器交给现有 Codex 状态插件。

## 版本基线

- Linux、systemd、nftables、Python 3.12。
- AstrBot `4.29.3`；QQ SDK `qq-botpy 1.2.1` 未修改。
- 已验证的 Codex CLI：`0.162.1`。
- 公开网页 MCP 使用 Python `mcp==1.30.0`，安装在 `/opt/astrbot/venv`；不需要额外搜索密钥。
- 模型供应商需要支持 OpenAI 兼容 Chat Completions；Codex 还需要 Responses API。看图和工具调用须由所选模型支持。

仓库的移植改动包括：删除默认真实群 ID、改用本机账号解析 UID、模型和上游地址使用环境变量、统一代理 token 路径、将 systemd drop-in 合入模板。它们没有自动覆盖当前线上部署。

## 新服务器部署参考

先安装 Python venv、Node.js/npm、nftables 和 patch。以下步骤针对新安装，已有服务应先备份并按文档逐项迁移。

1. 创建不能登录的系统账号 `astrbot`、`codexrunner`、`codexproxy`，把 `astrbot` 加入 `codexrunner` 组，使它能连接 runner 的 Unix socket。不要把 runner 加入 `astrbot` 或 `codexproxy` 组。
2. root 管理程序目录 `/opt/astrbot` 与 `/opt/astrbot-codex-runner`，在 `/opt/astrbot/venv` 创建 Python 3.12 环境：

   ```bash
   /opt/astrbot/venv/bin/pip install -r requirements.txt
   npm install -g @openai/codex@0.162.1
   python3 scripts/apply_patches.py --site-packages /opt/astrbot/venv/lib/python3.12/site-packages
   ```

   服务模板假设 Codex 位于 `/usr/local/bin/codex`，安装后检查实际路径。

3. 创建 `/var/lib/astrbot`、`/var/lib/codexrunner/{workspaces,jobs,codex}`，分别归对应服务账号所有；创建 `/var/lib/codexproxy` 归 root 管理。状态目录用 `0700`，密钥目录 `/var/lib/codexproxy` 用 `root:codexproxy 0750`。
4. 初始化拒绝所有会话的配置：

   ```bash
   sudo python3 scripts/initialize_private.py --enable-search \
     --api-base https://api.example.com/v1 --model REPLACE_WITH_MODEL_NAME
   ```

   替换示例地址和模型。脚本拒绝覆盖已有配置，随机初始密码只写入 `/etc/astrbot/admin-initial-password`（root 可读）。QQ 和模型保持关闭，直到你本地配置完成。

5. 将 `plugins/` 下的子目录复制到 `/var/lib/astrbot/data/plugins/`；将对应 `deploy/examples/*_config.json.example` 复制到 `/var/lib/astrbot/data/config/` 并去掉 `.example`。属主 `astrbot:astrbot`，目录 `0700`，配置 `0600`。复制 `services/*.py` 到 `/opt/astrbot-codex-runner/`，由 root 管理；持久 Codex runner 还需要 root 管理的 `codex-bwrap` 隔离启动器，路径由 `HARNESS_BWRAP` 指定。
   `intent_router` 默认使用 `regex`。要启用轻量路由，单独配置并启用 `planner-minimax-light` provider（示例为 MiniMax M3.1 Flash，`custom_extra_body.reasoning_effort=low`、`max_tokens=512`），确认 canary 后把 `intent_router_config.json` 的 `mode` 改为 `light`。规划器只返回固定路由，提醒/Codex/图片/搜索插件仍做最终校验。
6. 在服务器本地复制并填写 `deploy/examples/` 中的环境文件。`model.env` 放 `/etc/astrbot/`、`root:astrbot 0640`；`runner.env` 和 `proxy.env` 为 root 管理的 `0600` 文件。真实代理 API key 单独存 `/var/lib/codexproxy/upstream-token`，`root:codexproxy 0640`。不要把密钥作为命令行参数或发到 Git。
7. 将 `codex-config.toml.example` 复制到 `/var/lib/codexrunner/codex/config.toml`（`root:codexrunner 0640`）。该文件只指向本机模型代理，并配置 `public_web` stdio MCP，不保存真实密钥。确认 venv 已安装 `mcp==1.30.0`，且 `public_web.py`、`public_web_mcp.py` 为 root 管理的代码。保持 `web_search="disabled"`；`default_permissions` 位于 TOML 根级，`network_proxy=true` 和 `codex-net-read`/`codex-net-write` profile 为命令网络启用 `mode="full"`、域名 `*` 放行。只读任务不能写会话工作区或 `.tmp`；工作区写入任务可写分配的会话工作区和 `.tmp`。有本次或保留图片输入的任务会自动切到工作区写入 profile，以便生成图片结果。
8. 安装 `deploy/systemd/*.service` 到 `/etc/systemd/system/`、`deploy/firewall.nft` 到 `/etc/astrbot/`，执行 `systemctl daemon-reload`，然后按运维文档启用服务。通过 SSH 隧道进入管理页面，在本地填写 QQ AppID/AppSecret、启用模型和 QQ 适配器。
9. 把确认后的完整群 UMO 放入 AstrBot 主白名单和需要启用的插件 `allowed_umos`。AstrBot **主白名单为空会放行所有会话**，初始占位值应一直保留到替换为真实允许列表；各插件的空列表则拒绝所有会话。不要开启管理员绕过。

10. 如启用小红书搜索，按 [小红书部署说明](docs/XHS.md) 单独部署 `xpzouying/xiaohongshu-mcp`，保持回环监听和独立账号。API token 在服务器生成并保存；管理员只需在授权私聊发送 `/xhs login` 获取二维码，扫码后 Cookie 持久保留。不在 QQ 发送 Token 或 Cookie：框架在插件执行前就可能记录原始消息。`login_umos` 和 `login_senders` 必须同时配置为已核验的私聊身份，不能直接把群内 OpenID 当作私聊身份。

11. 如启用长期记忆，把 `long_term_memory_config.json.example` 复制为对应私有配置，并按同一允许范围配置 `allowed_umos`、`allowed_senders`。记忆目录应为 `astrbot:astrbot 0700`，数据库及 SQLite 辅助文件为 `0600`。空会话白名单拒绝读写；不要复制其他群或用户的记忆数据。更新代码时保留数据库。

QQ 凭据保存在服务器上的受限主配置中；模型密钥经环境变量引用。这些文件属于运行环境，绝不能复制回仓库。

## 公开网页与长期记忆

在获准群中 @机器人后，可以发送 `/codex 搜索 Python 官方文档并给出来源`，或 `/codex 读取 https://example.com 并概括内容`。Codex 通过 `search_public_web` 和 `read_public_web` 调用专用工具；搜索用 Bing RSS，读取只支持无需登录的 HTTP/HTTPS 文本页面，不运行网页 JavaScript、不接收 Cookie 或请求体。内网、回环、IP 字面量、非标准端口和含凭据的 URL 会被拒绝，重定向也逐次校验。

上述 GET 和目标地址限制由网页 MCP 执行。Codex 命令也已获准联网，其网络 profile 放行所有域名，不具备网页 MCP 相同的公开地址校验和 GET 限制。文件写入权限由任务模式独立控制。`allow_local_binding=false` 是 Codex 网络代理的本地/私网访问选项，不应只理解为控制监听端口；当前 profile 未配置精确的 localhost 或私网 IP 放行规则。

长期记忆不需要 Codex。可发送“记住我希望回答简洁一点”“把关于饮料的记忆改成我喜欢咖啡”“忘记我喜欢喝茶这条记忆”“查看我的长期记忆”或“清空我的长期记忆”。插件只从当前顶层消息识别明确请求，不把引用、转发、否定句或示例中的指令当作授权；不明确的修改目标需要补充。普通聊天不自动保存。相关记忆会提供给同一群、同一发送者的普通回答和 Codex 任务，但不会授予任何工具权限。记忆数据库仅由 AstrBot 插件按当前用户范围写入，不给 Codex 数据库读写权限。

长期记忆删除只影响该插件中的条目，不会删除已有聊天记录、旧 Codex 会话或外部备份。详见 [记忆插件说明](plugins/long_term_memory/README.md)。

## 验证

无需密钥、无 QQ/模型网络调用的测试：

```bash
python3 -m unittest discover -s plugins/reminder -p 'test_*.py' -v
python3 -m unittest discover -s plugins/image_context -p 'test_*.py' -v
python3 -m unittest discover -s plugins/intent_router -p 'test_*.py' -v
python3 plugins/long_term_memory/test_memory.py
python3 -m unittest discover -s services -p 'test_public_web.py' -v
python3 -m unittest discover -s tests -p 'test_portable_*.py' -v
python3 scripts/privacy_check.py
```

真实 AstrBot API 测试使用已安装 venv，传输均为 mock。请在独立临时目录执行，设置 `ASTRBOT_ROOT` 为该临时目录、`PYTHONDONTWRITEBYTECODE=1`，避免导入 AstrBot 时在工作树产生数据：

```bash
/opt/astrbot/venv/bin/python /path/to/personal-agent-harness/tests/test_reminder_runtime.py
/opt/astrbot/venv/bin/python /path/to/personal-agent-harness/tests/test_image_context_runtime.py
/opt/astrbot/venv/bin/python /path/to/personal-agent-harness/tests/test_codex_bridge_runtime.py
python3 scripts/verify_whitelist.py \
  --source-root /opt/astrbot/venv/lib/python3.12/site-packages
```

提交前检查完整暂存区和已有历史：

```bash
python3 scripts/privacy_check.py --staged --history
```

可额外提供仓库外的 JSON 私密词列表：`--private-terms-file /secure/path/private-terms.json`。检查器只报告位置和类别，不打印命中值。CI 运行通用规则、全部 Git 历史扫描和离线测试；自动检查不能替代人工审阅。

## 边界

- QQ 只有把图片推送给机器人后才能缓存；不确定时发图也 `@机器人`。图片过期或服务重启后需重发。
- 默认群聊天文字历史仍共享；图片引用隔离不代表整段群聊实现个人隐私隔离。
- 代理只监听本机，但没有独立的本机客户端认证；同主机有不受信任账号时需要额外限制本地访问。命令网络 profile 放行所有域名；网页 MCP 单独实施公开 GET 与地址检查，模型代理连接所选提供商。搜索关键词会发往搜索服务；不要把私人记忆或凭据作为搜索关键词。
- 不把仓库更新自动发布到服务器；升级 AstrBot 时需重新核对补丁哈希和运行 API。

第三方源码补丁的授权说明见 [THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES.md)。
