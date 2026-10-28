import unittest

from core import (
    BATCH_GAP_SECONDS, MAX_IMAGES, MAX_SESSIONS, TTL_SECONDS,
    ImageCache, ImageReferenceError, ImageSelection,
    is_safe_image_ref, parse_reference, without_historical_images,
)


def ref(index):
    return f"https://multimedia.nt.qq.com.cn/download?fileid={index}"


class ReferenceTests(unittest.TestCase):
    def test_explicit_image_references(self):
        for text, count in [
            ("帮我看看上一张图", 1), ("分析刚才三张图", 3),
            ("看看我刚发的那几张图", None), ("刚发的那几张图都有什么", None),
            ("刚刚的3张图片", 3), ("上一张照片清晰吗", 1),
            ("对比之前两张图片", 2), ("刚才十二张图", 12), ("刚才那张图", 1), ("刚才的图", None),
        ]:
            with self.subTest(text=text):
                self.assertEqual(parse_reference(text), ImageSelection(count))

    def test_regular_chat_and_commands_are_ignored(self):
        for text in ("今天天气怎么样", "图片功能怎么用", "请你画一张图", "/codex 分析上一张图", "看图说话"):
            self.assertIsNone(parse_reference(text))

    def test_ambiguous_or_oversize_reference(self):
        for text in ("刚才20张图", "上一张图和前一张图", "刚才0张图"):
            with self.assertRaises(ImageReferenceError):
                parse_reference(text)

    def test_url_constraints(self):
        self.assertTrue(is_safe_image_ref(ref(1)))
        self.assertTrue(is_safe_image_ref("https://gchat.qpic.cn/example.jpg"))
        for url in ("http://gchat.qpic.cn/x", "https://gchat.qpic.cn.evil.com/x", "file:///etc/passwd",
                    "https://127.0.0.1/x", "https://user:pwd@" + "gchat.qpic.cn/x", "https://gchat.qpic.cn:8080/x",
                    "https://gchat.qpic.cn/x#fragment", "https://example.com/x", "data:image/png;base64,abc", "https://gchat.qpic.cn/\nx"):
            self.assertFalse(is_safe_image_ref(url), url)


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.cache = ImageCache()
        self.key = ("qq:GroupMessage:group-a", "sender-a")

    def test_continuous_uploads_accumulate_in_order(self):
        self.cache.remember(self.key, [ref(1), ref(2)], 0)
        self.cache.remember(self.key, [ref(3)], 30)
        self.assertEqual(self.cache.select(self.key, ImageSelection(), 40), [ref(1), ref(2), ref(3)])
        self.assertEqual(self.cache.select(self.key, ImageSelection(1), 40), [ref(3)])
        self.assertEqual(self.cache.select(self.key, ImageSelection(2), 40), [ref(2), ref(3)])

    def test_separate_batch_and_explicit_count(self):
        self.cache.remember(self.key, [ref(1)], 0)
        self.cache.remember(self.key, [ref(2), ref(3)], BATCH_GAP_SECONDS + 1)
        self.assertEqual(self.cache.select(self.key, ImageSelection(), 200), [ref(2), ref(3)])
        self.assertEqual(self.cache.select(self.key, ImageSelection(3), 200), [ref(1), ref(2), ref(3)])

    def test_isolation_and_missing_image_error(self):
        self.cache.remember(self.key, [ref(1)], 0)
        for key in [(self.key[0], "sender-b"), ("qq:GroupMessage:group-b", self.key[1])]:
            with self.assertRaises(ImageReferenceError):
                self.cache.select(key, ImageSelection(), 1)
        with self.assertRaises(ImageReferenceError):
            self.cache.select(self.key, ImageSelection(2), 1)

    def test_expiry_does_not_refresh_on_read(self):
        self.cache.remember(self.key, [ref(1)], 0)
        self.cache.select(self.key, ImageSelection(), TTL_SECONDS - 1)
        with self.assertRaises(ImageReferenceError):
            self.cache.select(self.key, ImageSelection(), TTL_SECONDS)

    def test_image_and_session_limits(self):
        for index in range(MAX_IMAGES + 5):
            self.cache.remember(self.key, [ref(index)], index)
        self.assertEqual(self.cache.select(self.key, ImageSelection(), 50), [ref(i) for i in range(5, 17)])
        for index in range(MAX_SESSIONS):
            self.cache.remember((self.key[0], f"new-sender-{index}"), [ref(index)], 60 + index)
        with self.assertRaises(ImageReferenceError):
            self.cache.select(self.key, ImageSelection(), 130)
        self.assertEqual(len(self.cache._sessions), MAX_SESSIONS)

    def test_duplicates_and_bad_refs_not_stored(self):
        self.cache.remember(self.key, [ref(1), ref(1), "file:///tmp/x"], 0)
        self.cache.remember(self.key, [ref(1)], 20)
        self.assertEqual(self.cache.select(self.key, ImageSelection(), 30), [ref(1)])

    def test_history_sanitization_copies_only_request(self):
        original = [{"role": "user", "content": [{"type": "text", "text": "Earlier message"},
                     {"type": "image_url", "image_url": {"url": ref(9)}}]},
                    {"role": "user", "content": [{"type": "input_image", "image_url": ref(8)}]},
                    {"role": "assistant", "content": "previous answer"}]
        result = without_historical_images(original)
        self.assertEqual(len(original[0]["content"]), 2)
        self.assertEqual(len(result[0]["content"]), 1)
        self.assertEqual(result[1]["content"][0]["type"], "text")
        self.assertEqual(result[2], original[2])
        result[0]["content"][0]["text"] = "changed"
        self.assertEqual(original[0]["content"][0]["text"], "Earlier message")


if __name__ == "__main__":
    unittest.main()
