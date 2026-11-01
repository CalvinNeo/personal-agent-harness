# 代码与数据分离

可选集中根目录为 `/root/agent_bot_workspace`，也可用 `--base` 指定本机私有绝对路径。此处为通用模板，不包含真实部署路径。根目录保持 0700，服务不直接遍历 `/root`，通过固定系统兼容入口访问。

```text
agent_bot_workspace/
├── utils/
│   ├── plugins/                 七个自定义插件源码
│   ├── codex-runner/            Python 服务、网页 MCP、bubblewrap 副本
│   ├── astrbot-runtime/         固定版本 venv 和依赖
│   ├── xiaohongshu-mcp/         二进制及受控启动器
│   ├── source/                 已验证源码快照
│   ├── maintenance/            运维代码
│   └── tests/                  可复现测试
└── data/
    ├── astrbot/                配置、聊天、附件、SQLite 记忆和插件状态
    ├── codexrunner/            任务、会话、工作区和原图
    ├── codexproxy/             上游凭据
    ├── xhs/                    Cookie 与浏览器缓存
    ├── config/                 本机环境和防火墙配置
    ├── backups/                私有备份和部署清单
    ├── recovery/               私有恢复证据
    └── migration/              本机迁移验收
```

| 兼容入口 | 集中根目录中的实际位置 |
| --- | --- |
| `/opt/astrbot` | `utils/astrbot-runtime` |
| `/opt/astrbot-codex-runner` | `utils/codex-runner` |
| `/opt/xiaohongshu-mcp` | `utils/xiaohongshu-mcp` |
| `/opt/agent-bot-plugins`（只读） | `utils/plugins` |
| `/var/lib/astrbot` | `data/astrbot` |
| `/var/lib/codexrunner` | `data/codexrunner` |
| `/var/lib/codexproxy` | `data/codexproxy` |
| `/var/lib/xhs` | `data/xhs` |
| `/etc/astrbot` | `data/config/astrbot` |
| `/etc/xhs` | `data/config/xhs` |

这些是持久 bind mount，不是额外复制。插件状态目录中源文件使用指向只读插件入口的 loader 链接，可变 JSON／SQLite 留在 data。系统单元、内核策略、journald 和 `/run` socket 仍由系统管理。

## 已有标准目录迁移

`scripts/migrate_workspace.py` 仅面向上述标准目录的单次迁移，不是任意主机清理器。先审核 `plan`、备份系统与状态、确认没有排队／运行任务。要求源目录尚未挂载、目标不存在、无已有 drop-in 或迁移记录、同一文件系统可原子重命名，`/root` 为私有目录。不要在已迁移实例重复运行。

```sh
sudo python3 scripts/migrate_workspace.py plan --base /root/agent_bot_workspace
sudo python3 scripts/migrate_workspace.py migrate --base /root/agent_bot_workspace
sudo python3 scripts/verify_workspace.py --base /root/agent_bot_workspace
```

迁移会停止相关服务，记录原文件 SHA-256、大小、属主和权限，移动实际目录、拆分插件代码和状态，写入持久挂载与 `RequiresMountsFor`，逐文件验证后恢复原来已启动的服务。目标不兼容时拒绝执行，迁移过程失败会尝试回滚；恢复状态仍需人工核验。源码中的 rollback 应在同一份迁移 journal 及原路径未被后续修改时使用，不是长期版本回退工具。

不执行按历史名称批量删除，不包含真实主目录清理名单。临时文件须先检查进程占用与备份，只删除本次操作生成并确认不再使用的候选包／缓存。保留聊天、Cookie、记忆、任务、原图、备份和恢复证据。

统一目录的 systemd drop-in 在 `deploy/systemd/*.service.d/workspace.conf`。只在挂载准备好时安装；不集中迁移时沿用标准目录，不需要这些 drop-in。`public-images.conf` 是独立可选功能，不依赖集中目录。

`scripts/sandbox_probe.py` 仅在已部署 Linux 上以 runner 身份检查隔离，不调用模型或 QQ。它只在新建的临时任务工作区写入探针，检查不能读取主配置／代理凭据／小红书 token、任务库或 socket。不得为了测试放宽 `/root`。
