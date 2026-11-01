# Personal Agent Harness

基于 AstrBot 的私人 QQ 智能体定制集合：官方 QQ 接入、自定义兼容模型、公开网页检索、隔离 Codex 任务、SQLite 长期记忆、日程提醒、跨消息图片、小红书原帖图文总结，以及地图与票务筛选。

本仓库保存全部可复用源码、补丁、行为默认值、测试和部署文档。可公开的模型选择与参数保留；部署地址、QQ 身份、私人提供商地址、密钥、聊天记录和运行数据不在仓库中。初始化仍关闭 QQ 和主模型、拒绝所有会话；需在服务器本地填写凭据并明确放行。默认值和私有配置的界线见 [DEFAULTS](docs/DEFAULTS.md)。

## 已整理的修改

| 内容 | 目录 | 关键行为 |
| --- | --- | --- |
| QQ → Codex 桥接 | `plugins/codex_bridge/` | 显式 `/codex`、自然语言和复杂任务路由、群/发送者白名单、写权限单独授权、持久状态看板 |
| 独立任务执行和模型代理 | `services/` | Unix socket 调用方 UID 检查、专用账号、Codex 沙箱、任务超时、真实模型密钥只由代理持有 |
| Codex 会话续接 | `services/conversations.py`、`plugins/codex_bridge/` | 引用已发送的 Codex 回复可续接同一个原生 session，并保留原图；失败时不会静默新建会话 |
| Codex 公开网页工具 | `services/public_web.py`、`services/public_web_mcp.py` | stdio MCP 搜索及 GET 读取公开网页，校验 DNS 与跳转；另已开放任务命令联网 |
| 长期记忆 | `plugins/long_term_memory/` | 授权群／私聊增删改查、尾句授权、SQLite 持久化、按会话和发送者隔离；优先于 Codex，不创建 Markdown 记忆文件 |
| 自然语言提醒 | `plugins/reminder/` | 秒／分钟提醒、文字／图片日程、多条提醒、提前量、连续调整、原子持久化、真正 `@`、列表／取消 |
| 轻量意图路由 | `plugins/intent_router/` | MiniMax Flash low、2 秒预算；超时只读转 Codex，本地专用功能与退出规则优先 |
| 小红书图文 | `plugins/xhs_search/` | 本机扫码／状态、搜索后分步读帖、真实正文与原图、事件绑定、防重复及时间预算 |
| 地图与票务 | `plugins/travel_assistant/` | 高德／百度导航入口、12306、单程机票公开报价、多条件筛选、未知证据单列、同用户连续筛选 |
| 图片引用 | `plugins/image_context/` | 同会话同发送者、30 分钟／12 张上限、原子快照跨重启恢复、缺图提示重发 |
| AstrBot 补丁 | `patches/` | AnySearch 区域映射、密钥与上传日志脱敏、QQ 回执绑定、受限公开图片下载 |
| 部署与访问限制 | `deploy/`、`scripts/` | 管理端仅 loopback、nftables、systemd 限权、初始随机密码、环境变量密钥、白名单验证 |

详细清单见 [CUSTOMIZATIONS](docs/CUSTOMIZATIONS.md)，启停和审计见 [OPERATIONS](docs/OPERATIONS.md)，统一代码／数据目录见 [WORKSPACE](docs/WORKSPACE.md)。本地功能由 AstrBot 专用插件执行；复杂任务及符合边界的前置超时由 Codex 桥接处理。

## 版本基线

- Linux、systemd、nftables、Python 3.12。
- AstrBot `4.29.3`；QQ SDK `qq-botpy 1.2.1` 未修改。
- 已验证的 Codex CLI：`0.162.1`。
- 公开网页 MCP 使用 Python `mcp==1.30.0`，安装在 `/opt/astrbot/venv`；不需要额外搜索密钥。
- 模型供应商需要支持 OpenAI 兼容 Chat Completions；Codex 还需要 Responses API。看图和工具调用须由所选模型支持。

