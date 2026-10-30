# intent_router

这是一个独立的自然语言路由规划器。它调用单独配置的 OpenAI 兼容 provider，使用 provider `custom_extra_body` 中的 `reasoning_effort: low`，只输出受限 JSON，不直接调用任何插件或产生副作用。

配置在 `intent_router_config.json` 中切换：

- `regex`：使用现有正则、显式命令和主模型路径，默认值。
- `light`：先调用配置的轻量模型做路由；超时、非法 JSON、低置信度或 provider 不可用时回退到 `regex` 路径。

路由结果只作为静态提示，提醒时间、QQ 会话、发送者、Codex 模式、文件路径和工具调用仍由现有插件最终校验。

`planner_provider.json` 是单独的 provider 配置。示例使用 MiniMax M3.1 Flash；也可以将 `provider_id`、`model`、`api_base` 和 `custom_extra_body` 切换到其他兼容模型。真实 key 只能写在服务器本地受限配置中，不能提交到仓库。生产默认保持 `enable: false` 和 `mode: regex`；切到 `light` 前先做一次无副作用 canary。
