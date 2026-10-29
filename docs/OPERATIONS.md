# 手动运维与访问审计

以下命令面向使用本仓库模板部署的 Linux 服务器，需要管理员权限。`SERVER_HOST` 是占位符，必须替换成自己的服务器地址。仓库采用通用路径与参数；已经运行的实例以实际 systemd 单元和本机配置为准。

## 目录分工

| 路径 | 用途 | 是否纳入 Git |
| --- | --- | --- |
| `/opt/astrbot/venv` | 固定版本 AstrBot 和 Python 依赖 | 否，按版本重新安装 |
| `/opt/astrbot-codex-runner` | Codex runner 与代理程序 | 源码在 `services/`，部署副本不直接跟踪 |
| `/var/lib/astrbot/data/plugins` | 自定义插件及其他插件的部署位置 | 仅收录本仓库的插件源码 |
| `/var/lib/astrbot/data/config` | 插件私有配置、会话和发送者白名单 | 否 |
| `/var/lib/astrbot/data/cmd_config.json` | AstrBot、QQ、模型和后台私有配置 | 否 |
| `/var/lib/astrbot/data/plugins/reminder/reminders.json` | 待执行提醒及提醒内容 | 否 |
| `/var/lib/codexrunner` | Codex 配置、任务记录、按会话隔离的工作目录和原图 | 否 |
| `/var/lib/codexproxy/upstream-token` | 代理使用的上游模型密钥 | 否 |
| `/etc/xhs/api-token` | 本机小红书服务访问密钥（root:xhs-secret 0640） | 否 |
| `/var/lib/xhs/data/cookies.json` | 小红书扫码后的账号登录态（xhs 0600） | 否 |
| `/run/astrbot-codex/bridge.sock` | AstrBot 与 Codex runner 的本机通信 socket | 否，运行时创建 |
| `/etc/astrbot` | 主机侧防火墙及环境配置 | 仅收录脱敏模板 |
| `/etc/systemd/system` | 系统服务部署位置 | 模板在 `deploy/systemd/` |

私有状态应由对应服务账户持有，避免为排障而改成全局可读。`astrbot`、`codexrunner`、`codexproxy` 各自承担机器人、任务执行、上游凭据代理职责，不应合并成 root 进程。

## 配置和启动前检查

按照仓库 README 完成安装后，在服务器本地填写 QQ 凭据、模型提供商地址和密钥。不要把真实值填回示例文件后提交。初始化工具在 AstrBot 主白名单写入拒绝占位值，各插件的允许列表为空，机器人默认不接受任何群。AstrBot 主白名单不能清空，清空会放行所有会话；放行时同步维护框架层白名单和相关插件的 `allowed_umos`。

`allowed_senders` 空列表表示授权群内不再限制用户。若只允许部分成员使用，应填入明确的发送者标识。Codex 写入功能使用独立的 `write_senders`，默认关闭。

先检查模板参数、文件权限和白名单，再启动服务。配置验证脚本不能证明 QQ 平台权限或模型可用，仍需在获准的会话中进行实际验收。只有 Python 语法检查通过，也不能证明插件已被 AstrBot 成功加载。

## 开启、停止和重启

启动完整服务：

```bash
sudo systemctl start astrbot-firewall.service
sudo systemctl start codex-api-proxy.service
sudo systemctl start astrbot-codex-runner.service
sudo systemctl start astrbot.service
```

停止所有机器人处理和 Codex 执行：

```bash
sudo systemctl stop astrbot.service
sudo systemctl stop astrbot-codex-runner.service
sudo systemctl stop codex-api-proxy.service
```

防火墙服务可以保留启用。停止其 systemd 单元不等同于自动移除 nftables 规则，不要用清空整机防火墙的方式排障。

仅重启 AstrBot（例如插件更新后）：

```bash
sudo systemctl restart astrbot.service
```

小红书搜索服务独立使用 `systemctl start|stop|restart xhs-mcp.service`，暂停服务
不会删除 Cookie；账号退出应由授权私聊执行 `/xhs logout`。连接、扫码与权限配置
见 [XHS](XHS.md)。`18060` 必须只监听回环地址，不能公开到互联网。

重启会清空图片 URL 缓存并中断插件内尚未回传的 Codex 任务；持久化提醒会在启动后重新加载。需要确定停止正在执行的 Codex 子进程时，同时停止 runner 服务。部署更新前应检查是否有进行中的任务。

查看状态：

```bash
sudo systemctl --no-pager --full status astrbot.service astrbot-codex-runner.service codex-api-proxy.service astrbot-firewall.service
sudo systemctl is-enabled astrbot.service astrbot-codex-runner.service codex-api-proxy.service astrbot-firewall.service
```