仓库的移植差异仅保留必要的部署边界：QQ 身份留空、本机账号解析 UID、私人上游改用环境变量、通用 token／工作区路径、systemd 模板与 drop-in。功能和非敏感默认值均纳入版本控制；仓库推送不会自动覆盖线上配置或数据。

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
     --api-base https://api.example.com/v1 --model gpt-6-astra
   ```

   替换示例地址和模型。脚本拒绝覆盖已有配置，随机初始密码只写入 `/etc/astrbot/admin-initial-password`（root 可读）。QQ 和模型保持关闭，直到你本地配置完成。

5. 将 `plugins/` 下的子目录复制到 `/var/lib/astrbot/data/plugins/`；将对应 `deploy/examples/*_config.json.example` 复制到 `/var/lib/astrbot/data/config/` 并去掉 `.example`。属主 `astrbot:astrbot`，目录 `0700`，配置 `0600`。复制 `services/*.py` 到 `/opt/astrbot-codex-runner/`，由 root 管理；持久 Codex runner 还需要 root 管理的 `codex-bwrap` 隔离启动器，路径由 `HARNESS_BWRAP` 指定。
   `intent_router_config.json.example` 默认 `light`、2 秒，规划器默认 MiniMax M3.1 Flash、`reasoning_effort=low`、`max_tokens=512`。分别配置规划 provider 和 `$MINIMAX_API_KEY`；无配置时代码仍保守使用 `regex`。超时经既有桥接只读转 Codex，本地记忆／提醒／图文／出行优先；各执行插件仍做最终校验。
6. 在服务器本地复制并填写 `deploy/examples/` 中的环境文件。`model.env` 放 `/etc/astrbot/`、`root:astrbot 0640`；`runner.env` 和 `proxy.env` 为 root 管理的 `0600` 文件。真实代理 API key 单独存 `/var/lib/codexproxy/upstream-token`，`root:codexproxy 0640`。不要把密钥作为命令行参数或发到 Git。
7. 将 `codex-config.toml.example` 复制到 `/var/lib/codexrunner/codex/config.toml`（`root:codexrunner 0640`）。该文件只指向本机模型代理，并配置 `public_web` stdio MCP，不保存真实密钥。确认 venv 已安装 `mcp==1.30.0`，且 `public_web.py`、`public_web_mcp.py` 为 root 管理的代码。保持 `web_search="disabled"`；`default_permissions` 位于 TOML 根级，`network_proxy=true` 和 `codex-net-read`/`codex-net-write` profile 为命令网络启用 `mode="full"`、域名 `*` 放行。只读任务不能写会话工作区或 `.tmp`；工作区写入任务可写分配的会话工作区和 `.tmp`。有本次或保留图片输入的任务会自动切到工作区写入 profile，以便生成图片结果。
8. 安装 `deploy/systemd/*.service` 到 `/etc/systemd/system/`，按需递归安装对应 `*.service.d/` drop-in；统一目录的 `workspace.conf` 需先按 [WORKSPACE](docs/WORKSPACE.md) 配好挂载。`public-images.conf` 需要已有回环代理。安装防火墙、执行 `systemctl daemon-reload`，然后按运维文档启用服务。通过 SSH 隧道在本地填写 QQ 凭据、启用主模型和适配器。
9. 把确认后的完整群 UMO 放入 AstrBot 主白名单和需要启用的插件 `allowed_umos`。AstrBot **主白名单为空会放行所有会话**，初始占位值应一直保留到替换为真实允许列表；各插件的空列表则拒绝所有会话。不要开启管理员绕过。

10. 如启用小红书搜索，按 [小红书部署说明](docs/XHS.md) 单独部署 `xpzouying/xiaohongshu-mcp`，保持回环监听和独立账号。API token 在服务器生成并保存；管理员只需在授权私聊发送 `/xhs login` 获取二维码，扫码后 Cookie 持久保留。不在 QQ 发送 Token 或 Cookie：框架在插件执行前就可能记录原始消息。`login_umos` 和 `login_senders` 必须同时配置为已核验的私聊身份，不能直接把群内 OpenID 当作私聊身份。

11. 如启用长期记忆，把 `long_term_memory_config.json.example` 复制为私有配置，并明确批准群／私聊。记忆目录 `astrbot:astrbot 0700`、SQLite 及辅助文件 `0600`。不使用工作区 Markdown，不因前置超时交给 Codex。更新代码保留数据库。

12. 出行插件配置见 `travel_assistant_config.json.example`。模板保留本机机票代理和数据路径；没有代理时留空，不另换出口绕过限制。票务只查询和筛选，不下单；未知价格、库存、行李与退改要求单列待核实。导航是官方入口，不是实时路况 API。

隔离启动器就是系统 `bubblewrap` 的受控部署副本：安装固定发行版包后 `install -m 0755 /usr/bin/bwrap /opt/astrbot-codex-runner/codex-bwrap`，不要提交二进制。Ubuntu AppArmor 策略在 `deploy/apparmor/astrbot-codex-bwrap`；加载并按目标内核验证用户命名空间与禁止访问任务库／socket 的规则，不要为兼容而整体关闭隔离。

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
python3 -m unittest discover -s plugins/codex_bridge -p 'test_*.py' -v
python3 -m unittest discover -s plugins/travel_assistant -p 'test_*.py' -v
python3 -m unittest discover -s plugins/xhs_search -p 'test_*.py' -v
python3 plugins/long_term_memory/test_memory.py
python3 -m unittest discover -s services -p 'test_public_web.py' -v
python3 -m unittest discover -s tests -p 'test_portable_*.py' -v
python3 -m unittest discover -s tests -p 'test_timeout_bridge.py' -v
python3 -m unittest discover -s tests -p 'test_public_image_download.py' -v
python3 scripts/privacy_check.py
```

离线测试依赖见 `requirements-test.txt`。真实 AstrBot API 测试使用固定版本 venv，传输均为 mock，脚本和环境变量见 [TESTING](docs/TESTING.md)。请设置独立临时 `ASTRBOT_ROOT` 和 `PYTHONDONTWRITEBYTECODE=1`，避免工作树产生数据：

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

- QQ 只有推送给机器人后才能缓存；不确定时发图也 `@机器人`。缓存快照跨重启恢复剩余 TTL，但已过期／临时文件被清理仍需重发。
- 默认群聊天文字历史仍共享；图片引用隔离不代表整段群聊实现个人隐私隔离。
- 代理只监听本机，但没有独立的本机客户端认证；同主机有不受信任账号时需要额外限制本地访问。命令网络 profile 放行所有域名；网页 MCP 单独实施公开 GET 与地址检查，模型代理连接所选提供商。搜索关键词会发往搜索服务；不要把私人记忆或凭据作为搜索关键词。
- 不把仓库更新自动发布到服务器；升级 AstrBot 时需重新核对补丁哈希和运行 API。

第三方源码补丁的授权说明见 [THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES.md)。
