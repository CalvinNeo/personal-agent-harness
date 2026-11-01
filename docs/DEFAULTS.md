# 可公开的定制默认值

行为默认值不是凭据，必须随源码保存。真实 QQ 身份、授权范围、部署主机、私人上游、密钥、Cookie、聊天和记忆内容只留在服务器。示例允许列表留空不是遗漏，而是避免泄露身份和默认开放。

| 功能 | 保存的默认行为 | 模板／来源 |
| --- | --- | --- |
| 主聊天和 Codex | `gpt-6-astra`；聊天支持 text/image/tool_use，另有 text-only 配置 | `chat_provider.json.example`、`runner.env.example`、初始化器 |
| 前置意图 | `light`、MiniMax-M3.1-Flash、low、512 tokens、2 秒、置信度 0.85、6000 字符 | `intent_router_config.json.example`、`planner_provider.json.example` |
| 前置兜底 | 超时按既有权限只读转 Codex；无效 JSON、低置信度、并发满和一般错误走原规则 | `plugins/intent_router/`、`plugins/codex_bridge/` |
| Codex 路由 | 自然语言、自动复杂任务、受限语义派单全部开启；默认只读，写权限单独白名单 | `codex_bridge_config.json.example` |
| Codex 网络 | root 级 default_permissions、按任务模式选择网络 profile、内置 web_search 关闭、公开网页 stdio MCP | `codex-config.toml.example` |
| 模型代理 | HTTPS 上游由环境指定，独立 token 文件，最大请求体 8 MiB | `proxy.env.example`、`services/codex-api-proxy.py` |
| 普通聊天 | 网页搜索和来源链接默认开启；不显示推理／工具内部状态，不启用 Computer Use | `scripts/initialize_private.py` |
| 图片上下文 | 30 分钟 TTL、2 分钟隐式追问、12 张／scope、最多 64 scopes、0600 原子快照恢复 | `image_context_config.json.example`、插件常量 |
| 长期记忆 | SQLite、最多 100 条／scope、每条 1000 字符、明确操作才写；授权群和私聊早于前置处理 | `long_term_memory_config.json.example` |
| 提醒 | 北京时间、最短 5 秒、最长 7 天、每群 20／每人 10、文字与图片日程、多条和提前量 | `reminder_config.json.example`、`schedule_tools.py` |
| 小红书 | 本机服务、90 秒请求、5 条搜索；分步读 3 篇、最多 4 张原图，105 秒读取预算 | `xhs_search_config.json.example`、`notes.py`、`main.py` |
| 出行 | 12306、单程 1 成人经济舱公开报价、20 分钟连续筛选；机票使用固定回环代理 | `travel_assistant_config.json.example` |
| 高铁默认筛选 | G／D／C、二等座、明确有票；最多核价 24 班、3 并发，最多缓存 600 项 | `travel_assistant/core.py`、`providers.py` |
| 地图／图片代理 | 官方地点与导航链接；Wikimedia 原图专用回环代理，其他媒体不变 | `travel_assistant/`、`astrbot.service.d/public-images.conf` |

`chat_provider.json.example` 展示 provider source 及关联模型，合入主配置时分别放入 AstrBot 的 `provider_sources` 和 `provider`，不要原样当作单个 provider 导入。主聊天与 QQ 在初始化阶段关闭，待本机凭据和白名单配好再启用。规划 provider 模板保留启用意图，但缺少本地 key 时不会形成可用调用。

`allowed_umos=[]` 拒绝插件全部会话；`allowed_senders=[]` 只表示在已授权会话内不再细分用户；`write_senders=[]` 拒绝写入。小红书登录须同时匹配精确授权私聊和发送者。AstrBot 主白名单空列表会放行所有会话，所以初始化保留拒绝占位值，不得直接清空。

机票／公开图片模板依赖已有 `http://127.0.0.1:7890` 代理，没有代理就移除相应可选项并接受直连能力限制；不附带代理订阅、私人节点或自动绕过限流逻辑。工作区名和插件只读入口是可移植的通用别名，部署时统一替换，现有机器不自动迁移。