需要开机自启时使用 `systemctl enable`；仅手动启动一次不改变自启状态。彻底停用机器人可先停止上述运行服务，再禁用相应自启项。

## 管理后台只通过 SSH 隧道访问

在自己的电脑上执行：

```bash
ssh -N -L 127.0.0.1:16185:127.0.0.1:6185 root@SERVER_HOST
```

然后浏览器打开 `http://127.0.0.1:16185`。保持 SSH 进程运行，关闭该进程即关闭隧道。后台仍需使用自己的管理员凭据，不要为了免登录而关闭认证。

在服务器检查监听端口与 socket：

```bash
sudo ss -lntp '( sport = :6185 or sport = :6186 )'
sudo ss -lxnp
sudo nft list table inet astrbot_guard
sudo stat -c '%a %U:%G %n' /run/astrbot-codex/bridge.sock
```

按模板部署时，后台 `6185` 和模型代理 `6186` 应只监听回环地址，不应出现 `0.0.0.0` 或公网地址。Codex runner 使用 Unix socket。nftables 规则计数可帮助发现被拦截的连接，但计数本身不包含访问者身份或完整请求。

QQ 官方接入通过机器人主动建立的平台连接接收事件，不要求把 AstrBot 管理后台暴露到公网。主机上的其他服务、SSH 安全组和防火墙规则仍需单独管理。

## 查看日志与审计访问

限定时间范围查询，避免一次导出全部聊天历史：

```bash
sudo journalctl -u astrbot.service --since '30 minutes ago' --no-pager -o short-iso
sudo journalctl -u astrbot-codex-runner.service -u codex-api-proxy.service --since '30 minutes ago' --no-pager -o short-iso
sudo journalctl -u ssh.service -u sshd.service --since today --no-pager -o short-iso
```

实时跟踪机器人日志：

```bash
sudo journalctl -u astrbot.service -f -o short-iso
```

按时间关联以下证据：QQ 消息是否到达、插件是否加载及命中、提醒是否创建、发送是否报错、Codex runner 是否启动任务、模型代理是否返回异常，以及同一时段是否有管理员登录或服务重启。没有日志记录不等于已经证明“从未有人访问”，当前实现也不是完整的不可篡改审计系统。

提醒任务的私有 JSON 可以用于核对仍待执行的任务。成功发送或达到重试上限的记录会被移除，因此不能仅凭文件中不存在某条任务判断它已经送达。图片缓存位于内存中，没有图片 URL 审计文件。Codex 任务使用临时会话，仍可能在工作区留下用户授权写入的文件。

日志、待办提醒、工作目录名称和错误响应都可能含聊天内容、会话标识、用户信息或第三方服务返回的敏感数据。只在服务器上按需查看；分享排障信息前脱敏，不要把原始 `journalctl` 输出、完整配置或环境变量导出到仓库。API key 前缀日志补丁减少一类泄漏，不保证所有依赖日志都不含敏感信息。

## 更新、回滚和备份

更新插件和服务源码前，保留受限权限的服务器本地备份。备份要放在 Git 工作区之外，源码与私有状态分开保存。发布时只同步需要修改的代码文件，不能覆盖 `reminders.json`、插件配置、凭据文件、数据库或工作区。

AstrBot 固定为 4.29.3。两处源码补丁应通过仓库提供的补丁工具应用，并检查匹配的版本和基线。升级 AstrBot 或 Codex 后，重新验证插件导入、配置类型、事件优先级、QQ 回复方式、沙箱限制与模型接口，不要只运行 `py_compile` 就判断升级成功。

重启后检查各插件均加载成功，后台仍只监听本机，默认拒绝与允许列表按预期生效。对解析和图片选择先运行仓库的本地测试，再按需要进行获准会话中的端到端测试；不要通过重放旧提醒或旧聊天记录制造重复通知。

回滚时恢复对应版本的源文件和服务模板，保留当前私有状态；若状态结构发生变化，先按该版本说明迁移。恢复旧备份的提醒状态可能重复发送已经执行的任务，应先人工核对。

## 提交仓库前的隐私检查

只纳入插件、服务、补丁、通用部署模板、测试和文档。提交前运行仓库的隐私检查工具，并检查 `git diff --cached` 与待提交文件列表。机器扫描只能发现已知模式，无法替代对真实姓名、服务器地址、群标识、私人 URL 和聊天内容的人工检查。

`.gitignore` 只能阻止尚未跟踪的文件，不能移除 Git 历史中已有的信息。若凭据意外进入提交，应立即轮换受影响凭据，再清理历史；单纯删除当前文件不足以消除泄漏。
