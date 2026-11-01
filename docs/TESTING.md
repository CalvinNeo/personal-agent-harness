# 测试与发布验证

## 离线回归

```sh
python3 -m pip install -r requirements-test.txt
PYTHONDONTWRITEBYTECODE=1 python3 scripts/run_checks.py
python3 scripts/privacy_check.py --history
```

各插件、services 和通用测试分进程执行，避免 AstrBot stub 相互污染。无真实模型、票务、图片下载或 QQ 发送。网络返回、媒体和身份均为合成数据，不使用生产配置。没有把真实故障聊天、私人位置、Cookie 或验收日志当作测试 fixture 提交。

## 真实 AstrBot 框架

使用固定 AstrBot 4.29.3 的 venv；只替换模型／上游网络、runner RPC 与发送传输，仍执行真实事件注册、工具执行器和提交／序列化路径。下列脚本只使用临时数据库和合成消息，不运行真实 Codex 任务或向 QQ 发消息。

先在仓库之外创建私有临时目录，设置 `ASTRBOT_ROOT`、`PYTHONDONTWRITEBYTECODE=1`，并从该临时目录运行。`REPO` 是本地源码绝对路径，`TMP` 为本机临时目录：

```sh
export PYTHONDONTWRITEBYTECODE=1
export ASTRBOT_ROOT="$TMP/astrbot"
export REPAIR_STATE_ROOT="$TMP" REPAIR_PLUGIN_ROOT="$REPO/plugins"
export ROUTER_PLUGIN_ROOT="$REPO/plugins" ROUTER_REPORT="$TMP/timeout.json"
export MEMORY_PLUGIN_ROOT="$REPO/plugins" MEMORY_REPORT="$TMP/memory.json"
export TRAVEL_PLUGIN_ROOT="$REPO/plugins" TRAVEL_STATE_ROOT="$TMP" TRAVEL_REPORT="$TMP/travel.json"
export XHS_PLUGIN_ROOT="$REPO/plugins" XHS_REPORT="$TMP/xhs.json"
python "$REPO/tests/runtime_history_repairs.py"
python "$REPO/tests/runtime_timeout_codex.py"
python "$REPO/tests/runtime_memory_sqlite.py"
python "$REPO/tests/runtime_travel.py"
python "$REPO/tests/runtime_xhs_notes.py"
```

已有 `test_reminder_runtime.py`、`test_image_context_runtime.py` 和 `test_codex_bridge_runtime.py` 也应分别执行。真实框架测试不是正式 QQ 实发、真实网站可用性或 Codex 完成任务的端到端保证。图片适配器编码通过不代表 QQ 客户端已显示图片。

生产目录位于不可遍历的主目录时，使用受控 systemd transient unit 的只读 bind 提供源码、单独 `ReadWritePaths` 提供验收目录；不要通过放宽 `/root` 或运行身份获取测试便利。报告和临时内容保留在私有 data，不提交 Git。

## 补丁

AstrBot：对固定 wheel 解压副本执行 `scripts/apply_patches.py --site-packages PATH --check`，实际应用后再次验证所有哈希。新增 helper 在原始版本应不存在；未知或混合状态拒绝。XHS 从固定 v2.5.5 源码应用合并补丁并运行 Go 测试，详见对应 build 文档。

## 发布

发布前记录源文件／配置哈希，确认没有运行或排队任务，保存源码和 SQLite 一致性快照。只替换本次源文件，保留配置、Cookie、状态权限。恢复服务后检查全部插件、QQ transport、无 traceback／加载失败、数据库 quick_check 和其他服务 PID。

回滚只恢复对应源码，不把旧数据库覆盖当前记忆或重放提醒。后续文件被修改时拒绝盲目回滚。群内端到端测试必须单独得到授权，不能用正式群当自动化测试通道。
