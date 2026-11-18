import ast
import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "qq_text_mentions", ROOT / "patches/astrbot-qq-mentions/text_mentions.py"
)
mentions = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mentions)


class TextMentionTests(unittest.TestCase):
    def setUp(self):
        self.aliases = mentions.normalize_text_mention_aliases(["示例智能体", "test.bot+(one)"])

    def test_exact_prefix_is_removed_for_all_text_consumers(self):
        for prefix in ("@示例智能体 ", "＠示例智能体：", " \t@示例智能体\u2005", "@示例智能体, ", "@示例智能体\n", "\u200b@示例智能体\u200b"):
            for command in ("你好", "十分钟后提醒我喝水", "记住我喜欢简短回答", "/help"):
                with self.subTest(prefix=prefix, command=command):
                    self.assertEqual(mentions.strip_text_mention_prefix(prefix + command, self.aliases), (command, "示例智能体"))

    def test_empty_mention_can_use_existing_waiting_behavior(self):
        for text in ("@示例智能体", "＠示例智能体： ", "@示例智能体\u2005"):
            with self.subTest(text=text):
                self.assertEqual(mentions.strip_text_mention_prefix(text, self.aliases), ("", "示例智能体"))

    def test_nonmatching_messages_are_unchanged(self):
        for text in (
            "普通聊天", "@其他人 你好", "@所有人 你好", "@示例智能体二号 你好",
            "@示例智能体你好", "提到 @示例智能体 但不是唤醒", "`@示例智能体 你好`",
            "```\n@示例智能体 你好\n```", "> @示例智能体 你好", '"@示例智能体 你好"',
            '<qqbot-at-user id="synthetic-other"/> 你好', "<@synthetic-other> 你好",
            "@testXbot+(one) 你好", "@示例智能 你好",
        ):
            with self.subTest(text=text):
                self.assertEqual(mentions.strip_text_mention_prefix(text, self.aliases), (text, ""))

    def test_remainder_keeps_its_original_content(self):
        command = "请解释 `@其他人`\n  保留缩进和全角ＡＢＣ"
        self.assertEqual(mentions.strip_text_mention_prefix("@示例智能体 " + command, self.aliases), (command, "示例智能体"))

    def test_aliases_are_literal_and_longest_first(self):
        self.assertEqual(mentions.strip_text_mention_prefix("@test.bot+(one)：你好", self.aliases), ("你好", "test.bot+(one)"))
        aliases = mentions.normalize_text_mention_aliases(["示例", "示例 智能体"])
        self.assertEqual(mentions.strip_text_mention_prefix("@示例 智能体 你好", aliases), ("你好", "示例 智能体"))

    def test_missing_or_invalid_configuration_cannot_enable_aliases(self):
        for value in (None, "示例智能体", {}, 1, [], [None, 1, "", " ", "@示例智能体", "＠示例智能体", "bad\nname", "bad\tname", "x" * 129]):
            with self.subTest(value=value):
                aliases = mentions.normalize_text_mention_aliases(value)
                self.assertEqual(aliases, ())
                self.assertEqual(mentions.strip_text_mention_prefix("@示例智能体 你好", aliases), ("@示例智能体 你好", ""))

    def test_configuration_is_trimmed_deduplicated_and_not_mutated(self):
        value = [" 示例智能体 ", "示例智能体", "another"]
        self.assertEqual(mentions.normalize_text_mention_aliases(value), ("another", "示例智能体"))
        self.assertEqual(value, [" 示例智能体 ", "示例智能体", "another"])

    def test_cumulative_patch_mirrors_the_tested_helper(self):
        lines = (ROOT / "patches/astrbot-4.29.3-customizations.patch").read_text().splitlines(keepends=True)
        marker = "+++ b/astrbot/core/platform/sources/qqofficial/text_mentions.py\n"
        start = lines.index(marker) + 1
        added = []
        for line in lines[start:]:
            if line.startswith("--- "):
                break
            if line.startswith("+"):
                added.append(line[1:])
        source = "".join(added)
        self.assertEqual(source, (ROOT / "patches/astrbot-qq-mentions/text_mentions.py").read_text())
        ast.parse(source)


if __name__ == "__main__":
    unittest.main()
