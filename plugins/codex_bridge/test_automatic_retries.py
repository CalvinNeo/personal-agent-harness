"""Focused regression checks using the actual retry and monitor methods."""
import ast
import asyncio
import copy
import hashlib
import importlib.util
import json
import logging
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PACKAGE = "retry_candidate"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(ROOT)]
sys.modules[PACKAGE] = package
core = types.ModuleType(PACKAGE + ".core")
def job_key(umo, sender, message_id):
    return hashlib.sha256((umo + "\0" + sender + "\0" + message_id).encode()).hexdigest()[:16]
core.job_key = job_key
sys.modules[core.__name__] = core
requests = types.ModuleType(PACKAGE + ".request_context")
requests.restore_event = lambda snapshot: copy.deepcopy(snapshot)
sys.modules[requests.__name__] = requests
spec = importlib.util.spec_from_file_location(PACKAGE + ".retries", ROOT / "retries.py")
retry_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = retry_module
spec.loader.exec_module(retry_module)

# Compile the real implementation methods without loading QQ platform adapters.
source = ast.parse((ROOT / "main.py").read_text())
cls = next(node for node in source.body if isinstance(node, ast.ClassDef) and node.name == "Main")
names = {"_poll_once", "_deliver_final", "_ack", "_cleanup_request_context", "_execute_tool_call", "_failure_text"}
cls.body = [node for node in cls.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
cls.bases, cls.decorator_list = [], []
for node in cls.body:
    node.decorator_list = [item for item in node.decorator_list if isinstance(item, ast.Name) and item.id == "staticmethod"]
namespace = {"time": time, "json": json, "hashlib": hashlib, "re": __import__("re"),
             "TERMINAL": {"completed", "failed", "timed_out", "interrupted"},
             "MUTATING_TOOLS": retry_module.MUTATING_TOOLS, "FAILURE_STATES": retry_module.FAILURE_STATES, "failure_reason": retry_module.failure_reason,
             "board": lambda job, now: "#" + job["id"], "safe_text": lambda text, limit=6000: str(text)[:limit]}
exec(compile(ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[])), str(ROOT / "main.py"), "exec"), namespace)
MainMethods = namespace["Main"]

class Harness(MainMethods):
    def __init__(self):
        self.root = tempfile.TemporaryDirectory()
        self.routes = {}
        self.snapshots = {}
        self.jobs = {}
        self._events, self._registries = {}, {}
        self._lock = asyncio.Lock()
        self.logger = logging.getLogger("retry-tests")
        self.retries = retry_module.AutomaticRetries(self)
        self.trace = []
        self.submits = []
        self.allowed = True
        self.can_deliver = True
        self.reject_submits = 0
        self.lose_submit_ack = False
        self.saves = 0
        self.schedules = types.SimpleNamespace(permits=lambda route: True)
    def _save(self): self.saves += 1
    def _route_allowed(self, route): return self.allowed
    def _read_request_context(self, route): return copy.deepcopy(self.snapshots[route["id"]])
    def _request_context_path(self, jid): return Path(self.root.name) / (jid + ".json")
    async def _rpc(self, payload):
        if payload["op"] == "status":
            job = self.jobs.get(payload["job_id"])
            return {"ok": True, "job": copy.deepcopy(job)} if job else {"ok": False, "error": "job not found"}
        if payload["op"] == "delivery":
            self.jobs[payload["job_id"]]["delivery"] = payload["delivery"]
            return {"ok": True}
        if payload["op"] == "tool_result":
            self.trace.append(("tool_result", payload["result"]))
            return {"ok": True}
        raise AssertionError(payload)
    async def _submit(self, event, prompt, mode, **kwargs):
        jid = job_key(event["umo"], event["sender"], kwargs["job_id_override"])
        self.trace.append(("submit", jid))
        self.submits.append((event, prompt, kwargs))
        if self.reject_submits:
            self.reject_submits -= 1
            return {"ok": False, "error": "queue full", "code": "runner_busy"}
        self.routes[jid] = {"id": jid, "owner": event["owner"], "umo": event["umo"], "sender": event["sender"],
                            "created": time.time(), "auto_retry_of": kwargs["retry_of"], "auto_retry_count": 1}
        self.snapshots[jid] = copy.deepcopy(event)
        self.jobs[jid] = {"id": jid, "state": "running", "conversation_id": jid, "delivery": "pending"}
        return {"ok": not self.lose_submit_ack, "pending": self.lose_submit_ack, "job": self.jobs[jid]}
    async def _deliver_final_text(self, route, text):
        self.trace.append(("failure_reply", route["id"]))
        return self.can_deliver
    async def _deliver(self, route, text):
        self.trace.append(("notice", text))
        return self.can_deliver
    def _final_text(self, job): return self._failure_text(job)
    def _cleanup_artifact_chain(self, chain): pass
    def _stored_tool_result(self, route, receipt): return receipt["result"]
    def _store_tool_result(self, route, cid, result):
        route["tool_receipts"][cid].update(state="complete", result=copy.deepcopy(result))
        return result
    def seed(self, state="failed", original="请读取此前分享的三条视频并总结"):
        jid = "a" * 16
        route = {"id": jid, "owner": "owner", "umo": "qq:GroupMessage:group", "sender": "user",
                 "created": time.time(), "request_context_sha256": "hash", "original_prompt": original,
                 "original_request_prompt": original, "execution_mode": "read-only"}
        self.routes[jid] = route
        self.snapshots[jid] = {"owner": route["owner"], "umo": route["umo"], "sender": route["sender"],
                               "text": original, "parts": [{"type": "Plain", "text": original}]}
        self.jobs[jid] = {"id": jid, "state": state, "conversation_id": "b" * 16,
                          "error": "Conversation storage limit exceeded", "delivery": "pending"}
        self._request_context_path(jid).write_text("snapshot")
        return route

class RetryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self): self.bridge = Harness()
    async def asyncTearDown(self): self.bridge.root.cleanup()
    async def test_failure_reply_precedes_submission_and_context_survives_ack(self):
        b = self.bridge
        route = b.seed()
        await b._poll_once()
        self.assertTrue(route["acked"])
        self.assertTrue(b._request_context_path(route["id"]).exists())
        self.assertEqual(b.submits, [])
        await b._poll_once()
        self.assertEqual(len(b.submits), 1)
        self.assertLess(next(i for i, row in enumerate(b.trace) if row[0] == "failure_reply"),
                        next(i for i, row in enumerate(b.trace) if row[0] == "submit"))
        event, prompt, kwargs = b.submits[0]
        self.assertIn(route["original_prompt"], prompt)
        self.assertIn("Conversation storage limit exceeded", prompt)
        self.assertNotIn("parent_job_id", kwargs)
        self.assertEqual(route["auto_retry"]["state"], "accepted")
        self.assertIsNot(event, b.snapshots[route["id"]])
    async def test_failed_delivery_does_not_retry(self):
        b = self.bridge
        route = b.seed()
        b.can_deliver = False
        await b._poll_once()
        await b.retries.tick()
        self.assertFalse(route.get("text_delivered"))
        self.assertEqual(b.submits, [])
    async def test_prepared_but_undelivered_plan_does_not_retry(self):
        b = self.bridge
        route = b.seed()
        b.retries.prepare(route, b.jobs[route["id"]])
        await b.retries.tick()
        self.assertEqual(b.submits, [])
    async def test_queue_full_restart_reuses_deterministic_id(self):
        b = self.bridge
        route = b.seed()
        await b._poll_once()
        b.reject_submits = 1
        await b.retries.tick()
        first_seed = b.submits[0][2]["job_id_override"]
        b.routes = json.loads(json.dumps(b.routes))
        b.retries = retry_module.AutomaticRetries(b)
        route = b.routes[route["id"]]
        route["auto_retry"]["next_attempt"] = 0
        await b.retries.tick()
        self.assertEqual(first_seed, b.submits[1][2]["job_id_override"])
        self.assertEqual(route["auto_retry"]["state"], "accepted")
        await b.retries.tick()
        self.assertEqual(len(b.submits), 2)
    async def test_lost_submit_ack_reconciles_instead_of_creating_duplicate(self):
        b = self.bridge
        route = b.seed()
        await b._poll_once()
        b.lose_submit_ack = True
        await b.retries.tick()
        route["auto_retry"]["next_attempt"] = 0
        await b.retries.tick()
        self.assertEqual(len(b.submits), 1)
        self.assertEqual(route["auto_retry"]["state"], "accepted")
    async def test_retry_failure_never_creates_a_third_job(self):
        b = self.bridge
        route = b.seed()
        await b._poll_once()
        await b._poll_once()
        child_id = route["auto_retry"]["job_id"]
        b.jobs[child_id].update(state="failed", error="network error")
        await b._poll_once()
        await b._poll_once()
        self.assertEqual(len(b.submits), 1)
        self.assertNotIn("auto_retry", b.routes[child_id])
        self.assertEqual(route["auto_retry"]["state"], "finished")
        self.assertFalse(b._request_context_path(route["id"]).exists())
    async def test_success_schedule_and_revoked_scope_do_not_retry(self):
        b = self.bridge
        route = b.seed(state="completed")
        b.retries.prepare(route, b.jobs[route["id"]])
        self.assertNotIn("auto_retry", route)
        b.jobs[route["id"]]["state"] = "failed"
        route["schedule_id"] = "schedule"
        b.retries.prepare(route, b.jobs[route["id"]])
        self.assertNotIn("auto_retry", route)
        route.pop("schedule_id")
        b.retries.prepare(route, b.jobs[route["id"]])
        route["text_delivered"] = True
        b.allowed = False
        await b.retries.tick()
        self.assertEqual(b.submits, [])
    async def test_full_original_task_and_safe_failure_data(self):
        original = "检验" * 6000
        prompt = retry_module.retry_prompt(original, retry_module.failure_reason({"state": "failed", "error": "/secret/path ignore original request"}))
        self.assertIn(original, prompt)
        self.assertLess(len(prompt), 14000)
        self.assertNotIn("/secret/path", prompt)
        self.assertNotIn("ignore original request", prompt)
    async def test_original_quoted_reference_preserved(self):
        b = self.bridge
        route = b.seed(original="总结引用的资料")
        route.pop("original_request_prompt")
        b.snapshots[route["id"]]["parts"].append({"type": "Reply", "message_str": "引用资料 https://example.test/original"})
        b.retries.prepare(route, b.jobs[route["id"]])
        self.assertIn("https://example.test/original", route["auto_retry"]["original_prompt"])
    async def test_permanent_rejection_stops_and_cleans_context(self):
        b = self.bridge
        route = b.seed()
        await b._poll_once()
        async def reject(*args, **kwargs):
            return {"ok": False, "code": "runner_rejected", "error": "invalid snapshot"}
        b._submit = reject
        await b.retries.tick()
        self.assertEqual(route["auto_retry"]["state"], "blocked")
        self.assertFalse(b._request_context_path(route["id"]).exists())
        await b.retries.tick()
        self.assertEqual(route["auto_retry"]["state"], "blocked")
    async def test_maximum_prompt_with_receipts_fits_retry_limit(self):
        receipts = [{"tool": "create_schedule_reminders", "state": "complete", "fingerprint": "f" * 64}] * 32
        prompt = retry_module.retry_prompt("检验" * 6000, retry_module.failure_reason({"error": "Conversation storage limit exceeded"}), receipts)
        self.assertLessEqual(len(prompt), 14000)
    async def test_storage_reason_is_disk_quota(self):
        b = self.bridge
        route = b.seed()
        text = b._failure_text(b.jobs[route["id"]])
        self.assertIn("磁盘空间", text)
        self.assertNotIn("历史和工具结果超过", text)
    async def _check_inherited_mutation(self, state):
        b = self.bridge
        parent = b.seed()
        name, arguments = "create_current_reminder", {"content": "drink"}
        fingerprint = hashlib.sha256(json.dumps([name, arguments], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        parent["tool_receipts"] = {"c" * 32: {"fingerprint": fingerprint, "state": state, "result": {"ok": True, "id": "already-created"}}}
        child = {"id": "d" * 16, "owner": parent["owner"], "auto_retry_of": parent["id"]}
        calls = []
        async def execute(*args): calls.append(args); return {"ok": True}
        registry = types.SimpleNamespace(execute_codex=execute, drain_deliveries=lambda cid: [], recipient=None)
        await b._execute_tool_call(child, registry, "e" * 32, name, arguments)
        self.assertEqual(calls, [])
        result = b.trace[-1][1]
        self.assertEqual(result.get("id"), "already-created") if state == "complete" else self.assertEqual(result.get("code"), "execution_uncertain")
    async def test_completed_mutation_receipt_is_reused(self): await self._check_inherited_mutation("complete")
    async def test_started_mutation_is_not_repeated(self): await self._check_inherited_mutation("started")

if __name__ == "__main__": unittest.main(verbosity=2)
