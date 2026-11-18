# 测试与发布验证

按需读取与媒体回传的回归位于 `plugins/codex_bridge/test_history.py`、`plugins/codex_bridge/test_video_delivery.py`、`services/test_output_media.py`，覆盖完整分页、全文分段、跨群/私聊拒绝、失败不冒充空记录、实时记忆更新、无附件图片和视频输出、媒体校验、部分发送重试与临时文件清理。真实 MP4 测试需安装 `ffmpeg`/`ffprobe`；缺失时会明确跳过，不代表视频链路已验通。`tests/runtime_on_demand.py` 使用真实 AstrBot 组件、合成数据、伪 QQ 发送进行集成验收；`ON_DEMAND_PLUGIN_ROOT` 可选择候选或正式插件目录，`ON_DEMAND_REPORT` 指定报告路径。

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
export MENTION_PLUGIN_ROOT="$REPO/plugins" MENTION_STATE_ROOT="$TMP" MENTION_REPORT="$TMP/mentions.json"
python "$REPO/tests/runtime_history_repairs.py"
python "$REPO/tests/runtime_timeout_codex.py"
python "$REPO/tests/runtime_memory_sqlite.py"
python "$REPO/tests/runtime_travel.py"
python "$REPO/tests/runtime_xhs_notes.py"
python "$REPO/tests/runtime_qq_text_mentions.py"
```

已有 `test_reminder_runtime.py`、`test_image_context_runtime.py` 和 `test_codex_bridge_runtime.py` 也应分别执行。真实框架测试不是正式 QQ 实发、真实网站可用性或 Codex 完成任务的端到端保证。图片适配器编码通过不代表 QQ 客户端已显示图片。

文字提及测试执行真实 QQ SDK payload 解析、群消息回调、唤醒、白名单、提醒过滤器／持久化和意图路由，发送与模型均替换为 mock。部署前可用 `MENTION_ADAPTER_ROOT` 指向包含候选 `qqofficial_platform_adapter.py` 和 `text_mentions.py` 的目录；部署后不设置该变量以验证已安装版本。它不能验证 QQ 是否会投递用户手打的普通群消息。

生产目录位于不可遍历的主目录时，使用受控 systemd transient unit 的只读 bind 提供源码、单独 `ReadWritePaths` 提供验收目录；不要通过放宽 `/root` 或运行身份获取测试便利。报告和临时内容保留在私有 data，不提交 Git。

## 补丁

AstrBot：对固定 wheel 解压副本执行 `scripts/apply_patches.py --site-packages PATH --check`，实际应用后再次验证所有哈希。新增 helper 在原始版本应不存在；未知或混合状态拒绝。XHS 从固定 v2.5.5 源码应用合并补丁并运行 Go 测试，详见对应 build 文档。

## 发布

发布前记录源文件／配置哈希，确认没有运行或排队任务，保存源码和 SQLite 一致性快照。只替换本次源文件，保留配置、Cookie、状态权限。恢复服务后检查全部插件、QQ transport、无 traceback／加载失败、数据库 quick_check 和其他服务 PID。

回滚只恢复对应源码，不把旧数据库覆盖当前记忆或重放提醒。后续文件被修改时拒绝盲目回滚。群内端到端测试必须单独得到授权，不能用正式群当自动化测试通道。

## 共享会话、失败恢复与可选服务

`test_automatic_retries.py` 检查先回复失败再重试、去重与不明状态副作用回执；共享会话覆盖授权 UMO 跨成员续接、结果、原图及工具 actor 身份。`test_storage_budgets.py` 和 `test_media_selection.py` 检查持久状态与临时媒体限额、合规流选择及紧凑 MCP 返回。共享会话访问不会放宽记忆／提醒身份。

闲鱼离线测试包含账号身份校验、令牌交换与响应竞态、Cookie 快照权限、默认不发商品图及去重提醒；真实 Chromium 的启动检测回归只在显式设置 `XIANYU_TEST_BROWSER` 后运行，浏览器所有网络均由本地 fixture 拦截。缺少浏览器时报告跳过。`tests/runtime_xianyu.py` 使用真实 AstrBot 和合成商品／事件，QQ 和网站请求均在边界替换。

可选语音的离线边界、worker 协议和真实框架入口见 `tests/test_voice_*.py`、`tests/runtime_voice_input.py`；它们不读取真实语音或调用线上转写模型。统一检查还执行隐私规则的合成 Git、索引和历史回归。
