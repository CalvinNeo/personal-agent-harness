import importlib.util
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


runner = load("harness_runner", "services/codex-runner.py")
proxy = load("harness_proxy", "services/codex-api-proxy.py")
initializer = load("harness_initializer", "scripts/initialize_private.py")
privacy = load("harness_privacy", "scripts/privacy_check.py")


class PortabilityTests(unittest.TestCase):
    def test_runner_workspace_boundary_and_uid_resolution(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(runner, "ROOT", Path(folder)):
            name = "a" * 32
            self.assertEqual(runner.safe_workspace(name).parent, Path(folder).resolve())
            for bad in ("../escape", "", "a" * 31, "/tmp/escape", "z" * 32):
                with self.assertRaises(ValueError):
                    runner.safe_workspace(bad)
        with patch.object(runner.pwd, "getpwnam", return_value=SimpleNamespace(pw_uid=4242)):
            self.assertEqual(runner.allowed_uids(), {0, 4242})

    def test_runner_redacts_secret_before_return(self):
        secret = "s" + "k-" + "synthetic" * 6
        self.assertNotIn(secret, runner.clean_output("result " + secret))
        value = "MODEL_API_KEY=" + "synthetic" * 5
        self.assertNotIn(value, runner.clean_output(value))

    def test_disabled_config_and_environment_reference(self):
        config = initializer.build_config("test only " * 4, "https://api.example.com/v1", "example-model", True)
        self.assertFalse(config["platform"][0]["enable"])
        self.assertFalse(config["provider_settings"]["enable"])
        self.assertEqual(config["provider_sources"][0]["key"], ["$MODEL_API_KEY"])
        self.assertEqual(config["platform_settings"]["id_whitelist"], ["__deny_all_until_configured__"])
        self.assertFalse(config["platform_settings"]["wl_ignore_admin_on_group"])
        self.assertTrue(config["provider_settings"]["web_search"])
        self.assertNotEqual(config["dashboard"]["password"], "test only " * 4)

    def test_proxy_fixed_upstream_and_private_token(self):
        handler = object.__new__(proxy.Handler)
        handler.path = "/v1/responses"
        handler.command = "POST"
        handler.headers = {"Content-Length": "2", "Content-Type": "application/json"}
        handler.rfile, handler.wfile = io.BytesIO(b"{}"), io.BytesIO()
        handler.send_error = lambda *args: self.fail("unexpected error")
        handler.send_response = lambda status: self.assertEqual(status, 200)
        handler.send_header = lambda *args: None
        handler.end_headers = lambda: None
        class Response:
            status, headers = 200, {"Content-Type": "application/json"}
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return b'{"ok":true}'
        with tempfile.TemporaryDirectory() as folder:
            token = Path(folder) / "upstream-token"
            token.write_text("synthetic-token-for-unit-test")
            with patch.object(proxy, "TOKEN_FILE", str(token)), patch.object(proxy, "UPSTREAM", "https://api.example.com"), patch.object(proxy, "urlopen", return_value=Response()) as fetch:
                handler.forward()
                request = fetch.call_args.args[0]
                self.assertEqual(request.full_url, "https://api.example.com/v1/responses")
                self.assertEqual(request.headers["Authorization"], "Bearer synthetic-token-for-unit-test")
                self.assertNotIn(b"synthetic-token", handler.wfile.getvalue())
                errors = []
                handler.send_error = lambda code, *args: errors.append(code)
                handler.path = "/other"
                handler.forward()
                handler.path = "/v1/responses"
                handler.headers["Content-Length"] = "-1"
                handler.forward()
                self.assertEqual(errors, [404, 413])
                self.assertEqual(fetch.call_count, 1)

    def test_privacy_reports_location_without_secret(self):
        secret = "s" + "k-" + "synthetic" * 6
        findings = privacy.inspect_content("fixture.txt", secret.encode(), [])
        self.assertTrue(findings)
        self.assertNotIn(secret, " ".join(findings))
        private = "private-" + "group-marker"
        self.assertTrue(privacy.inspect_content("fixture.txt", private.encode(), [private]))
        self.assertTrue(privacy.inspect_path("fixture", "data/conversation.db"))
        self.assertFalse(privacy.inspect_path("fixture", "deploy/examples/model.env.example"))


if __name__ == "__main__":
    unittest.main()
