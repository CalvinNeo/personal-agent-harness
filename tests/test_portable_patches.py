import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("harness_patch_installer", ROOT / "scripts/apply_patches.py")
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


def digest(content):
    return hashlib.sha256(content).hexdigest()


class PatchStateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "package/source.py"
        self.source.parent.mkdir()
        self.source.write_bytes(b"original\n")
        self.helper = self.root / "package/helper.py"
        self.entries = [
            {"path": "package/source.py", "original_sha256": digest(b"original\n"), "patched_sha256": digest(b"patched\n")},
            {"path": "package/helper.py", "original_sha256": None, "patched_sha256": digest(b"helper\n")},
        ]

    def test_original_state_requires_absent_new_helper(self):
        self.assertEqual(installer.source_state(self.root, self.entries), "original")

    def test_complete_patched_state(self):
        self.source.write_bytes(b"patched\n")
        self.helper.write_bytes(b"helper\n")
        self.assertEqual(installer.source_state(self.root, self.entries), "patched")

    def test_mixed_state_is_rejected(self):
        self.source.write_bytes(b"patched\n")
        with self.assertRaisesRegex(ValueError, "Mixed patch state"):
            installer.source_state(self.root, self.entries)

    def test_unknown_state_is_rejected(self):
        self.source.write_bytes(b"unreviewed\n")
        with self.assertRaisesRegex(ValueError, "unknown source content"):
            installer.source_state(self.root, self.entries)

    def test_unsafe_manifest_paths_are_rejected(self):
        for name in ("../source.py", "/tmp/source.py"):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "Unsafe"):
                installer.source_state(self.root, [{**self.entries[0], "path": name}])

    def test_source_symlink_is_rejected(self):
        self.helper.symlink_to(self.source)
        with self.assertRaisesRegex(ValueError, "symlink"):
            installer.source_state(self.root, self.entries)

    def test_parent_symlink_is_rejected(self):
        (self.root / "linked").symlink_to(self.source.parent, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            installer.source_state(self.root, [{**self.entries[0], "path": "linked/source.py"}])

    def test_empty_manifest_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Mixed patch state"):
            installer.source_state(self.root, [])


if __name__ == "__main__":
    unittest.main()
