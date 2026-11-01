# intent_router

这是独立的无副作用前置意图规划器，只产生受限 JSON。部署模板保留当前定制默认：`mode=light`、`MiniMax-M3.1-Flash`、`reasoning_effort=low`、`max_tokens=512`、2 秒预算、置信度门槛 0.85、输入上限 6000 字符。

配置在 `intent_router_config.json` 中切换：

- `regex`：现有正则、显式命令和主模型路径；没有配置时的保守 fallback。
- `light`：先调用 MiniMax Flash。正常结果供专用插件参考，最终权限与参数仍由执行插件校验。
- 超时：由 Codex 桥接在原白名单、自动复杂任务开关、用户未退出等限制下提交只读任务。小红书、出行、提醒、长期记忆等本地专用流程仍优先，不取得写权限。
- 并发满、无效 JSON、低置信度、普通 provider 错误不冒充超时，沿用既有兜底。

路由结果只作为静态提示，提醒时间、QQ 会话、发送者、Codex 模式、文件路径和工具调用仍由现有插件最终校验。

deadline 不等待 provider 取消清理；取消中的请求仍占最多两个并发名额，同事件失败不重试，迟到结果不能写回事件。两秒是前置预算，不是 Codex 完成时限；事件循环阻塞时不是硬实时保证。Flash 使用 thinking，不发送无效的 `none`／`disabled` 参数强行关闭。

`deploy/examples/planner_provider.json.example` 与 `intent_router_config.json.example` 保存可公开默认值；密钥通过 `$MINIMAX_API_KEY` 本地注入。QQ 身份列表留空拒绝未授权会话。配置 provider 后先做无副作用验证，再向批准会话开放。
