# Personal Agent Harness

基于 AstrBot 的私人 QQ 智能体定制集合：官方 QQ 接入、自定义 OpenAI 兼容模型、网页搜索、隔离 Codex 任务、自然语言提醒，以及跨消息图片引用。

本仓库保存可复用源码、补丁、配置模板、测试和运维文档。部署地址、QQ 身份、模型提供商选择、密钥、聊天记录和运行数据均不在仓库中。模板默认关闭 QQ 和模型、拒绝所有会话；需要在自己的服务器本地填写配置并明确放行。

## 已整理的修改

| 内容 | 目录 | 关键行为 |
| --- | --- | --- |
| QQ → Codex 桥接 | `plugins/codex_bridge/` | 显式 `/codex`、群/发送者白名单、写权限单独授权、保留原消息用于异步回复 |
| 独立任务执行和模型代理 | `services/` | Unix socket 调用方 UID 检查、专用账号、Codex 沙箱、任务超时、真实模型密钥只由代理持有 |
| 自然语言提醒 | `plugins/reminder/` | “两分钟后提醒我喝水”、北京时间、原子持久化、一次性调度、Markdown 真正 `@`、重试、列表/取消 |
| 图片引用 | `plugins/image_context/` | “上一张图”“刚发的那几张图”、同群同发送者、30 分钟/12 张上限、缺图提示重发 |
| AstrBot 小补丁 | `patches/` | AnySearch `global → intl`；限流日志不再输出 API Key 前缀 |
| 部署与访问限制 | `deploy/`、`scripts/` | 管理端仅 loopback、nftables、systemd 限权、初始随机密码、环境变量密钥、白名单验证 |

详细清单见 [CUSTOMIZATIONS](docs/CUSTOMIZATIONS.md)，启停和审计见 [OPERATIONS](docs/OPERATIONS.md)。提醒与图片功能直接由 AstrBot 执行；复杂任务由显式 `/codex` 发起。

## 版本基线

- Linux、systemd、nftables、Python 3.12。
- AstrBot `4.29.3`；QQ SDK `qq-botpy 1.2.1` 未修改。
- 已验证的 Codex CLI：`0.162.1`。
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

3. 创建 `/var/lib/astrbot`、`/var/lib/codexrunner/{workspaces,codex}`，分别归对应服务账号所有；创建 `/var/lib/codexproxy` 归 root 管理。状态目录用 `0700`，密钥目录 `/var/lib/codexproxy` 用 `root:codexproxy 0750`。
4. 初始化拒绝所有会话的配置：

   ```bash
   sudo python3 scripts/initialize_private.py --enable-search \
     --api-base https://api.example.com/v1 --model REPLACE_WITH_MODEL_NAME
   ```

   替换示例地址和模型。脚本拒绝覆盖已有配置，随机初始密码只写入 `/etc/astrbot/admin-initial-password`（root 可读）。QQ 和模型保持关闭，直到你本地配置完成。

5. 将三个 `plugins/` 子目录复制到 `/var/lib/astrbot/data/plugins/`；将对应 `deploy/examples/*_config.json.example` 复制到 `/var/lib/astrbot/data/config/` 并去掉 `.example`。属主 `astrbot:astrbot`，目录 `0700`，配置 `0600`。复制 `services/*.py` 到 `/opt/astrbot-codex-runner/`，由 root 管理。
6. 在服务器本地复制并填写 `deploy/examples/` 中的环境文件。`model.env` 放 `/etc/astrbot/`、`root:astrbot 0640`；`runner.env` 和 `proxy.env` 为 root 管理的 `0600` 文件。真实代理 API key 单独存 `/var/lib/codexproxy/upstream-token`，`root:codexproxy 0640`。不要把密钥作为命令行参数或发到 Git。
7. 将 `codex-config.toml.example` 复制到 `/var/lib/codexrunner/codex/config.toml`（`codexrunner:codexrunner 0600`）。该文件只指向本机代理，不保存真实密钥。
8. 安装 `deploy/systemd/*.service` 到 `/etc/systemd/system/`、`deploy/firewall.nft` 到 `/etc/astrbot/`，执行 `systemctl daemon-reload`，然后按运维文档启用服务。通过 SSH 隧道进入管理页面，在本地填写 QQ AppID/AppSecret、启用模型和 QQ 适配器。
9. 把确认后的完整群 UMO 放入 AstrBot 主白名单和需要启用的插件 `allowed_umos`。AstrBot **主白名单为空会放行所有会话**，初始占位值应一直保留到替换为真实允许列表；三个插件的空列表则拒绝所有会话。不要开启管理员绕过。

QQ 凭据保存在服务器上的受限主配置中；模型密钥经环境变量引用。这些文件属于运行环境，绝不能复制回仓库。

## 验证

无需密钥、无 QQ/模型网络调用的测试：

```bash
python3 -m unittest discover -s plugins/reminder -p 'test_*.py' -v
python3 -m unittest discover -s plugins/image_context -p 'test_*.py' -v
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
- 代理只监听本机，但没有独立的本机客户端认证；同主机有不受信任账号时需要额外限制本地访问。Codex 沙箱不等于完整网络隔离。
- 不把仓库更新自动发布到服务器；升级 AstrBot 时需重新核对补丁哈希和运行 API。

第三方源码补丁的授权说明见 [THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES.md)。
