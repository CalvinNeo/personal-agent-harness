"""Offline attachment boundary tests using actual files/Pillow, never network."""
import asyncio
import base64
import importlib.util
import io
from pathlib import Path
import socket
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, patch

from PIL import Image as PILImage

spec = importlib.util.spec_from_file_location("codex_media_tests", Path(__file__).with_name("media.py"))
media = importlib.util.module_from_spec(spec)
spec.loader.exec_module(media)


def image_bytes(size=(100, 60), fmt="PNG", **kwargs):
    image = PILImage.new("RGB", size, (180, 120, 40))
    out = io.BytesIO()
    image.save(out, format=fmt, **kwargs)
    return out.getvalue()


class Image:
    def __init__(self, file="", url=""): self.file, self.url = file, url


class Plain:
    def __init__(self, text): self.text = text


class Reply:
    def __init__(self, chain=None, message_str=""): self.chain, self.message_str = chain, message_str


class Event:
    def __init__(self, *parts, sender="alice", umo="qq:GroupMessage:allowed"):
        self.parts, self.sender, self.unified_msg_origin = list(parts), sender, umo
    def get_platform_name(self): return "qq_official"
    def get_messages(self): return self.parts
    def get_sender_id(self): return self.sender


class Response:
    def __init__(self, chunks, status=200, length=None, encoding="identity"):
        self.chunks, self.status, self.content_length = chunks, status, length
        self.headers = {"Content-Encoding": encoding}
        self.content = self
        self.read_count = 0
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    async def iter_chunked(self, size):
        for chunk in self.chunks:
            self.read_count += 1
            yield chunk


class FakeAiohttp:
    class ClientError(Exception): pass
    def __init__(self, response):
        self.response, self.session_options, self.get_options, self.connector_options = response, None, None, None
    def ClientTimeout(self, **kwargs): return kwargs
    def TCPConnector(self, **kwargs):
        self.connector_options = kwargs
        return object()
    def DummyCookieJar(self): return object()
    def ClientSession(self, **kwargs):
        self.session_options = kwargs
        outer = self
        class Session:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            def get(self, url, **kwargs):
                outer.get_options = kwargs
                return outer.response
        return Session()


