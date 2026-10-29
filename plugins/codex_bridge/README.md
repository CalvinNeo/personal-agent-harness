# Codex bridge

QQ 群内 @ 机器人即可说「请用 Codex 检查这个项目的依赖漏洞」「帮我分析这段代码，交给 Codex」「用 Codex 调研并比较这三个方案」。原来的 `/codex`、`/cx` 命令仍可用。

明确的代码审查、排错、编程和多步骤调研会自动交给 Codex。陌生说法由主聊天模型判断是否需要调用受限的 `delegate_current_task_to_codex` 工具。该工具只提交当前用户消息，不接受模型生成的替代任务或权限参数。普通聊天、识图、提醒、使用方法询问、引用和否定句留在原来的处理路径。

所有入口都需要 QQ 官方平台、明确 @ 或唤醒、精确群白名单和可选发送者白名单。默认以 `read-only` 执行。即使任务要求修复代码，也不会因此自动开启写权限；只有明确要求工作区写入模式并且发送者位于 `write_senders` 中才允许 `workspace-write`。

当前执行器只接收文本。依赖图片、附件、引用或缺少上下文的任务会要求先补齐内容，避免把丢失上下文的任务提交给 Codex。安装独立 `intent_router` 后，`mode=regex` 保持现有规则；`mode=light` 会先用 GPT-6 Astra low 识别复杂任务，再复用这里的只读与白名单校验。规划失败自动回到原规则。

受理后会回复任务看板，后台继续执行并回传结果。可以自然地问「Codex 任务怎么样了」「Codex 结果呢」「我的 Codex 任务列表」，也可以使用 `/codex status`、`/codex tasks`、`/codex result`。任务状态和结果按群与发送者隔离。

三个配置开关为 `natural_language_enabled`（明确自然语言 Codex 请求）、`auto_complex_enabled`（明确复杂任务自动路由）、`semantic_routing_enabled`（主模型语义路由），默认开启。`allowed_umos` 默认空列表，未配置的实例拒绝所有会话。

本地回归：在插件目录执行 `python3 -m unittest discover -p 'test*.py' -q`。测试覆盖自然语言、语义分派、权限、附件边界及状态持久化，使用伪 QQ 发送和伪 runner RPC，不发送真实群消息。
