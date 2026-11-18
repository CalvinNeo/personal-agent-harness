import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("conversations", Path(__file__).with_name("conversations.py"))
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)
MiB = 1024 * 1024

def sparse(path, size):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as stream:
        stream.truncate(size)

class StorageBudgets(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
    def tearDown(self):
        self.tmp.cleanup()
    def test_compliant_media_and_home_no_longer_share_64mib(self):
        sparse(self.root / "home" / "session.jsonl", 60 * MiB)
        sparse(self.root / "workspace" / ".tmp" / "video.mp4", 128 * MiB)
        sparse(self.root / "workspace" / ".tmp" / "frames" / "frame.png", 32 * MiB)
        self.assertEqual(c.conversation_size(self.root), 220 * MiB)
        with self.assertRaisesRegex(ValueError, "storage limit exceeded"):
            c.disk_size(self.root)
    def test_retained_budget_cannot_borrow_scratch_budget(self):
        sparse(self.root / "home" / "session.jsonl", 64 * MiB + 1)
        with self.assertRaisesRegex(ValueError, "retained storage"):
            c.conversation_size(self.root)
    def test_scratch_budget_is_bounded_at_boundary(self):
        video = self.root / "workspace" / ".tmp" / "video.mp4"
        sparse(video, 256 * MiB)
        self.assertEqual(c.conversation_size(self.root), 256 * MiB)
        sparse(video, 256 * MiB + 1)
        with self.assertRaisesRegex(ValueError, "scratch storage"):
            c.conversation_size(self.root)
    def test_only_exact_workspace_tmp_receives_scratch_budget(self):
        sparse(self.root / "home" / ".tmp" / "video.mp4", 65 * MiB)
        with self.assertRaisesRegex(ValueError, "retained storage"):
            c.conversation_size(self.root)
    def test_get_and_prune_accept_same_legal_media_budget(self):
        manager = c.Conversations(self.root / "records", self.root / "roots", None)
        item = manager.create("a" * 16, "b" * 64)
        sparse(manager.root(item) / "workspace" / ".tmp" / "video.mp4", 128 * MiB)
        self.assertIs(manager.get(item["id"], item["owner"]), item)
        manager.prune(set())
        self.assertIsNone(item["unavailable"])
        sparse(manager.root(item) / "home" / "session.jsonl", 65 * MiB)
        manager.prune(set())
        self.assertIsNotNone(item["unavailable"])
    def test_special_files_and_file_count_still_validated(self):
        import os
        fifo = self.root / "fifo"
        os.mkfifo(fifo)
        with self.assertRaisesRegex(ValueError, "unsupported file"):
            c.conversation_size(self.root)
        fifo.unlink()
        for i in range(3):
            (self.root / str(i)).touch()
        with patch.object(c, "MAX_FILES", 2):
            with self.assertRaisesRegex(ValueError, "storage limit"):
                c.conversation_size(self.root)

if __name__ == "__main__":
    unittest.main()