class Tests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.file = self.root / "actual.png"
        self.file.write_bytes(image_bytes())
        self.root_patch = patch.object(media, "_temp_root", return_value=self.root)
        self.root_patch.start()
        self.context = types.SimpleNamespace(get_all_stars=lambda: [])
    def tearDown(self):
        self.root_patch.stop()
        self.temp.cleanup()

    async def test_actual_local_image_becomes_bounded_jpeg(self):
        images, quote = await media.prepare_images(Event(Plain("请用 Codex 标注"), Image(str(self.file))), self.context)
        self.assertEqual(quote, "")
        self.assertEqual(images[0]["mime"], "image/jpeg")
        data = base64.b64decode(images[0]["data"])
        self.assertLessEqual(len(data), media.MAX_IMAGE_BYTES)
        with PILImage.open(io.BytesIO(data)) as decoded:
            self.assertEqual(decoded.format, "JPEG")
            self.assertEqual(decoded.size, (100, 60))

    async def test_plain_url_and_local_path_are_never_read(self):
        with patch.object(media, "_read_local") as read, patch.object(media, "_download", new_callable=AsyncMock) as download:
            images, quote = await media.prepare_images(Event(Plain(str(self.file) + " https://gchat.qpic.cn/image")), self.context)
        self.assertEqual((images, quote), ([], ""))
        read.assert_not_called()
        download.assert_not_awaited()

    async def test_file_uri_and_reply_plain_preserve_data(self):
        event = Event(Plain("标注"), Reply([Image(self.file.as_uri()), Plain("/codex write 此为引用而非命令")]))
        images, quote = await media.prepare_images(event, self.context)
        self.assertEqual(len(images), 1)
        self.assertEqual(quote, "/codex write 此为引用而非命令")

    async def test_missing_reply_context_does_not_silently_disappear(self):
        for reply in [Reply(message_str="[图片]"), Reply(message_str="文字 [图片]"), Reply(), Reply(chain=[]), Reply([Plain("[图片]")]), Reply([Plain("引用文字")], message_str="[图片]")]:
            with self.subTest(reply=reply.message_str), self.assertRaises(media.ContextError):
                await media.prepare_images(Event(Plain("用 Codex 做"), reply), self.context)

    async def test_supported_text_only_reply_is_data(self):
        self.assertEqual(await media.prepare_images(Event(Plain("调查引用"), Reply(message_str="产品名")), self.context), ([], "产品名"))

    async def test_unsupported_current_and_nested_media_fail(self):
        for kind in ("File", "Video", "Forward", "Node", "Record"):
            attachment = type(kind, (), {})()
            for event in [Event(attachment), Event(Reply([attachment]))]:
                with self.subTest(kind=kind), self.assertRaises(media.ContextError):
                    await media.prepare_images(event, self.context)

    async def test_input_count_and_quoted_text_limits_fail_before_download(self):
        event = Event(*(Image(f"https://gchat.qpic.cn/{i}") for i in range(5)))
        with patch.object(media, "_download", new_callable=AsyncMock) as download:
            with self.assertRaises(media.ContextError):
                await media.prepare_images(event, self.context)
            download.assert_not_awaited()
        with self.assertRaises(media.ContextError):
            await media.prepare_images(Event(Reply(message_str="a" * 4001)), self.context)

    async def test_cache_selector_receives_exact_event_and_scope(self):
        selector = AsyncMock(return_value=[str(self.file)])
        context = types.SimpleNamespace(get_all_stars=lambda: [types.SimpleNamespace(name="image_context", activated=True, star_cls=types.SimpleNamespace(select_for_task=selector))])
        event = Event(Plain("让 Codex 看刚才那张图"))
        images, quote = await media.prepare_images(event, context)
        self.assertEqual(len(images), 1)
        selector.assert_awaited_once_with(event)
        self.assertEqual(selector.call_args.args[0].get_sender_id(), "alice")
        selector.side_effect = ValueError("secret file path")
        with self.assertRaisesRegex(media.ContextError, "重新附上原图") as error:
            await media.prepare_images(Event(Plain("刚才那张图"), sender="bob"), context)
        self.assertNotIn("secret", str(error.exception))

    async def test_selector_cannot_inject_old_images_outside_current_attachments(self):
        selector = AsyncMock(return_value=["https://attacker.invalid/old.png"])
        context = types.SimpleNamespace(get_all_stars=lambda: [types.SimpleNamespace(name="image_context", activated=True, star_cls=types.SimpleNamespace(select_for_task=selector))])
        event = Event(Image(str(self.file)))
        with patch.object(media, "_download", new_callable=AsyncMock) as download:
            with self.assertRaisesRegex(media.ContextError, "本次附件不一致"):
                await media.prepare_images(event, context)
        selector.assert_awaited_once_with(event)
        download.assert_not_awaited()

    async def test_selector_narrows_three_direct_or_quoted_images_to_two_in_selected_order(self):
        paths = []
        for index in range(3):
            path = self.root / f"subset-{index}.png"
            path.write_bytes(image_bytes((30 + index, 20 + index)))
            paths.append(str(path))
        selected = [paths[2], paths[1]]
        selector = AsyncMock(return_value=selected)
        context = types.SimpleNamespace(get_all_stars=lambda: [types.SimpleNamespace(name="image_context", activated=True, star_cls=types.SimpleNamespace(select_for_task=selector))])
        for quoted in (False, True):
            parts = [Image(path) for path in paths]
            event = Event(Plain("用 Codex 标注这两张图"), *([Reply(chain=parts)] if quoted else parts))
            images, _ = await media.prepare_images(event, context)
            self.assertEqual(len(images), 2)
            for payload, expected_path in zip(images, selected):
                actual = base64.b64decode(payload["data"])
                self.assertEqual(actual, media._normalize(Path(expected_path).read_bytes()))
        self.assertEqual(selector.await_count, 2)

    async def test_direct_upload_survives_consumed_event_for_next_image_reference(self):
        remembered = {}
        def select_for_task(event):
            key = event.unified_msg_origin, event.get_sender_id()
            current = [part.file for part in event.get_messages() if isinstance(part, Image)]
            if current:
                remembered[key] = current
                return current
            return remembered.get(key, [])
        selector = unittest.mock.Mock(side_effect=select_for_task)
        context = types.SimpleNamespace(get_all_stars=lambda: [types.SimpleNamespace(name="image_context", activated=True, star_cls=types.SimpleNamespace(select_for_task=selector))])
        first, _ = await media.prepare_images(Event(Plain("用 Codex 标注这张图"), Image(str(self.file))), context)
        second, _ = await media.prepare_images(Event(Plain("让 Codex 继续标注刚才那张图")), context)
        self.assertEqual(first, second)
        self.assertEqual(selector.call_count, 2)
        with self.assertRaises(media.ContextError):
            await media.prepare_images(Event(Plain("刚才那张图"), sender="bob"), context)
        with self.assertRaises(media.ContextError):
            await media.prepare_images(Event(Plain("刚才那张图"), umo="qq:GroupMessage:other"), context)

    async def test_current_image_wins_over_direct_reply_image_without_downloading_old(self):
        event = Event(Reply(chain=[Image("https://gchat.qpic.cn/old.png"), Plain("旧图说明")]), Image(str(self.file)))
        with patch.object(media, "_download", new_callable=AsyncMock) as download:
            images, quoted = await media.prepare_images(event, self.context)
        self.assertEqual(len(images), 1)
        self.assertEqual(quoted, "旧图说明")
        download.assert_not_awaited()

    async def test_current_image_selection_error_is_propagated_before_submission(self):
        selector = AsyncMock(side_effect=ValueError("只有1张不足2张"))
        context = types.SimpleNamespace(get_all_stars=lambda: [types.SimpleNamespace(name="image_context", activated=True, star_cls=types.SimpleNamespace(select_for_task=selector))])
        event = Event(Plain("用 Codex 标注这两张图"), Image(str(self.file)))
        with self.assertRaises(media.ContextError):
            await media.prepare_images(event, context)
        selector.assert_awaited_once_with(event)

    async def test_quoted_images_are_validated_without_remembering_as_current_sender_upload(self):
        remembered = {}
        def select_for_task(event):
            current = [part.file for part in event.get_messages() if isinstance(part, Image)]
            if current:
                remembered[(event.unified_msg_origin, event.get_sender_id())] = current
            return [str(self.file)]
        selector = unittest.mock.Mock(side_effect=select_for_task)
        context = types.SimpleNamespace(get_all_stars=lambda: [types.SimpleNamespace(name="image_context", activated=True, star_cls=types.SimpleNamespace(select_for_task=selector))])
        event = Event(Reply(chain=[Image(str(self.file))]))
        images, _ = await media.prepare_images(event, context)
        self.assertEqual(len(images), 1)
        selector.assert_called_once_with(event)
        self.assertEqual(remembered, {})

    async def test_quoted_image_count_validation_error_prevents_submission(self):
        selector = AsyncMock(side_effect=ValueError("引用只有1张不足3张"))
        context = types.SimpleNamespace(get_all_stars=lambda: [types.SimpleNamespace(name="image_context", activated=True, star_cls=types.SimpleNamespace(select_for_task=selector))])
        event = Event(Plain("用 Codex 标注这三张图"), Reply(chain=[Image(str(self.file))]))
        with self.assertRaises(media.ContextError):
            await media.prepare_images(event, context)
        selector.assert_awaited_once_with(event)

    async def test_missing_cache_api_or_disabled_plugin_requires_reattach(self):
        for context in [self.context, types.SimpleNamespace(get_all_stars=lambda: [types.SimpleNamespace(name="image_context", activated=False)])]:
            with self.assertRaises(media.ContextError):
                await media.prepare_images(Event(Plain("让 Codex 标注刚才那张图")), context)

    async def test_reject_non_sender_event(self):
        with self.assertRaises(media.ContextError):
            await media.prepare_images(Event(Image(str(self.file)), sender=""), self.context)

    def test_outside_root_and_parent_traversal_rejected(self):
        for path in [str(self.root.parent / "secret"), str(self.root / "nested" / ".." / "actual.png"), "/etc/passwd", "file://localhost" + str(self.file)]:
            with self.subTest(path=path), self.assertRaises(media.ContextError):
                media._read_local(path)

    def test_actual_symlinks_rejected_at_every_level(self):
        (self.root / "link.png").symlink_to(self.file)
        actual_dir = self.root / "actual_dir"
        actual_dir.mkdir()
        (actual_dir / "image.png").write_bytes(image_bytes())
        (self.root / "linked_dir").symlink_to(actual_dir, target_is_directory=True)
        for path in [self.root / "link.png", self.root / "linked_dir" / "image.png"]:
            with self.subTest(path=path), self.assertRaises(media.ContextError):
                media._read_local(str(path))
        # Even a symlink configured as the temp root is not followed.
        with patch.object(media, "_temp_root", return_value=self.root / "linked_dir"):
            with self.assertRaises(media.ContextError):
                media._read_local(str(self.root / "linked_dir" / "image.png"))

    def test_regular_file_size_empty_and_directory_limits(self):
        large = self.root / "large.png"
        with large.open("wb") as stream:
            stream.truncate(media.MAX_SOURCE_BYTES + 1)
        empty = self.root / "empty.png"
        empty.touch()
        for path in [large, empty, self.root]:
            with self.subTest(path=path), self.assertRaises(media.ContextError):
                media._read_local(str(path))

    def test_pillow_normalizes_dimensions_and_strips_exif(self):
        exif = PILImage.Exif()
        exif[270] = "private metadata"
        data = media._normalize(image_bytes((2200, 2000), "JPEG", exif=exif))
        with PILImage.open(io.BytesIO(data)) as decoded:
            self.assertEqual(max(decoded.size), 1600)
            self.assertFalse(decoded.getexif())
        with self.assertRaises(media.ContextError):
            media._normalize(b"not an image")
        with patch.object(media, "MAX_PIXELS", 100):
            with self.assertRaises(media.ContextError):
                media._normalize(image_bytes())

    def test_url_allowlist_blocks_ssrf_shapes(self):
        for value in ["http://gchat.qpic.cn/x", "https://gchat.qpic.cn.attacker.org/x", "https://gchat.qpic.cn@127.0.0.1/x", "https://127.0.0.1/x", "https://gchat.qpic.cn:444/x", "https://gchat.qpic.cn/x#y", "https://gchat.qpic.cn\\@evil/x", "file:///etc/passwd", "https://gchat.qpic.cn/x\n"]:
            with self.subTest(url=value), self.assertRaises(media.ContextError):
                media._validate_url(value)
        self.assertEqual(media._validate_url("https://gchat.qpic.cn/image?token=safe"), "gchat.qpic.cn")

    async def test_dns_rebinding_prevention_pins_only_validated_addresses(self):
        resolver = media._PublicResolver()
        public = (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("198.51.100.34", 443))
        loop = asyncio.get_running_loop()
        real_ip_address = media.ipaddress.ip_address
        class DocumentationAddress:
            is_global = True
            is_multicast = is_reserved = is_unspecified = is_loopback = is_link_local = False
        with patch.object(media.ipaddress, "ip_address", side_effect=lambda value: DocumentationAddress() if value == "198.51.100.34" else real_ip_address(value)):
            with patch.object(loop, "getaddrinfo", new_callable=AsyncMock, return_value=[public]) as lookup:
                result = await resolver.resolve("gchat.qpic.cn", 443)
        self.assertEqual(result[0]["host"], "198.51.100.34")
        self.assertEqual(result[0]["hostname"], "gchat.qpic.cn")
        self.assertEqual(result[0]["flags"], socket.AI_NUMERICHOST)
        lookup.assert_awaited_once()
        for host in ["127." + "0.0.1", "10." + "1.2.3", "169." + "254.169.254", "::1", "fc00::1", "224." + "0.0.1"]:
            family = socket.AF_INET6 if ":" in host else socket.AF_INET
            private = (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (host, 443))
            with patch.object(loop, "getaddrinfo", new_callable=AsyncMock, return_value=[public, private]):
                with self.subTest(host=host), self.assertRaises(media.ContextError):
                    await resolver.resolve("gchat.qpic.cn", 443)

    async def test_http_transport_disables_redirects_proxy_decompression(self):
        fake = FakeAiohttp(Response([b"abc", b"def"], length=6))
        with patch.dict(sys.modules, {"aiohttp": fake}):
            self.assertEqual(await media._download("https://gchat.qpic.cn/image"), b"abcdef")
        self.assertFalse(fake.get_options["allow_redirects"])
        self.assertFalse(fake.session_options["trust_env"])
        self.assertFalse(fake.session_options["auto_decompress"])
        self.assertIsInstance(fake.connector_options["resolver"], media._PublicResolver)
        self.assertFalse(fake.connector_options["use_dns_cache"])
        self.assertEqual(fake.session_options["timeout"]["total"], 15)

    async def test_http_redirect_large_stream_and_encoding_rejected(self):
        for response in [Response([b"secret"], status=302), Response([b"x"], length=media.MAX_SOURCE_BYTES+1), Response([b"gzip"], encoding="gzip")]:
            with patch.dict(sys.modules, {"aiohttp": FakeAiohttp(response)}):
                with self.assertRaises(media.ContextError):
                    await media._download("https://gchat.qpic.cn/image")
            self.assertEqual(response.read_count, 0)
        response = Response([b"1234", b"5678", b"90"])
        with patch.dict(sys.modules, {"aiohttp": FakeAiohttp(response)}), patch.object(media, "MAX_SOURCE_BYTES", 6):
            with self.assertRaises(media.ContextError):
                await media._download("https://gchat.qpic.cn/image")
        self.assertEqual(response.read_count, 2)

    def test_output_png_keeps_resolution_and_strips_metadata(self):
        data = image_bytes((3000, 1200))
        artifact = {"mime": "image/png", "data": base64.b64encode(data).decode()}
        result = media.decode_artifact(artifact)
        with PILImage.open(io.BytesIO(result)) as decoded:
            self.assertEqual(decoded.format, "PNG")
            self.assertEqual(decoded.size, (3000, 1200))
            self.assertFalse(decoded.getexif())

    def test_animated_input_and_artifact_rejected(self):
        output = io.BytesIO()
        first = PILImage.new("RGB", (8, 8), "red")
        second = PILImage.new("RGB", (8, 8), "blue")
        first.save(output, format="PNG", save_all=True, append_images=[second], duration=100, loop=0)
        data = output.getvalue()
        with self.assertRaises(media.ContextError):
            media._normalize(data)
        with self.assertRaises(media.ContextError):
            media.decode_artifact({"mime": "image/png", "data": base64.b64encode(data).decode()})

    def test_artifact_strict_base64_mime_size_and_pixel_limits(self):
        good = base64.b64encode(image_bytes()).decode()
        invalid = [None, {"mime": "text/html", "data": good}, {"mime": "image/jpeg", "data": good}, {"mime": "image/png", "data": good + "\n"}, {"mime": "image/png", "data": ""}, {"mime": "image/png", "data": "x" * (4*((media.MAX_ARTIFACT_BYTES+2)//3)+1)}, {"mime": "image/png", "data": base64.b64encode(image_bytes((4097, 1))).decode()}]
        for artifact in invalid:
            with self.subTest(mime=artifact.get("mime") if artifact else None), self.assertRaises(media.ContextError):
                media.decode_artifact(artifact)
        with patch.object(media, "MAX_PIXELS", 10):
            with self.assertRaises(media.ContextError):
                media.decode_artifact({"mime": "image/png", "data": good})


if __name__ == "__main__":
    unittest.main()
