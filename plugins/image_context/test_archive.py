"""Actual SQLite/filesystem archive tests; synthetic images and no network."""
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image as PILImage

from archive import (
    ImageArchive, ImageArchiveError, MAX_IMAGE_BYTES, MAX_MODEL_IMAGE_BYTES,
    normalize_for_model, read_temp_image,
)


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Zl1sAAAAASUVORK5CYII="
)
UMO = "qq:GroupMessage:family"
SENDER = "requester-a"


def image(index=0):
    # Valid PNGs tolerate trailing metadata. Distinct inputs exercise content
    # addressing without importing the provider, downloading, or using Pillow.
    return PNG + str(index).encode("ascii")


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name).resolve()
        self.root = self.folder / "archive"
        self.archive = ImageArchive(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def ingest(self, message_id="upload-1", data=PNG, *, now=100, **kwargs):
        return self.archive.ingest(UMO, SENDER, message_id,
                                   [(data, "https://multimedia.nt.qq.com/download?token=private")], now, **kwargs)

    def test_persists_original_bytes_and_metadata_across_restart_without_ttl(self):
        refs = self.ingest()
        self.assertEqual(len(refs), 1)
        expected = self.root / "files" / hashlib.sha256(PNG).hexdigest()
        self.assertEqual(refs, [str(expected)])
        reopened = ImageArchive(self.root)
        rows = reopened.list_images(UMO, SENDER)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ref"], str(expected))
        self.assertEqual(rows[0]["received_wall_at"], 100)
        self.assertEqual(rows[0]["message_id"], "upload-1")
        self.assertEqual(rows[0]["batch_id"], "upload-1")
        self.assertEqual(rows[0]["source"], "upload")
        self.assertNotIn("token=private", repr(rows))
        self.assertEqual(reopened.read(UMO, SENDER, str(expected)), PNG)
        # A query years later still sees the original input. There is no TTL.
        self.assertEqual(reopened.list_images(UMO, SENDER, before=10**12), rows)
        self.assertEqual(reopened.list_images(UMO, SENDER)[0]["received_wall_at"], 100)

    def test_scope_is_exact_for_group_sender_and_private_conversation(self):
        ref = self.ingest()[0]
        for umo, sender in ((UMO, "requester-b"), ("qq:GroupMessage:other", SENDER),
                            ("qq:FriendMessage:requester-a", SENDER)):
            with self.subTest(umo=umo, sender=sender):
                self.assertEqual(self.archive.list_images(umo, sender), [])
                with self.assertRaises(ImageArchiveError) as error:
                    self.archive.read(umo, sender, ref)
                self.assertEqual(error.exception.code, "not_found")
        # Sharing bytes saves storage, never grants another scope access until
        # that requester explicitly supplies its own input event.
        other_refs = self.archive.ingest(UMO, "requester-b", "b-input", [(PNG, "quoted")], 200,
                                        source="reply", reply_id="original-from-a")
        self.assertEqual(other_refs, [ref])
        self.assertEqual(self.archive.read(UMO, "requester-b", ref), PNG)
        self.assertEqual(self.archive.list_images(UMO, "requester-b")[0]["source"], "reply")
        self.assertEqual(self.archive.list_images(UMO, SENDER)[0]["received_wall_at"], 100)

    def test_event_replay_is_idempotent_and_keeps_first_receipt_and_source(self):
        refs = self.ingest()
        duplicate = self.archive.ingest(UMO, SENDER, "upload-1", [(PNG, "different-local-reference")],
                                        200_000, source="reply", reply_id="later-quote")
        self.assertEqual(duplicate, refs)
        rows = self.archive.list_images(UMO, SENDER)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["received_wall_at"], 100)
        self.assertEqual(rows[0]["source"], "upload")
        self.assertEqual(rows[0]["reply_id"], "")
        self.assertEqual(rows[0]["original_ref_sha256"], hashlib.sha256(
            b"https://multimedia.nt.qq.com/download?token=private").hexdigest())

    def test_explicit_quote_has_its_own_receipt_and_batch_without_refreshing_original(self):
        refs = self.ingest()
        quoted = self.archive.ingest(UMO, SENDER, "quote-input", [(PNG, "original")],
                                    1_000_000, source="reply", reply_id="upload-1")
        self.assertEqual(quoted, refs)
        rows = self.archive.list_images(UMO, SENDER)
        self.assertEqual([row["received_wall_at"] for row in rows], [100, 1_000_000])
        self.assertEqual([row["batch_id"] for row in rows], ["upload-1", "quote-input"])
        self.assertEqual(rows[1]["reply_id"], "upload-1")
        self.assertEqual(self.archive.list_images(UMO, SENDER, message_id="upload-1"), rows[:1])
        self.assertEqual(self.archive.list_images(UMO, SENDER, message_id="quote-input"), rows[1:])

    def test_reply_id_restores_only_its_exact_recorded_source_in_same_scope(self):
        original = self.ingest("original", image(0), now=100)[0]
        quoted = self.archive.ingest(UMO, SENDER, "quote-request", [(image(1), "quoted")],
                                    200, source="reply", reply_id="source-qq-message")
        self.ingest("unrelated-latest", image(2), now=300)
        rows = self.archive.list_images(UMO, SENDER, reply_id="source-qq-message")
        self.assertEqual([row["ref"] for row in rows], quoted)
        self.assertEqual(rows[0]["message_id"], "quote-request")
        self.assertEqual(rows[0]["source"], "reply")
        self.assertNotIn(original, [row["ref"] for row in rows])
        self.assertEqual(self.archive.list_images(UMO, SENDER, reply_id="source-qq"), [])
        self.assertEqual(self.archive.list_images(UMO, SENDER, message_id="original",
                                                  reply_id="source-qq-message"), [])
        reopened = ImageArchive(self.root)
        self.assertEqual(reopened.list_images(UMO, SENDER, reply_id="source-qq-message"), rows)
        for umo, sender in ((UMO, "other-sender"), ("qq:GroupMessage:other", SENDER),
                            ("qq:FriendMessage:requester-a", SENDER)):
            self.assertEqual(reopened.list_images(umo, sender, reply_id="source-qq-message"), [])

    def test_reply_id_query_validates_identifiers_without_exposing_input(self):
        for value in ("", "bad\nreply", "x" * 513, ["reply-id"]):
            with self.subTest(value=value), self.assertRaises(ImageArchiveError) as error:
                self.archive.list_images(UMO, SENDER, reply_id=value)
            self.assertEqual(error.exception.code, "invalid_query")

    def test_all_history_survives_more_than_twelve_images_and_pages_are_ordered(self):
        refs = [self.ingest(f"input-{index}", image(index), now=100 + index)[0] for index in range(30)]
        self.assertEqual([row["ref"] for row in self.archive.list_images(UMO, SENDER)], refs[-12:])
        self.assertEqual([row["ref"] for row in self.archive.list_images(UMO, SENDER, limit=2)], refs[-2:])
        self.assertEqual([row["ref"] for row in self.archive.list_images(UMO, SENDER, before=118)], refs[6:18])
        reopened = ImageArchive(self.root)
        for index in range(30):
            rows = reopened.list_images(UMO, SENDER, message_id=f"input-{index}")
            self.assertEqual(len(rows), 1)
            self.assertEqual(reopened.read(UMO, SENDER, rows[0]["ref"]), image(index))
        self.assertEqual(len(list((self.root / "files").iterdir())), 30)
        with sqlite3.connect(self.root / "index.sqlite3") as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM images").fetchone()[0], 30)

    def test_one_large_message_keeps_all_images_and_duplicate_bytes_are_deduplicated(self):
        inputs = [(image(index), f"input-{index}") for index in range(20)]
        refs = self.archive.ingest(UMO, SENDER, "large-message", inputs + inputs[:1], 100)
        self.assertEqual(len(refs), 20)
        rows = self.archive.list_images(UMO, SENDER, message_id="large-message")
        self.assertEqual([row["ref"] for row in rows], refs[-12:])
        self.assertEqual([row["position"] for row in rows], list(range(8, 20)))
        self.assertEqual(self.archive.read(UMO, SENDER, refs[0]), image(0))

    def test_cursor_pages_all_images_with_identical_timestamps_without_skips_or_duplicates(self):
        inputs = [(image(index), f"input-{index}") for index in range(31)]
        refs = self.archive.ingest(UMO, SENDER, "one-timestamp", inputs, 100)
        cursor, seen, pages = None, [], []
        while True:
            rows = self.archive.list_images(UMO, SENDER, cursor=cursor)
            if not rows:
                break
            pages.append(len(rows))
            page_refs = [row["ref"] for row in rows]
            self.assertFalse(set(page_refs) & set(seen))
            seen = page_refs + seen
            cursor = rows[0]["cursor"]
        self.assertEqual(pages, [12, 12, 7])
        self.assertEqual(seen, refs)
        # A cursor is stable across connections/restarts and uses the row ID
        # tie-breaker, rather than dropping all entries with the same time.
        first = self.archive.list_images(UMO, SENDER)
        reopened = ImageArchive(self.root)
        second = reopened.list_images(UMO, SENDER, cursor=first[0]["cursor"])
        self.assertEqual([row["ref"] for row in second], refs[7:19])

    def test_cursor_and_legacy_before_filters_compose_in_timestamp_order(self):
        expected = []
        for index in range(35):
            timestamp = 100 + index // 7
            expected.append(self.ingest(f"input-{index}", image(index), now=timestamp)[0])
        cursor, seen = None, []
        while True:
            rows = self.archive.list_images(UMO, SENDER, limit=5, before=104, cursor=cursor)
            if not rows:
                break
            seen = [row["ref"] for row in rows] + seen
            cursor = rows[0]["cursor"]
        self.assertEqual(seen, expected[:28])

    def test_cursor_input_is_strict_and_never_exposes_sensitive_values(self):
        for cursor in ("0:100.0", "01:100.0", "-1:100.0", "1:nan", "1:inf", "1:-1.0",
                       "1:1e999", "9223372036854775808:100.0", "1:100.0\n", "1:100.0 OR 1=1",
                       "https://private.invalid/?secret=token", ["1:100.0"]):
            with self.subTest(cursor=cursor), self.assertRaises(ImageArchiveError) as error:
                self.archive.list_images(UMO, SENDER, cursor=cursor)
            self.assertEqual(error.exception.code, "invalid_query")
            self.assertEqual(str(error.exception), "图片归档查询参数无效。")

    def test_permissions_are_private_and_repaired_on_reopen(self):
        ref = Path(self.ingest()[0])
        for path in (self.root, self.root / "files"):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
            path.chmod(0o755)
        for path in (self.root / "index.sqlite3", ref):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            path.chmod(0o644)
        reopened = ImageArchive(self.root)
        reopened.read(UMO, SENDER, str(ref))
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.root / "files").stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.root / "index.sqlite3").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(ref.stat().st_mode), 0o600)

    def test_symlink_root_or_ancestor_is_rejected(self):
        alias = self.folder / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        for path in (alias, alias / "nested"):
            with self.subTest(path=path), self.assertRaises(ImageArchiveError) as error:
                ImageArchive(path)
            self.assertEqual(error.exception.code, "unsafe_storage")

    def test_symlink_files_directory_or_sqlite_index_is_rejected(self):
        for component in ("files", "index.sqlite3"):
            with self.subTest(component=component):
                root = self.folder / component.replace(".", "-")
                root.mkdir()
                target = self.folder / (component + "-target")
                if component == "files":
                    target.mkdir()
                else:
                    target.write_bytes(b"sensitive outside content")
                (root / component).symlink_to(target, target_is_directory=component == "files")
                with self.assertRaises(ImageArchiveError) as error:
                    ImageArchive(root)
                self.assertEqual(error.exception.code, "unsafe_storage")
                if component != "files":
                    self.assertEqual(target.read_bytes(), b"sensitive outside content")

    def test_symlink_blob_is_never_followed_by_read_or_reingest(self):
        ref = Path(self.ingest()[0])
        outside = self.folder / "outside.png"
        outside.write_bytes(PNG)
        ref.unlink()
        ref.symlink_to(outside)
        for action in (lambda: self.archive.read(UMO, SENDER, str(ref)), lambda: self.ingest()):
            with self.assertRaises(ImageArchiveError) as error:
                action()
            self.assertEqual(error.exception.code, "unsafe_storage")
            self.assertNotIn(str(outside), str(error.exception))
        self.assertEqual(outside.read_bytes(), PNG)

    def test_symlink_sqlite_sidecars_are_rejected_without_touching_target(self):
        outside = self.folder / "outside-secret"
        outside.write_bytes(b"never change")
        for suffix in ("-journal", "-wal", "-shm"):
            sidecar = self.root / ("index.sqlite3" + suffix)
            sidecar.symlink_to(outside)
            with self.subTest(suffix=suffix), self.assertRaises(ImageArchiveError) as error:
                self.archive.list_images(UMO, SENDER)
            self.assertEqual(error.exception.code, "unsafe_storage")
            self.assertEqual(outside.read_bytes(), b"never change")
            sidecar.unlink()

    def test_unindexed_or_modified_files_and_paths_outside_archive_are_rejected(self):
        ref = Path(self.ingest()[0])
        unindexed = self.root / "files" / hashlib.sha256(image(9)).hexdigest()
        unindexed.write_bytes(image(9))
        for invalid in (str(unindexed), str(self.folder / "outside.png"),
                        str(self.root / "files" / ".." / "index.sqlite3"), ref.as_uri()):
            with self.subTest(invalid=invalid), self.assertRaises(ImageArchiveError) as error:
                self.archive.read(UMO, SENDER, invalid)
            self.assertEqual(error.exception.code, "not_found")
        ref.write_bytes(image(2))
        with self.assertRaises(ImageArchiveError) as error:
            self.archive.read(UMO, SENDER, str(ref))
        self.assertEqual(error.exception.code, "unsafe_storage")

    def test_invalid_batch_is_rejected_before_writing_any_blob_or_row(self):
        for bad in (b"", b"plain text", b"\x89PNG\r\n\x1a\n" + b"x" * MAX_IMAGE_BYTES):
            with self.subTest(size=len(bad)), self.assertRaises(ImageArchiveError) as error:
                self.archive.ingest(UMO, SENDER, "bad", [(PNG, "valid"), (bad, "invalid")], 1)
            self.assertEqual(error.exception.code, "invalid_image")
        self.assertEqual(self.archive.list_images(UMO, SENDER), [])
        self.assertEqual(list((self.root / "files").iterdir()), [])

    def test_invalid_scope_and_query_are_safe(self):
        for kwargs in ({"umo": ""}, {"sender": "bad\nsender"}, {"message_id": ""},
                       {"received_wall_at": float("nan")}, {"received_wall_at": True}):
            options = dict(umo=UMO, sender=SENDER, message_id="input", images=[(PNG, "original")],
                           received_wall_at=100)
            options.update(kwargs)
            with self.subTest(kwargs=kwargs), self.assertRaises(ImageArchiveError):
                self.archive.ingest(**options)
        self.assertEqual(self.archive.list_images(UMO, SENDER, limit=13), [])
        for kwargs in ({"limit": 101}, {"limit": True}, {"before": float("inf")}, {"message_id": ""}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ImageArchiveError) as error:
                self.archive.list_images(UMO, SENDER, **kwargs)
            self.assertEqual(error.exception.code, "invalid_query")
        for root in (Path("relative"), Path("/"), self.root / ".." / "another"):
            with self.subTest(root=root), self.assertRaises(ImageArchiveError):
                ImageArchive(root)

    def test_concurrent_replay_keeps_one_index_row_and_one_blob(self):
        def ingest(index):
            instance = ImageArchive(self.root)
            return instance.ingest(UMO, SENDER, "one-message", [(PNG, "original")], 100 + index)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(ingest, range(8)))
        self.assertTrue(all(result == results[0] for result in results))
        self.assertEqual(len(self.archive.list_images(UMO, SENDER)), 1)
        self.assertEqual(len(list((self.root / "files").iterdir())), 1)


class ImageHelperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name).resolve()
        self.root = self.folder / "framework-temp"
        self.root.mkdir()
        self.image = self.root / "original.png"
        self.image.write_bytes(PNG)

    def tearDown(self):
        self.temp.cleanup()

    def test_temp_reader_accepts_absolute_and_file_uri_without_modifying_original(self):
        self.image.chmod(0o644)
        self.assertEqual(read_temp_image(str(self.image), self.root), PNG)
        self.assertEqual(read_temp_image(self.image.as_uri(), self.root), PNG)
        self.assertEqual(stat.S_IMODE(self.image.stat().st_mode), 0o644)

    def test_temp_reader_rejects_escape_symlinks_and_invalid_file_uris(self):
        outside = self.folder / "outside.png"
        outside.write_bytes(PNG)
        alias = self.root / "alias.png"
        alias.symlink_to(self.image)
        directory_alias = self.root / "linked"
        directory_alias.symlink_to(self.root, target_is_directory=True)
        root_alias = self.folder / "root-alias"
        root_alias.symlink_to(self.root, target_is_directory=True)
        for candidate, root in ((str(outside), self.root), (str(alias), self.root),
                                (str(directory_alias / "original.png"), self.root),
                                (str(root_alias / "original.png"), root_alias),
                                (str(self.root / ".." / "outside.png"), self.root),
                                ("relative.png", self.root),
                                ("file://remote" + str(self.image), self.root),
                                (self.image.as_uri() + "?token=secret", self.root),
                                (self.image.as_uri() + "#fragment", self.root)):
            with self.subTest(candidate=candidate), self.assertRaises(ImageArchiveError) as error:
                read_temp_image(candidate, root)
            self.assertEqual(error.exception.code, "unsafe_storage")
            self.assertNotIn("secret", str(error.exception))
            self.assertNotIn(str(self.folder), str(error.exception))

    def test_temp_reader_rejects_special_empty_large_and_nonimage_files(self):
        fifo = self.root / "image-fifo"
        os.mkfifo(fifo)
        large = self.root / "large.png"
        with large.open("wb") as stream:
            stream.truncate(MAX_IMAGE_BYTES + 1)
        empty = self.root / "empty.png"
        empty.write_bytes(b"")
        text = self.root / "text.png"
        text.write_bytes(b"untrusted text")
        for candidate in (fifo, large, empty, text, self.root):
            with self.subTest(candidate=candidate), self.assertRaises(ImageArchiveError):
                read_temp_image(str(candidate), self.root)

    def test_model_view_bounds_dimensions_strips_metadata_and_keeps_source_unchanged(self):
        source = PILImage.new("RGB", (3200, 1800), "orange")
        exif = PILImage.Exif()
        exif[0x010E] = "private source metadata"
        buffer = io.BytesIO()
        source.save(buffer, format="JPEG", exif=exif)
        original = buffer.getvalue()
        digest = hashlib.sha256(original).hexdigest()
        normalized = normalize_for_model(original)
        self.assertLessEqual(len(normalized), MAX_MODEL_IMAGE_BYTES)
        self.assertEqual(hashlib.sha256(original).hexdigest(), digest)
        with PILImage.open(io.BytesIO(normalized)) as result:
            self.assertEqual(result.format, "JPEG")
            self.assertEqual(result.size, (1600, 900))
            self.assertEqual(dict(result.getexif()), {})
            self.assertNotIn(b"private source metadata", normalized)

    def test_model_view_orients_exif_and_flattens_transparency(self):
        source = PILImage.new("RGB", (4, 3), "blue")
        exif = PILImage.Exif()
        exif[0x0112] = 6
        buffer = io.BytesIO()
        source.save(buffer, format="JPEG", exif=exif)
        with PILImage.open(io.BytesIO(normalize_for_model(buffer.getvalue()))) as result:
            self.assertEqual(result.size, (3, 4))
        buffer = io.BytesIO()
        PILImage.new("RGBA", (8, 8), (255, 0, 0, 0)).save(buffer, format="PNG")
        with PILImage.open(io.BytesIO(normalize_for_model(buffer.getvalue()))) as result:
            self.assertEqual(result.mode, "RGB")
            self.assertTrue(all(value >= 250 for value in result.getpixel((0, 0))))

    def test_model_view_rejects_invalid_animated_oversized_and_excess_pixel_inputs(self):
        for data in (b"plain text", b"\x89PNG\r\n\x1a\ninvalid", PNG + b"x" * MAX_IMAGE_BYTES):
            with self.subTest(size=len(data)), self.assertRaises(ImageArchiveError):
                normalize_for_model(data)
        buffer = io.BytesIO()
        first = PILImage.new("RGB", (4, 4), "orange")
        first.save(buffer, format="GIF", save_all=True,
                   append_images=[PILImage.new("RGB", (4, 4), "green")])
        with self.assertRaises(ImageArchiveError):
            normalize_for_model(buffer.getvalue())
        buffer = io.BytesIO()
        first.save(buffer, format="PNG")
        with patch("archive.MAX_PIXELS", 10), self.assertRaises(ImageArchiveError):
            normalize_for_model(buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
