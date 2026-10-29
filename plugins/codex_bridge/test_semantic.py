"""Hard routing boundaries for model-selected delegation, without model calls."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import types
import unittest


package = types.ModuleType("codex_semantic_tests")
package.__path__ = [str(Path(__file__).parent)]
sys.modules[package.__name__] = package
for leaf in ("core", "semantic"):
    spec = importlib.util.spec_from_file_location(package.__name__ + "." + leaf, Path(__file__).with_name(leaf + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
semantic = module


class SemanticTests(unittest.TestCase):
    def test_tool_cannot_accept_model_generated_task_or_scope(self):
        self.assertEqual(semantic.TOOL_SCHEMA["properties"], {})
        self.assertIs(semantic.TOOL_SCHEMA["additionalProperties"], False)

    def test_explicit_opt_out_blocks_semantic_escape(self):
        for text in (
            "不要让 Codex 分析这个项目",
            "不需要使用 Codex，请直接回答",
            "不用 codex，帮我分析这个项目的代码",
            "Please don't use Codex to review this function.",
            "Do not call Codex to run this task.",
            "Analyze this project without Codex.",
            "Never invoke Codex for this request.",
            "不要使用 ＣＯＤＥＸ，直接回答即可",
        ):
            with self.subTest(text=text):
                self.assertFalse(semantic.semantic_eligible(text))

    def test_reminder_requests_stay_local(self):
        for text in (
            "两分钟后提醒我用 Codex 检查代码",
            "给我定个两分钟后的喝水提醒",
            "Remind me in two minutes to use Codex.",
            "让 Codex 按照截图里的日程安排提醒",
        ):
            with self.subTest(text=text):
                self.assertFalse(semantic.semantic_eligible(text))

    def test_explicit_codex_images_eligible_while_local_vision_stays_local(self):
        for text in ("请用 Codex 分析刚才那张图片", "图发给你了，请调用 Codex 计算热量", "用 Codex 标注这张图里的品牌和用途"):
            with self.subTest(text=text):
                self.assertTrue(semantic.semantic_eligible(text))
        for text in ("分析刚才那张图片", "图发给你了，计算热量", "看看截图里是什么"):
            with self.subTest(text=text):
                self.assertFalse(semantic.semantic_eligible(text))

    def test_quotes_slash_commands_and_capability_queries_stay_out(self):
        for text in (
            "/codex 调研三个开源框架",
            "“用 Codex 检查代码”这句话是什么意思？",
            "我转发一下：用 Codex 修复这个项目",
            "下面是引用：让 Codex 做完这个任务",
            "他说用 Codex 帮我检查这个项目",
            "Quote: use Codex to review this function.",
            "如果我说用 Codex 检查代码，你会怎么做？",
            "怎么用 Codex？",
            "Codex 是什么？",
            "你能调用 Codex 吗？",
            "What is Codex?",
            "看下 Codex 当前任务的状态",
            "Codex 的结果呢？",
        ):
            with self.subTest(text=text):
                self.assertFalse(semantic.semantic_eligible(text))

    def test_task_words_do_not_disable_semantic_fallback(self):
        for text in (
            "请安排 Codex 帮我实现登录功能",
            "交给 Codex 调研 Python 和 Go 的区别并给我一份报告",
            "请使用 Codex 研究为什么官方文档建议不要使用旧接口",
            "我需要对三种数据库做基准测试与成本分析，再给出选型建议",
            "Use Codex to explain why this algorithm is slow and propose a fix.",
            "能不能安排 Codex 帮我核验所有数据源并汇总成比较报告？",
        ):
            with self.subTest(text=text):
                self.assertTrue(semantic.semantic_eligible(text))


if __name__ == "__main__":
    unittest.main()
