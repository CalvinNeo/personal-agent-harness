"""Exercise the patched failure branch with synthetic image data only."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("qq_image_log_patch", ROOT / "scripts/redact_qq_image_upload_log.py")
patcher = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(patcher)

FIXTURE = '''from __future__ import annotations

class QQOfficialMessageEvent:
    async def upload_group_and_c2c_image(self, image_base64, file_type, **kwargs):
        payload = {"file_data": image_base64, "file_type": file_type, "srv_send_msg": False}
        async def _do_upload():
            if "openid" in kwargs:
                payload["openid"] = kwargs["openid"]
                route = ("POST", "/v2/users/{openid}/files")
            elif "group_openid" in kwargs:
                payload["group_openid"] = kwargs["group_openid"]
                route = ("POST", "/v2/groups/{group_openid}/files")
            else:
                raise ValueError("Invalid upload parameters")
            result = await self.bot.api._http.request(route, json=payload)
            if result is None:
                raise APIReturnNoneError("upload returned no response")
            return result
        try:
            result = await _do_upload()
        except APIReturnNoneError:
            logger.warning(f"上传图片API返回None，共尝试5次后放弃: {payload}")
            raise
        return result

    def unrelated(self):
        return "unchanged behavior"
'''


class APIReturnNoneError(RuntimeError):
    pass


class PatchTests(unittest.IsolatedAsyncioTestCase):
    async def run_fixture(self, source, response):
        logger = Mock()
        namespace = {"logger": logger, "APIReturnNoneError": APIReturnNoneError}
        exec(compile(source, "synthetic_fixture.py", "exec"), namespace)
        instance = namespace["QQOfficialMessageEvent"]()
        request = AsyncMock(return_value=response)
        instance.bot = SimpleNamespace(api=SimpleNamespace(_http=SimpleNamespace(request=request)))
        if response is None:
            with self.assertRaises(APIReturnNoneError):
                await instance.upload_group_and_c2c_image("synthetic-qr-bytes", 1, openid="synthetic-private-identity")
        else:
            self.assertIs(await instance.upload_group_and_c2c_image("synthetic-qr-bytes", 1, openid="synthetic-private-identity"), response)
        return logger, request, instance

    async def test_failure_logs_no_qr_or_identity_and_preserves_exception(self):
        patched, changed = patcher.patched_source(FIXTURE)
        self.assertTrue(changed)
        before, old_request, _ = await self.run_fixture(FIXTURE, None)
        after, new_request, _ = await self.run_fixture(patched, None)
        self.assertIn("synthetic-qr-bytes", str(before.warning.call_args))
        self.assertNotIn("synthetic-qr-bytes", str(after.warning.call_args))
        self.assertNotIn("synthetic-private-identity", str(after.warning.call_args))
        after.warning.assert_called_once()
        self.assertEqual(old_request.call_args, new_request.call_args)

    async def test_success_path_and_other_source_are_unchanged(self):
        patched, _ = patcher.patched_source(FIXTURE)
        self.assertEqual(patched.replace(patcher.NEW, patcher.OLD, 1), FIXTURE)
        response = {"file_info": "synthetic-media-result"}
        logger, _, instance = await self.run_fixture(patched, response)
        logger.warning.assert_not_called()
        self.assertEqual(instance.unrelated(), "unchanged behavior")

    def test_check_is_read_only_and_apply_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / patcher.TARGET
            source.parent.mkdir(parents=True)
            source.write_text(FIXTURE)
            source.chmod(0o640)
            self.assertEqual(patcher.apply_patch(root, check=True), "applicable")
            self.assertEqual(source.read_text(), FIXTURE)
            self.assertEqual(patcher.apply_patch(root), "patched")
            self.assertEqual(source.stat().st_mode & 0o777, 0o640)
            patched = source.read_bytes()
            self.assertEqual(patcher.apply_patch(root), "already-patched")
            self.assertEqual(source.read_bytes(), patched)

    def test_unknown_mixed_duplicate_and_wrong_method_states_are_rejected(self):
        for source in (
            FIXTURE.replace(patcher.OLD, 'logger.warning("different upstream version")'),
            FIXTURE.replace(patcher.OLD, patcher.OLD + "\n            " + patcher.OLD),
            FIXTURE.replace(patcher.OLD, patcher.OLD + "\n            " + patcher.NEW),
            FIXTURE.replace("upload_group_and_c2c_image", "unrelated_upload"),
        ):
            with self.subTest(source_kind=source.count(patcher.OLD)):
                with self.assertRaises(patcher.PatchError):
                    patcher.patched_source(source)

    def test_symlink_target_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / patcher.TARGET
            target.parent.mkdir(parents=True)
            outside = root / "fixture.py"
            outside.write_text(FIXTURE)
            target.symlink_to(outside)
            with self.assertRaises(patcher.PatchError):
                patcher.apply_patch(root)
            self.assertEqual(outside.read_text(), FIXTURE)


if __name__ == "__main__":
    unittest.main()
