"""Privacy checks against synthetic values and disposable Git repositories."""
from __future__ import annotations

from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


SOURCE_ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("privacy_check_under_test", SOURCE_ROOT / "scripts/privacy_check.py")
privacy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(privacy)


class ContentChecks(unittest.TestCase):
    def findings(self, value, terms=()):
        return privacy.inspect_content("fixture.txt", value.encode(), list(terms))

    def test_numeric_ids_require_identity_context(self):
        identifier = "24680" + "13579"
        for value in (
            'sender_id = "' + identifier + '"',
            '"group_openid": "' + identifier + '"',
            '"allowed_senders": ["' + identifier + '"]',
            "qq:GroupMessage:" + identifier,
        ):
            with self.subTest(value=value):
                self.assertTrue(any("numeric account identifier" in item for item in self.findings(value)))
        for value in (
            "received_at = 1794984300", "max_bytes = 16777216",
            'digest = "' + "a" * 64 + '"', 'sender = "synthetic-member"',
            'item_id = "' + identifier + '"', "retry_at = 1794984400",
        ):
            with self.subTest(value=value):
                self.assertEqual(self.findings(value), [])

    def test_ipv6_checks_valid_addresses_and_preserves_documentation_ranges(self):
        private_address = "2001:" + "db9" + ":" * 2 + "12"
        findings = self.findings('host = "[' + private_address + ']"')
        self.assertTrue(any("non-example IPv6 address" in item for item in findings))
        self.assertNotIn(private_address, "\n".join(findings))
        for address in ("::1", "::", "2001:db8::12", "::ffff:192.0.2.1", "12:30:00"):
            with self.subTest(address=address):
                self.assertEqual(self.findings('host = "' + address + '"'), [])

    def test_special_use_ipv6_requires_network_configuration_context(self):
        for prefix in ("fd00", "fec0"):
            address = prefix + ":" * 2 + "123"
            self.assertEqual(self.findings('for denied in ["' + address + '"]:'), [])
            for value in ('host = "' + address + '"', '"endpoint": "https://[' + address + ']/"'):
                with self.subTest(value=value):
                    self.assertTrue(any("non-example IPv6" in item for item in self.findings(value)))

    def test_personal_paths_include_root_windows_and_exact_home_roots(self):
        for value in (
            "/" + "root/personal-project/secrets", "/" + "Users/fixture-person",
            "/" + "home/fixture-person/project", "C:" + "\\Users\\fixture-person\\Documents",
            "D:" + "\\\\Users\\\\fixture-person\\\\Documents",
        ):
            with self.subTest(value=value):
                self.assertTrue(self.findings(value))
        self.assertEqual(self.findings("/root/agent_bot_workspace/plugins"), [])
        self.assertEqual(self.findings("/var/lib/codexrunner/workspaces"), [])

    def test_two_character_names_and_private_filenames_are_redacted(self):
        name = "合" + "成"
        findings = privacy.inspect_content(name + ".txt", ("author " + name).encode(), [name])
        findings += privacy.inspect_path(name, "docs/" + name + ".md", [name])
        self.assertTrue(findings)
        self.assertNotIn(name, "\n".join(findings))

    def test_public_noreply_identity_remains_allowed(self):
        self.assertEqual(self.findings("author Fixture <123+fixture@users.noreply.github.com>"), [])
        address = "fixture@" + "private.invalid"
        self.assertTrue(any("non-example email" in item for item in self.findings(address)))

    def test_browser_versions_and_url_userinfo_are_not_mistaken_for_addresses(self):
        version = "131." + "0.0.0"
        self.assertEqual(self.findings("Chrome/" + version), [])
        self.assertTrue(self.findings("host = " + version))
        authority = "https://" + "user@" + "www.xiaohongshu.com/explore/fixture"
        self.assertEqual(self.findings(authority), [])
        address = "fixture@" + "private.invalid"
        self.assertTrue(any("non-example email" in item for item in self.findings("https://example.com/?mail=" + address)))

    def test_runtime_templates_do_not_escape_blocked_directories(self):
        for name in (
            "cookies.json", "session.json", "login-state.json", "storage_state.json",
            "watches.json", "codex-jobs.json", "request-context.json",
            "state/export.json.example", "sessions/fixture.txt.example",
            "nested/request-contexts/turn.json", "browser-profile/Preferences",
            "browser-cookies.json", "task-session.json",
        ):
            with self.subTest(name=name):
                self.assertTrue(privacy.inspect_path(name, name))
        for name in ("deploy/examples/model.env.example", "deploy/examples/xianyu_watch_config.json.example",
                     "plugins/xianyu_watch/main.py", "plugins/codex_bridge/test_jobs.py"):
            with self.subTest(name=name):
                self.assertEqual(privacy.inspect_path(name, name), [])


class RepositoryChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="privacy-check-tests-")
        self.root = Path(self.temp.name) / "repo"
        self.root.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Fixture Author")
        self.git("config", "user.email", "fixture@example.com")
        self.root_patch = patch.object(privacy, "ROOT", self.root)
        self.root_patch.start()

    def tearDown(self):
        self.root_patch.stop()
        self.temp.cleanup()

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.root), *args], stderr=subprocess.DEVNULL)

    def write(self, name, data="safe source\n"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data)

    def commit(self, message="fixture commit"):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)

    def run_checker(self, *args):
        output = io.StringIO()
        with patch("sys.argv", ["privacy_check.py", *args]), redirect_stdout(output):
            status = privacy.main()
        return status, output.getvalue()

    def test_external_denylist_accepts_short_names_and_never_echoes_values(self):
        marker = "合" + "成"
        self.write("source.py", "# " + marker + "\n")
        terms = Path(self.temp.name) / "terms.json"
        terms.write_text(json.dumps([marker]))
        status, output = self.run_checker("--private-terms-file", str(terms))
        self.assertEqual(status, 1)
        self.assertIn("source.py:1: private denylist match", output)
        self.assertNotIn(marker, output)
        for values in ([""], [" "], [None], "fixture"):
            terms.write_text(json.dumps(values))
            with self.assertRaises(ValueError):
                privacy.private_terms(terms)

    def test_denylist_cannot_be_checked_in(self):
        terms = self.root / "terms.json"
        terms.write_text('["fixture"]')
        with self.assertRaises(ValueError):
            privacy.private_terms(terms)

    def test_history_checks_alias_paths_for_identical_blobs(self):
        self.write("placeholder.txt", "identical safe bytes\n")
        self.commit()
        self.write("cookies.json", "identical safe bytes\n")
        self.write("state/export.json.example", "identical safe bytes\n")
        self.commit("reuse same blob under runtime paths")
        self.git("rm", "-r", "cookies.json", "state")
        self.commit("remove runtime paths")
        findings, count = privacy.inspect_history([])
        self.assertGreater(count, 0)
        for name in ("cookies.json", "state/export.json.example"):
            self.assertTrue(any("history:" + name + ": runtime/credential" in item for item in findings), findings)

    def test_history_inspects_commit_and_nested_tag_metadata(self):
        marker = "synthetic-private-" + "marker"
        self.write("source.py")
        self.commit(marker)
        self.git("tag", "-a", "first", "-m", marker)
        self.git("tag", "-a", "second", "first", "-m", marker)
        findings, _ = privacy.inspect_history([marker])
        self.assertTrue(any(item.startswith("commit:") and "denylist" in item for item in findings))
        self.assertEqual(sum(item.startswith("tag:") and "denylist" in item for item in findings), 2, findings)
        self.assertNotIn(marker, "\n".join(findings))

    def test_tags_pointing_to_standalone_blobs_and_trees_are_inspected(self):
        marker = "synthetic-private-" + "tagged-payload"
        self.write("payload.txt", marker)
        oid = self.git("hash-object", "-w", "payload.txt").decode().strip()
        self.git("tag", "-a", "payload", oid, "-m", "fixture")
        self.git("update-index", "--add", "--cacheinfo", "100644," + oid + ",cookies.json")
        tree = self.git("write-tree").decode().strip()
        self.git("tag", "tree", tree)
        findings, _ = privacy.inspect_history([marker])
        self.assertTrue(any("denylist match" in item for item in findings), findings)
        self.assertTrue(any("history:cookies.json: runtime/credential" in item for item in findings), findings)

    def test_staged_symlink_is_detected_when_worktree_has_a_regular_file(self):
        self.write("target.txt")
        os.symlink("target.txt", self.root / "link")
        self.git("add", "target.txt", "link")
        (self.root / "link").unlink()
        self.write("link", "regular worktree replacement")
        status, output = self.run_checker("--staged")
        self.assertEqual(status, 1)
        self.assertIn("index:link: symlinks require manual review", output)

    def test_gitignore_blocks_runtime_state_but_keeps_examples(self):
        self.write(".gitignore", (SOURCE_ROOT / ".gitignore").read_text())
        names = ["cookies.json", "sessions.json", "login-state.json", "watches.json", "codex-jobs.json",
                 "request-contexts/turn.json", "browser-profile/Preferences"]
        for name in names:
            self.write(name)
            self.assertEqual(self.git("check-ignore", name).decode().strip(), name)
        example = "deploy/examples/xianyu_watch_config.json.example"
        self.write(example)
        result = subprocess.run(["git", "-C", str(self.root), "check-ignore", example], capture_output=True)
        self.assertEqual(result.returncode, 1)


if __name__ == "__main__":
    unittest.main()
