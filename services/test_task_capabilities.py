"""No external messages: exercise the real per-job spool and stdio MCP protocol."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from task_capabilities import CapabilityError, MAX_ARGUMENTS, MAX_CALLS, MAX_RESULT, MAX_WAIT, TaskCapabilities, TaskCapabilitiesClient, revoke_spools, validate_tools

runner_spec = importlib.util.spec_from_file_location("capability_test_runner", Path(__file__).with_name("codex-runner.py"))
runner = importlib.util.module_from_spec(runner_spec)
runner_spec.loader.exec_module(runner)

OWNER, OTHER = "a" * 64, "b" * 64
JOB = "1" * 16
TOOLS = [{"name": "remember", "description": "Store an explicitly requested memory in this chat",
          "inputSchema": {"type": "object", "properties": {"content": {"type": "string"}},
                          "required": ["content"], "additionalProperties": False}}]


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        (self.base / ".tmp").mkdir(mode=0o700)
        self.root = self.base / ".tmp" / ("current-chat-" + JOB)
        self.server = TaskCapabilities(self.root, TOOLS, owner=OWNER, job_id=JOB)
        self.client = TaskCapabilitiesClient(self.root)

    async def asyncTearDown(self):
        self.client.release()
        self.server.close()
        self.tmp.cleanup()

    async def pending(self):
        async with asyncio.timeout(2):
            while True:
                pending = self.server.pending(OWNER)
                if pending:
                    return pending[0]
                await asyncio.sleep(.01)

    async def test_call_roundtrip_owner_and_idempotent_response(self):
        task = asyncio.create_task(self.client.call("remember", {"content": "test"}))
        call = await self.pending()
        self.assertEqual(call["arguments"], {"content": "test"})
        self.assertEqual(set(call), {"call_id", "name", "arguments"})
        for operation in (lambda: self.server.pending(OTHER), lambda: self.server.respond(OTHER, call["call_id"], {})):
            with self.assertRaisesRegex(CapabilityError, "not found"):
                operation()
        result = {"ok": True, "id": "memory-1"}
        self.assertTrue(self.server.respond(OWNER, call["call_id"], result))
        self.assertFalse(self.server.respond(OWNER, call["call_id"], result))
        with self.assertRaisesRegex(CapabilityError, "different result"):
            self.server.respond(OWNER, call["call_id"], {"ok": False})
        self.assertEqual(await task, result)
        self.assertEqual(self.server.pending(OWNER), [])

    async def test_unadvertised_tool_and_identity_arguments_are_rejected(self):
        with self.assertRaisesRegex(CapabilityError, "not available"):
            await self.client.call("send_other_group", {})
        with self.assertRaisesRegex(CapabilityError, "advertised schema"):
            await self.client.call("remember", {"content": "test", "owner": OTHER})
        self.assertEqual(self.server.pending(OWNER), [])

    async def test_expired_call_does_not_dispatch(self):
        task = asyncio.create_task(self.client.call("remember", {"content": "test"}, timeout=.03))
        with self.assertRaisesRegex(CapabilityError, "timed out"):
            await task
        self.assertEqual(self.server.pending(OWNER), [])

    async def test_timeout_budget_covers_plugins_and_is_still_bounded(self):
        self.assertEqual(MAX_WAIT, 240)
        for timeout in (0, MAX_WAIT + 1, float("inf"), True):
            with self.assertRaisesRegex(CapabilityError, "240 seconds"):
                await self.client.call("remember", {"content": "test"}, timeout=timeout)
        self.assertEqual(self.server.pending(OWNER), [])

    async def test_closed_task_cannot_leak_into_resumed_job(self):
        task = asyncio.create_task(self.client.call("remember", {"content": "old"}))
        await self.pending()
        self.server.close()
        with self.assertRaises(CapabilityError):
            await task
        next_job = "2" * 16
        next_server = TaskCapabilities(self.root.with_name("current-chat-" + next_job), TOOLS, owner=OWNER, job_id=next_job)
        try:
            self.assertEqual(next_server.pending(OWNER), [])
            with self.assertRaisesRegex(CapabilityError, "no longer active"):
                self.server.pending(OWNER)
        finally:
            next_server.close()

    async def test_oversize_payload_and_results_are_bounded(self):
        with self.assertRaisesRegex(CapabilityError, "size limit"):
            await self.client.call("remember", {"content": "x" * MAX_ARGUMENTS})
        task = asyncio.create_task(self.client.call("remember", {"content": "test"}))
        call = await self.pending()
        with self.assertRaisesRegex(CapabilityError, "size limit"):
            self.server.respond(OWNER, call["call_id"], "x" * (MAX_RESULT + 1))
        self.server.respond(OWNER, call["call_id"], {"ok": False, "code": "too_large"})
        self.assertEqual((await task)["code"], "too_large")

    async def test_symlink_request_does_not_read_external_file(self):
        target = self.base / "outside.json"
        target.write_text('{"private":"should-not-be-read"}')
        (self.root / ("f" * 32 + ".request.json")).symlink_to(target)
        self.assertEqual(self.server.pending(OWNER), [])
        response = json.loads((self.root / "responses" / ("f" * 32 + ".response.json")).read_text())
        self.assertFalse(response["result"]["ok"])
        self.assertNotIn("should-not-be-read", json.dumps(response))

    async def test_manifest_mutation_and_replaced_root_fail_closed(self):
        manifest = self.root / "manifest.json"
        manifest.chmod(0o600)
        manifest.write_text('{}')
        with self.assertRaisesRegex(CapabilityError, "manifest was changed"):
            self.server.pending(OWNER)
        with self.assertRaisesRegex(CapabilityError, "manifest was changed"):
            self.client.list_tools()

    async def test_replaced_root_never_reads_replacement_payloads(self):
        previous = self.root.with_name("retired")
        self.root.rename(previous)
        self.root.mkdir(mode=0o700)
        (self.root / "active.json").write_text('{}')
        with self.assertRaisesRegex(CapabilityError, "replaced"):
            self.server.pending(OWNER)
        with self.assertRaisesRegex(CapabilityError, "replaced"):
            self.client.list_tools()

    async def test_interrupted_spools_revoked_before_resume(self):
        revoke_spools(self.root.parent)
        self.assertFalse(self.root.exists())
        with self.assertRaises(CapabilityError):
            self.client.list_tools()

    async def test_call_budget_is_enforced(self):
        for index in range(MAX_CALLS):
            request = {"call_id": f"{index:032x}", "job_id": JOB, "generation": self.server.generation,
                       "name": "remember", "arguments": {"content": "test"}, "deadline": time.time() + 30}
            (self.root / (request["call_id"] + ".request.json")).write_text(json.dumps(request))
        self.assertEqual(len(self.server.pending(OWNER)), MAX_CALLS)
        with self.assertRaisesRegex(CapabilityError, "call limit"):
            await self.client.call("remember", {"content": "one too many"})

    async def test_mutating_call_after_dispatch_is_rejected(self):
        task = asyncio.create_task(self.client.call("remember", {"content": "first"}))
        call = await self.pending()
        path = self.root / (call["call_id"] + ".request.json")
        request = json.loads(path.read_text())
        request["arguments"]["content"] = "different"
        path.write_text(json.dumps(request))
        self.assertEqual(self.server.pending(OWNER), [])
        self.assertEqual((await task)["code"], "call_changed")

    async def test_precreated_response_is_rejected_and_cannot_overwrite_a_link(self):
        task = asyncio.create_task(self.client.call("remember", {"content": "first"}))
        call = await self.pending()
        outside = self.base / "outside-result.json"
        outside.write_text('{"untouched":true}')
        path = self.root / "responses" / (call["call_id"] + ".response.json")
        path.symlink_to(outside)
        with self.assertRaisesRegex(CapabilityError, "pre-created"):
            self.server.respond(OWNER, call["call_id"], {"ok": True})
        self.assertEqual(outside.read_text(), '{"untouched":true}')
        with self.assertRaises(CapabilityError):
            await task

    async def test_invalid_manifests_never_create_spool(self):
        for tools in [[TOOLS[0], TOOLS[0]], [{"name": "bad-name", "inputSchema": {"type": "object"}}],
                      [{"name": "external", "inputSchema": {"type": "object", "$ref": "https://example.com/schema"}}]]:
            with self.assertRaises(CapabilityError):
                validate_tools(tools)

    async def _stdio_result(self, payload):
        script = Path(__file__).with_name("task_capabilities_mcp.py")
        process = await asyncio.create_subprocess_exec(sys.executable, "-u", str(script), str(self.root),
                                                       stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                                                       stderr=asyncio.subprocess.PIPE, limit=16 * 1024 * 1024)

        async def write(value):
            process.stdin.write(json.dumps(value).encode() + b"\n")
            await process.stdin.drain()

        async def read_response(wanted):
            async with asyncio.timeout(4):
                while True:
                    line = await process.stdout.readline()
                    if not line:
                        self.fail((await process.stderr.read()).decode())
                    response = json.loads(line)
                    if response.get("id") == wanted:
                        return response

        try:
            await write({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}})
            self.assertIn("result", await read_response(1))
            await write({"jsonrpc": "2.0", "method": "notifications/initialized"})
            await write({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
            listed = await read_response(2)
            self.assertEqual(listed["result"]["tools"][0]["inputSchema"], TOOLS[0]["inputSchema"])
            await write({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
                "name": "remember", "arguments": {"content": "same event"}}})
            call = await self.pending()
            self.server.respond(OWNER, call["call_id"], payload)
            response = await read_response(3)
            return response["result"]
        finally:
            process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), 3)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()

    @unittest.skipUnless(importlib.util.find_spec("mcp"), "MCP runtime not installed")
    async def test_stdio_mcp_lists_and_executes_same_schema(self):
        result = await self._stdio_result({"ok": True, "saved": "same event"})
        self.assertFalse(result["isError"])
        self.assertEqual(json.loads(result["content"][0]["text"])["saved"], "same event")

    @unittest.skipUnless(importlib.util.find_spec("mcp"), "MCP runtime not installed")
    async def test_stdio_native_image_result_keeps_image_content(self):
        import base64
        import io
        from PIL import Image
        output = io.BytesIO()
        Image.frombytes("RGB", (128, 128), os.urandom(128 * 128 * 3)).save(output, format="PNG")
        data = base64.b64encode(output.getvalue()).decode()
        result = await self._stdio_result({"_mcp_result": {"isError": False, "content": [
            {"type": "text", "text": "Synthetic image fixture"},
            {"type": "image", "mimeType": "image/png", "data": data}]}})
        self.assertFalse(result["isError"])
        self.assertEqual([part["type"] for part in result["content"]], ["text", "image"])
        self.assertEqual(result["content"][1]["mimeType"], "image/png")
        self.assertEqual(result["content"][1]["data"], data)

    @unittest.skipUnless(importlib.util.find_spec("mcp"), "MCP runtime not installed")
    async def test_stdio_invalid_native_envelope_reports_error(self):
        result = await self._stdio_result({"_mcp_result": {"content": [{"type": "missing_type"}]}})
        self.assertTrue(result["isError"])
        self.assertEqual(json.loads(result["content"][0]["text"])["code"], "invalid_mcp_result")


class RunnerCapabilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.patch_root = patch.object(runner, "ROOT", self.base / "workspaces")
        self.patch_root.start()
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.paths = []
        self.fail = False

        async def fake_run(prompt, mode, workspace, progress, **kwargs):
            kwargs["on_thread"]("11111111-1111-4111-8111-111111111111")
            self.paths.append(kwargs.get("capability_path"))
            self.assertIsNotNone(kwargs["memory_path"])
            self.started.set()
            await self.release.wait()
            if self.fail:
                raise ValueError("test failure")
            return "Local capability completed"

        self.fake_patch = patch.object(runner, "run_codex", fake_run)
        self.fake_patch.start()
        self.manager = runner.JobManager(self.base / "jobs")
        await self.manager.start()

    async def asyncTearDown(self):
        self.release.set()
        await self.manager.close()
        self.fake_patch.stop()
        self.patch_root.stop()
        self.tmp.cleanup()

    def submit(self, jid=JOB, tools=None, **extra):
        return self.manager.request({"op": "submit", "owner": OWNER, "workspace": "c" * 32,
                                     "prompt": "Use the current chat tool", "job_id": jid,
                                     "capability_tools": TOOLS if tools is None else tools, **extra})

    async def finish(self, jid=JOB):
        self.release.set()
        async with asyncio.timeout(3):
            while self.manager.jobs[jid]["state"] in runner.ACTIVE:
                await asyncio.sleep(.01)

    async def test_rpc_roundtrip_owner_scope_and_terminal_revocation(self):
        self.submit()
        await asyncio.wait_for(self.started.wait(), 2)
        path = self.paths[-1]
        client = TaskCapabilitiesClient(path)
        try:
            task = asyncio.create_task(client.call("remember", {"content": "approved"}))
            async with asyncio.timeout(2):
                while True:
                    response = self.manager.request({"op": "tool_pending", "owner": OWNER, "job_id": JOB})
                    if response["calls"]:
                        call = response["calls"][0]
                        break
                    await asyncio.sleep(.01)
            with self.assertRaisesRegex(ValueError, "job not found"):
                self.manager.request({"op": "tool_pending", "owner": OTHER, "job_id": JOB})
            self.assertTrue(self.manager.request({"op": "tool_result", "owner": OWNER, "job_id": JOB,
                              "call_id": call["call_id"], "result": {"ok": True, "saved": "approved"}})["accepted"])
            self.assertEqual(await task, {"ok": True, "saved": "approved"})
            await self.finish()
            self.assertFalse(path.exists())
            self.assertEqual(self.manager.capabilities, {})
            with self.assertRaisesRegex(ValueError, "capability_unavailable"):
                self.manager.request({"op": "tool_pending", "owner": OWNER, "job_id": JOB})
        finally:
            client.release()

    async def test_submit_schema_collision_and_no_manifest_in_job_records(self):
        self.submit()
        self.assertTrue(self.submit()["ok"])
        changed = [dict(TOOLS[0], description="A changed capability")]
        with self.assertRaisesRegex(ValueError, "collision"):
            self.submit(tools=changed)
        record = (self.base / "jobs" / (JOB + ".json")).read_text()
        self.assertNotIn("_capability_tools", record)
        self.assertNotIn("Store an explicitly requested memory", record)
        self.assertNotIn("capability_tools", self.manager.public(self.manager.jobs[JOB]))
        await self.finish()

    async def test_failed_job_removes_capabilities(self):
        self.fail = True
        self.submit()
        await asyncio.wait_for(self.started.wait(), 2)
        path = self.paths[-1]
        await self.finish()
        self.assertEqual(self.manager.jobs[JOB]["state"], "failed")
        self.assertEqual(self.manager.capabilities, {})
        self.assertFalse(path.exists())

    async def test_resumed_job_gets_its_own_catalog_and_lease(self):
        self.submit()
        await asyncio.wait_for(self.started.wait(), 2)
        old = self.paths[-1]
        await self.finish()
        self.started.clear()
        self.release.clear()
        next_job = "2" * 16
        replacement = [dict(TOOLS[0], name="travel_help")]
        self.submit(next_job, tools=replacement, parent_job_id=JOB)
        await asyncio.wait_for(self.started.wait(), 2)
        current = self.paths[-1]
        self.assertNotEqual(current, old)
        self.assertFalse(old.exists())
        client = TaskCapabilitiesClient(current)
        try:
            self.assertEqual([tool["name"] for tool in client.list_tools()], ["travel_help"])
            with self.assertRaisesRegex(CapabilityError, "not available"):
                await client.call("remember", {"content": "stale"})
        finally:
            client.release()
        await self.finish(next_job)

    async def test_command_combines_local_tools_and_memory_without_weakening_mounts(self):
        workspace = self.base / "command-workspace"
        (workspace / ".tmp").mkdir(parents=True, mode=0o700)
        spool = TaskCapabilities(workspace / ".tmp" / ("current-chat-" + JOB), TOOLS, owner=OWNER, job_id=JOB)
        memory = self.base / "snapshot.json"
        memory.write_text('{}')
        try:
            command = runner.command(workspace, "read-only", capability_path=spool.root, memory_path=memory)
            manifest = str(spool.root / "manifest.json")
            position = command.index(manifest)
            self.assertEqual(command[position - 1], "--ro-bind")
            responses_position = command.index(str(spool.root / "responses"))
            self.assertEqual(command[responses_position - 1], "--ro-bind")
            self.assertIn('mcp_servers.current_chat.required=true', command)
            self.assertIn('mcp_servers.current_chat.env={}', command)
            self.assertIn('mcp_servers.current_chat.env_vars=[]', command)
            self.assertIn('mcp_servers.current_chat.tool_timeout_sec=245', command)
            self.assertIn('mcp_servers.current_chat.default_tools_approval_mode="approve"', command)
            self.assertIn('mcp_servers.long_term_memory.tool_timeout_sec=10', command)
            argument = next(value for value in command if value.startswith('mcp_servers.current_chat.args='))
            self.assertEqual(json.loads(argument.split('=', 1)[1]), ["-I", str(Path(runner.__file__).with_name("task_capabilities_mcp.py")), str(spool.root)])
            with self.assertRaisesRegex(ValueError, "invalid task capability path"):
                runner.command(self.base / "another-workspace", "read-only", capability_path=spool.root)
            home = workspace.parent / "home"
            home.mkdir(mode=0o700)
            resumed = runner.command(workspace, "read-only", session_home=home,
                resume_thread_id="11111111-1111-4111-8111-111111111111", capability_path=spool.root, memory_path=memory)
            following = resumed[resumed.index("resume") + 1:]
            self.assertIn('mcp_servers.current_chat.default_tools_approval_mode="approve"', following)
            self.assertTrue(any(value.startswith('mcp_servers.current_chat.args=') for value in following))
            self.assertTrue(any(value.startswith('mcp_servers.long_term_memory.args=') for value in following))
        finally:
            spool.close()


if __name__ == "__main__":
    unittest.main()
