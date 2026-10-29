"""Language-level routing regressions; no runner or QQ traffic is performed.

These cases specify user intent independently of the matching implementation.
Task contents must survive routing, and natural wording must never grant write
access merely because a task asks for code or mentions changing something.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


spec = importlib.util.spec_from_file_location("codex_intent_core", Path(__file__).with_name("core.py"))
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)


EXPLICIT_READ_ONLY = (
    "帮我用 codex 查资料，比较三个开源方案并给出建议",
    "这个任务交给 Codex：把这三种方案做完整对比",
    "让 Codex 帮我分析这个项目的依赖和潜在问题",
    "调用 CODEX 调研一下 AstrBot 的部署方式",
    "麻烦请 Codex 帮我检查这份代码",
    "Codex，帮我分析一下这个报错",
    "能不能用 Codex 帮我检查这个项目的依赖漏洞？",
    "请让 Codex 帮我整理这份技术方案，可以吗？",
    "这件事用 codex 做：比较 Python 和 Go 的并发模型",
    "请用 Codex 分析为什么不应该使用老版本 Python",
    "帮我用 Codex 写一个 Python 脚本",
    "Use Codex to compare these three implementation options.",
    "Please ask Codex to review this function for race conditions.",
    "用 codex 来标注，要求看刚才和你说的",
    "@calvin的智能体 用 codex 来标注，要求看刚才和你说的&#x20;",
    "用 codex 处理：这个图，给我标注一下到底都是什么东西。你要真实对每个都搜一下用途，而不能只是翻译。标注完毕把图发我",
    "让 Codex 给这份方案加标签并整理说明",
    "请用 Codex 在图上圈出并高亮重点",
)


EXPLICIT_WRITE = (
    "用 Codex 的工作区写入模式修复这个项目的测试",
    "请用 Codex 可写模式修改工作区的 README",
    "Use Codex in workspace-write mode to update this project.",
    "Ask Codex in write mode to fix the test in this workspace.",
    "让 Codex 切换到可写模式修复这个项目的测试",
    "请用 Codex 在可写工作区模式下修复这个项目",
    "Please use Codex with workspace-write mode to repair this function.",
)


MODE_MENTIONS_READ_ONLY = (
    "用 Codex 只读分析 workspace-write 模式为什么不安全",
    "请用 Codex 分析 workspace-write 模式为什么不安全",
    "请用 Codex 分析开启可写模式的风险",
    "用 Codex 比较工作区写入模式和可写模式的区别",
    "请用 Codex 解释如何使用可写模式修改文件",
    "请用 Codex 的可写模式说明文档分析执行权限",
    "用 Codex 的可写模式检查代码，但是不要写入任何文件",
    "请用 Codex 可写模式分析代码，禁止修改文件",
    "请用 Codex 可写模式分析代码，但不使用写入功能",
    "请用 Codex 可写模式检查代码，保持只读",
    "用 Codex 分析这个项目，不需要开启可写模式",
    "Use Codex in read-only mode to analyze why workspace-write mode is unsafe.",
    "Use Codex to explain the risks of using workspace-write mode.",
    "Ask Codex to compare write mode and workspace-write mode.",
    "Use Codex in workspace-write mode to analyze this function, but do not write any files.",
    "Use Codex in write mode to inspect this function without modifying files.",
    "Use Codex in workspace-write mode, but keep the task read only and review this code.",
    "Use Codex to review this function; never enable workspace-write mode.",
)


COMPLEX_READ_ONLY = (
    "帮我审查这个项目的代码，找出潜在问题并给出修复建议",
    "分析这个 Python 项目的依赖冲突，定位报错原因并给出修复方案",
    "调研三种 QQ 机器人框架，比较部署成本和安全性，最后给出选型报告",
    "帮我查阅多个来源验证这些数据，并写一份有引用的对比报告",
    "请审查下面这段代码是否存在并发竞争和资源泄漏",
    "帮我实现一个带登录、权限管理和数据导出的后台系统",
)


DO_NOT_SUBMIT = (
    # Ordinary conversation and task descriptions are not delegated work.
    "你好",
    "今天天气怎么样",
    "2 加 3 等于多少",
    "帮我搜索一下 AstrBot 的官网",
    "帮我翻译这段英文",
    "Codex 是什么？",
    "你能调用 Codex 吗？",
    "怎么用 Codex？",
    "我昨天用 Codex 修好了这个问题",
    "帮我解释 Codex 和 ChatGPT 的区别",
    "帮我看一下 Codex 的官网",
    "我昨天修复了代码",
    "这个项目已经部署好了",
    "代码审查有什么用",
    "这段程序怎么运行",
    "需要实现登录功能吗",
    "不用修复了，告诉我测试结果即可",
    "帮我解释如何重构代码",
    "我已经完成代码审查了",
    "我们上周调研比较了三种方案，报告已经发出",
    # A quoted command or discussion must not turn into execution.
    "他说用 Codex 帮我检查这个项目",
    "转发：请用 Codex 修复这个项目的报错",
    "引用：用 Codex 分析这个仓库",
    "“用 Codex 检查代码”这句话是什么意思？",
    "例如：用 Codex 帮我审查代码",
    "如果我说用 Codex 检查代码，你会怎么做？",
    # Negation applies to routing, including instructions with other actions.
    "不要用 Codex，直接告诉我答案",
    "别让 Codex 处理，帮我解释一下这个报错",
    "不用 codex，帮我分析这个项目的代码",
    "请不要调用 Codex 审查代码",
    # Reminder and image plugins own these requests.
    "两分钟后提醒我喝水",
    "给我定个两分钟后的提醒",
    "两分钟后提醒我用 Codex 检查代码",
    "帮我设置明天上午的代码审查提醒",
    "按照这张图上的日程提前十分钟提醒我",
    "把刚才图片里的每个日程都提前十分钟设个提醒",
    "算一下刚才那个图，多少热量",
    "图发你了，算一下多少热量",
    "分析一下这张图片有什么食物",
    "帮我识别这张截图里的文字",
    # Explicit slash commands have their existing command handler.
    "/codex 分析这个项目",
    "/codex write 修复测试",
    "/提醒 2分钟后 喝水",
    # Status queries must keep their owner-scoped status path.
    "Codex 任务现在到哪了？",
    "看下 Codex 当前任务的状态",
    "Codex 的结果呢？",
)


class IntentTests(unittest.TestCase):
    def test_explicit_requests_preserve_original_task_and_read_only_default(self):
        for text in EXPLICIT_READ_ONLY:
            with self.subTest(text=text):
                self.assertEqual(core.natural_codex_request(text), ("read-only", text))

    def test_explicit_write_mode_is_detected_without_other_escalation(self):
        for text in EXPLICIT_WRITE:
            with self.subTest(text=text):
                self.assertEqual(core.natural_codex_request(text), ("workspace-write", text))

    def test_mode_mentions_or_write_prohibitions_never_escalate(self):
        for text in MODE_MENTIONS_READ_ONLY:
            with self.subTest(text=text):
                self.assertEqual(core.natural_codex_request(text), ("read-only", text))

    def test_complex_tasks_can_delegate_without_naming_codex(self):
        for text in COMPLEX_READ_ONLY:
            with self.subTest(text=text):
                self.assertEqual(core.natural_codex_request(text), ("read-only", text))

    def test_non_commands_and_other_plugin_requests_are_not_submitted(self):
        for text in DO_NOT_SUBMIT:
            with self.subTest(text=text):
                self.assertIsNone(core.natural_codex_request(text))


if __name__ == "__main__":
    unittest.main()
