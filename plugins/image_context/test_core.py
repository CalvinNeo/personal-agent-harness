from pathlib import Path
import tempfile
import unittest

from core import (
    BATCH_GAP_SECONDS, IMPLICIT_REFERENCE_SECONDS, MAX_IMAGES, MAX_SESSIONS, TTL_SECONDS,
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
        for text, code in (("刚才20张图", "count_out_of_range"),
                           ("上一张图和前一张图", "ambiguous"),
                           ("刚才0张图", "count_out_of_range")):
            with self.subTest(text=text), self.assertRaises(ImageReferenceError) as error:
                parse_reference(text)
            self.assertEqual(error.exception.code, code)
            self.assertEqual(str(error.exception), error.exception.user_message)

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

    def test_forced_reply_batch_does_not_merge_with_recent_uploads(self):
        # A direct reply can select a new original or re-quote one of the
        # recent originals. Both cases must scope a broad follow-up to only
        # that explicit selection, even inside the normal upload batch gap.
        for quoted in ([ref(3), ref(4)], [ref(1)]):
            with self.subTest(quoted=quoted):
                cache = ImageCache()
                cache.remember(self.key, [ref(1)], 0)
                cache.remember(self.key, [ref(2)], 30)
                cache.remember(self.key, quoted, 31, new_batch=True)
                self.assertEqual(cache.select(self.key, ImageSelection(), 32), quoted)
                self.assertEqual(cache.select(self.key, ImageSelection(1), 32), quoted[-1:])

    def test_reply_batch_reads_do_not_extend_receipt_ttl_or_implicit_freshness(self):
        received_at, received_wall = 100, 10_000
        self.cache.remember(self.key, [ref(1)], 0, wall_now=received_wall - received_at)
        self.cache.remember(self.key, [ref(2)], received_at, wall_now=received_wall, new_batch=True)
        recent_read = received_at + IMPLICIT_REFERENCE_SECONDS - 1
        self.assertTrue(self.cache.has_recent(self.key, recent_read))
        self.assertEqual(self.cache.select(self.key, ImageSelection(), recent_read), [ref(2)])
        self.assertFalse(self.cache.has_recent(self.key, received_at + IMPLICIT_REFERENCE_SECONDS))
        before_expiry = received_at + TTL_SECONDS - 1
        self.assertEqual(self.cache.select(self.key, ImageSelection(), before_expiry), [ref(2)])
        saved = self.cache.export(wall_now=received_wall + TTL_SECONDS - 1, now=before_expiry)
        self.assertEqual(saved[0]["entries"][0]["received_wall_at"], received_wall)
        with self.assertRaises(ImageReferenceError) as error:
            self.cache.select(self.key, ImageSelection(), received_at + TTL_SECONDS)
        self.assertEqual(error.exception.code, "cache_missing")

    def test_isolation_and_missing_image_error(self):
        self.cache.remember(self.key, [ref(1)], 0)
        for key in [(self.key[0], "sender-b"), ("qq:GroupMessage:group-b", self.key[1])]:
            with self.assertRaises(ImageReferenceError) as error:
                self.cache.select(key, ImageSelection(), 1)
            self.assertEqual(error.exception.code, "cache_missing")
            self.assertEqual(str(error.exception), "当前未收到你在这个会话里可用的图片；请直接引用原始图片消息并 @我，或把图片和 @ 放在同一条消息重发。")
        with self.assertRaises(ImageReferenceError) as error:
            self.cache.select(self.key, ImageSelection(2), 1)
        self.assertEqual(error.exception.code, "count_mismatch")
        self.assertIn("当前只有 1 张可用图片，不足你要求的 2 张", error.exception.user_message)

    def test_expiry_does_not_refresh_on_read(self):
        self.cache.remember(self.key, [ref(1)], 0)
        self.cache.select(self.key, ImageSelection(), TTL_SECONDS - 1)
        with self.assertRaises(ImageReferenceError) as error:
            self.cache.select(self.key, ImageSelection(), TTL_SECONDS)
        self.assertEqual(error.exception.code, "cache_missing")

    def test_disappeared_local_image_has_unavailable_reason_without_its_path(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            image = root / "sensitive-original.png"
            image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 8)
            cache = ImageCache(root)
            cache.remember(self.key, [str(image)], 0)
            image.unlink()
            with self.assertRaises(ImageReferenceError) as error:
                cache.select(self.key, ImageSelection(), 1)
            self.assertEqual(error.exception.code, "unavailable")
            self.assertIn("已不可用", error.exception.user_message)
            self.assertNotIn(str(image), str(error.exception))

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


class ErrorContractTests(unittest.TestCase):
    def test_only_supported_reason_codes_produce_user_messages(self):
        denied = ImageReferenceError("denied")
        self.assertEqual(denied.code, "denied")
        self.assertIn("无权读取", denied.user_message)
        secret = "https://gchat.qpic.cn/private?token=secret /private/cache.png"
        unknown = ImageReferenceError(secret)
        self.assertEqual(unknown.code, "unknown")
        self.assertEqual(str(unknown), unknown.user_message)
        self.assertNotIn("secret", str(unknown))
        self.assertNotIn("/private", str(unknown))

    def test_count_details_accept_only_bounded_integers(self):
        error = ImageReferenceError("count_mismatch", available="/private/image.png", requested=True)
        self.assertIn("数量超过", error.user_message)
        self.assertNotIn("/private", error.user_message)


if __name__ == "__main__":
    unittest.main()
