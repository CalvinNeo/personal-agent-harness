# intent_router

这是一个独立的自然语言路由规划器。它调用专用的 `gpt-6-astra` provider，使用 OpenAI 的 `reasoning_effort: low`（AstrBot 4.29 的 Chat Completions 适配器通过 provider 的 `custom_extra_body` 发送），只输出受限 JSON，不直接调用任何插件或产生副作用。

配置在 `intent_router_config.json` 中切换：

- `regex`：使用现有正则、显式命令和主模型路径，默认值。
- `light`：先调用 GPT-6 Astra 的 `reasoning_effort=low` 做路由；超时、非法 JSON、低置信度或 provider 不可用时回退到 `regex` 路径。

路由结果只作为静态提示，提醒时间、QQ 会话、发送者、Codex 模式、文件路径和工具调用仍由现有插件最终校验。

`planner_provider.json` 是单独的 provider 配置。真实 key 只能写在服务器本地受限配置中，不能提交到仓库。生产默认保持 `enable: false` 和 `mode: regex`；切到 `light` 前先做一次无副作用 canary。
