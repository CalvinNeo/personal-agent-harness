#!/usr/bin/env python3
"""Real Codex/bwrap smoke with synthetic local tools and no QQ adapter.

Run as the production runner user, with its existing EnvironmentFile and
sandbox properties. Point --services at the candidate/installed service
directory; --state-root must be a NEW private directory on an allowed writable
mount. Nothing imports an AstrBot plugin or reads a real memory database.

The script requires --execute-model explicitly. Two real model turns verify a
tool call and a refreshed catalog on the same retained Codex session. A random
fact is generated only by the mock broker, so echoing the prompt cannot pass.
No production jobs, production socket or QQ message are created.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import secrets
import sys
import time


TOOL_NAME = "verify_current_chat_context"
DEPLOY_FILES = (
    "plugins/codex_bridge/main.py", "plugins/codex_bridge/capabilities.py",
    "plugins/codex_bridge/request_context.py", "plugins/codex_bridge/recipients.py",
    "plugins/codex_bridge/memory.py", "plugins/codex_bridge/metadata.yaml", "plugins/codex_bridge/README.md",
    "plugins/long_term_memory/main.py", "plugins/long_term_memory/metadata.yaml", "plugins/long_term_memory/README.md",
    "plugins/reminder/main.py", "plugins/reminder/metadata.yaml",
    "plugins/image_context/main.py", "plugins/image_context/metadata.yaml",
    "services/codex-runner.py", "services/task_capabilities.py", "services/task_capabilities_mcp.py",
    "services/memory_tools.py", "services/memory_mcp.py",
)


def candidate_manifest_digest(candidate: Path) -> str:
    """Identical file allowlist/serialization to the release deploy manifest."""
    values = {relative: hashlib.sha256((candidate / relative).read_bytes()).hexdigest()
              for relative in DEPLOY_FILES}
    return hashlib.sha256(json.dumps(values, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def installed_service_hashes(services: Path) -> dict:
    return {relative: hashlib.sha256((services / relative.split("/", 1)[1]).read_bytes()).hexdigest()
            for relative in DEPLOY_FILES if relative.startswith("services/")}


def load_runner(services: Path):
    path = services / "codex-runner.py"
    spec = importlib.util.spec_from_file_location("synthetic_capability_smoke_runner", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SyntheticRegistry:
    """A fake bridge registry; its only data is one freshly generated fact."""
    def __init__(self, phase: str):
        self.phase = phase
        self.fact = "SYNTHETIC_LOCAL_FACT_" + phase.upper() + "_" + secrets.token_hex(12)
        self.calls = []

    def schemas(self):
        return [{"name": TOOL_NAME,
                 "description": "Synthetic deployment smoke fixture only; return the current originating-chat test fact.",
                 "inputSchema": {"type": "object", "properties": {"phase": {"type": "string", "enum": [self.phase]}},
                                 "required": ["phase"], "additionalProperties": False}}]

    async def execute(self, name: str, arguments: dict):
        if name != TOOL_NAME or arguments != {"phase": self.phase}:
            raise AssertionError("Worker called an old or unexpected capability schema")
        self.calls.append({"name": name, "phase": self.phase})
        return {"ok": True, "phase": self.phase, "synthetic_fact": self.fact,
                "source": "synthetic originating-chat fixture; no real user data"}


async def exercise(runner, state: Path, timeout: int):
    runner.ROOT = state / "workspaces"
    runner.JOBS = state / "jobs"
    runner.TIMEOUT = timeout
    if not runner.MODEL:
        raise ValueError("HARNESS_MODEL must come from the runner service environment")
    if not Path(runner.CODEX_BWRAP).is_file():
        raise ValueError("Production Codex bwrap wrapper is unavailable")
    manager = runner.JobManager(runner.JOBS)
    owner = hashlib.sha256(("synthetic-capability-smoke-" + secrets.token_hex(16)).encode()).hexdigest()
    other = hashlib.sha256(("different-synthetic-owner-" + secrets.token_hex(16)).encode()).hexdigest()
    registries, results, spools = {}, {}, []
    stopping = asyncio.Event()
    report = {"ok": False, "real_model_turns": 0, "synthetic_tool_calls": 0,
              "qq_messages_sent": 0, "production_jobs_created": 0,
              "real_memories_read": 0, "checks": []}

    async def verify_spool_mounts():
        workspace = runner.ROOT / "synthetic-spool-preflight"
        (workspace / ".tmp").mkdir(parents=True, mode=0o700)
        (workspace / "output").mkdir(mode=0o700)
        jid = secrets.token_hex(8)
        transport = runner.task_capabilities.TaskCapabilities(workspace / ".tmp" / ("current-chat-" + jid),
            SyntheticRegistry("preflight").schemas(), owner=owner, job_id=jid)
        try:
            command = runner.command(workspace, "read-only", capability_path=transport.root)
            # Execute only a local filesystem probe under the exact outer
            # wrapper. This preflight never starts Codex or a model request.
            command = command[:command.index("--") + 1]
            probe = (
                "import errno,json,os,pathlib\n"
                "root=pathlib.Path(" + repr(str(transport.root)) + ")\n"
                "for target,chmod in [(root/'manifest.json',True),(root/'responses'/'forged.json',False)]:\n"
                " try:\n"
                "  if chmod: os.chmod(target,0o600)\n"
                "  else: target.write_text('{}')\n"
                " except OSError as error:\n"
                "  assert error.errno in (errno.EROFS,errno.EACCES,errno.EPERM),type(error).__name__\n"
                " else: raise AssertionError('worker could forge a trusted transport file')\n"
                "(root/'synthetic-writable-request.json').write_text('{}')\n"
                "print(json.dumps({'spool_boundaries_verified':True}))\n"
            )
            command.extend([runner.IMAGE_PYTHON, "-I", "-c", probe])
            environment = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
                           "HOME": str(Path(runner.CODEX_HOME).parent), "CODEX_HOME": runner.CODEX_HOME,
                           "TMPDIR": str(workspace / ".tmp")}
            process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, env=environment, start_new_session=True)
            try:
                output, _ = await asyncio.wait_for(process.communicate(), 20)
            except BaseException:
                await runner.reap_process(process)
                raise
            if process.returncode != 0 or json.loads(output) != {"spool_boundaries_verified": True}:
                raise AssertionError("Actual bwrap did not enforce manifest/response read-only mounts")
            report["checks"].append("actual bwrap permits requests but denies manifest edits and forged tool responses")
        finally:
            transport.close()

    async def broker():
        while not stopping.is_set():
            for jid, registry in list(registries.items()):
                if manager.jobs[jid]["state"] != "running" or jid not in manager.capabilities:
                    continue
                spool = manager.capabilities[jid].root
                if spool not in spools:
                    spools.append(spool)
                pending = manager.request({"op": "tool_pending", "owner": owner, "job_id": jid})
                for call in pending["calls"]:
                    key = (jid, call["call_id"])
                    if key not in results:
                        results[key] = await registry.execute(call["name"], call["arguments"])
                    manager.request({"op": "tool_result", "owner": owner, "job_id": jid,
                                     "call_id": call["call_id"], "result": results[key]})
            await asyncio.sleep(.1)

    async def wait_job(jid, broker_task):
        last_progress = 0.0
        async with asyncio.timeout(timeout + 15):
            while manager.jobs[jid]["state"] in runner.ACTIVE:
                if broker_task.done():
                    await broker_task
                    raise RuntimeError("Synthetic broker exited unexpectedly")
                if time.monotonic() - last_progress >= 15:
                    last_progress = time.monotonic()
                    print(json.dumps({"event": "smoke_progress", "job": jid,
                                      "phase": manager.jobs[jid].get("phase")}), flush=True)
                await asyncio.sleep(.1)
        return manager.jobs[jid]

    await verify_spool_mounts()
    await manager.start()
    broker_task = asyncio.create_task(broker())
    try:
        previous = None
        for phase in ("first", "refreshed"):
            registry = SyntheticRegistry(phase)
            jid = secrets.token_hex(8)
            prompt = (
                "This is a deployment smoke test using only synthetic data. "
                f"Discover and call the current_chat MCP tool {TOOL_NAME} with phase={phase!r}. "
                "You must actually invoke that tool in this turn, even if an earlier turn called a similarly named tool. "
                "Its current input schema was refreshed for this turn. Do not use shell, public web, memory tools, "
                "or any other capability. Return only the exact synthetic_fact from this turn's successful tool result. "
                "If the tool fails, report the exact tool error and do not invent a fact."
            )
            payload = {"op": "submit", "job_id": jid, "owner": owner,
                       "workspace": hashlib.sha256(b"synthetic smoke workspace").hexdigest()[:32],
                       "mode": "read-only", "prompt": prompt, "capability_tools": registry.schemas(),
                       "memory_snapshot": {"status": "ok", "observed_at": runner.now(), "items": [], "total": 0}}
            if previous is not None:
                payload["parent_job_id"] = previous
            response = manager.request(payload)
            if not response.get("ok"):
                raise AssertionError("Synthetic job was not accepted")
            registries[jid] = registry
            report["real_model_turns"] += 1
            try:
                manager.request({"op": "status", "job_id": jid, "owner": other})
            except ValueError:
                pass
            else:
                raise AssertionError("Different owner was allowed to inspect a synthetic job")
            job = await wait_job(jid, broker_task)
            if job["state"] != "completed":
                raise AssertionError("Real worker failed: " + str(job.get("error") or job["state"]))
            if not registry.calls or registry.fact not in (job.get("result") or ""):
                raise AssertionError("Model did not return the fact from an actual local capability call")
            if previous is not None:
                if job.get("parent_job_id") != previous or job["conversation_id"] != manager.jobs[previous]["conversation_id"]:
                    raise AssertionError("Second turn did not resume the retained Codex conversation")
                if registries[previous].fact in job["result"]:
                    raise AssertionError("Second turn reused the earlier tool result")
                report["checks"].append("retained session resumed with refreshed tool schema and new tool result")
            else:
                report["checks"].append("actual model discovered and invoked current_chat over stdio MCP inside bwrap")
            try:
                manager.request({"op": "tool_pending", "job_id": jid, "owner": owner})
            except ValueError as error:
                if "capability_unavailable" not in str(error):
                    raise
            else:
                raise AssertionError("Completed job still exposed a capability lease")
            if jid in manager.capabilities:
                raise AssertionError("Completed job retained a live transport")
            previous = jid
        report["checks"].extend(["different owner denied before tool dispatch",
                                 "terminal jobs deny further tool RPCs",
                                 "earlier task spools removed before session continuation"])
        if any(path.exists() for path in spools):
            raise AssertionError("An earlier task's capability spool was retained")
        report["synthetic_tool_calls"] = sum(len(registry.calls) for registry in registries.values())
        report["ok"] = True
        return report
    finally:
        stopping.set()
        broker_task.cancel()
        await asyncio.gather(broker_task, return_exceptions=True)
        await manager.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--services", type=Path, required=True)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--manifest-root", type=Path,
                        help="Frozen candidate ledger root when executing actually installed --services")
    parser.add_argument("--deployment-report", type=Path,
                        help="Deployment JSON whose installed_hashes must match all five service files")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--execute-model", action="store_true")
    args = parser.parse_args()
    if not args.execute_model:
        parser.error("--execute-model is required to start the two real model calls")
    if not 30 <= args.timeout <= 600:
        parser.error("--timeout must be between 30 and 600 seconds per model turn")
    # Keep the service's namespace-visible alias (e.g. /opt); resolving a
    # compatibility link back into /root could defeat ProtectHome isolation.
    services = args.services.absolute()
    if not services.is_dir():
        parser.error("--services must name the candidate or installed service directory")
    manifest_root = args.manifest_root.absolute() if args.manifest_root else services.parent
    actual_start = installed_service_hashes(services)
    if args.deployment_report:
        deployment = json.loads(args.deployment_report.read_text())
        expected = deployment.get("installed_hashes", {})
        if deployment.get("ok") is not True or any(expected.get(key) != value for key, value in actual_start.items()):
            parser.error("Actually installed service files do not match the successful deployment ledger")
    state = args.state_root.absolute()
    if any(path.is_symlink() for path in (state, *state.parents)):
        parser.error("--state-root must not contain symlinks")
    state.mkdir(mode=0o700)  # Never reuse production jobs or an earlier smoke.
    start_digest = candidate_manifest_digest(manifest_root)
    print(json.dumps({"event": "candidate_bound", "candidate_manifest_sha256": start_digest}), flush=True)
    report = {"ok": False, "qq_messages_sent": 0, "production_jobs_created": 0, "real_memories_read": 0,
              "candidate_manifest_sha256": start_digest}
    try:
        report = asyncio.run(exercise(load_runner(services), state, args.timeout))
    except BaseException as error:
        report["error_type"] = type(error).__name__
        report["error"] = str(error)[:500]
        raise
    finally:
        report["candidate_manifest_sha256"] = start_digest
        report["candidate_manifest_end_sha256"] = candidate_manifest_digest(manifest_root)
        report["candidate_unchanged"] = report["candidate_manifest_end_sha256"] == start_digest
        report["executed_services"] = str(services)
        report["installed_service_hashes"] = actual_start
        report["installed_service_end_hashes"] = installed_service_hashes(services)
        report["installed_services_unchanged"] = report["installed_service_end_hashes"] == actual_start
        report["installed_deployment_match"] = bool(args.deployment_report)
        if args.deployment_report:
            report["deployment_report_sha256"] = hashlib.sha256(args.deployment_report.read_bytes()).hexdigest()
        if not report["installed_services_unchanged"]:
            report.update(ok=False, error="Actually installed services changed during smoke execution")
        if not report["candidate_unchanged"]:
            report.update(ok=False, error="Candidate source changed during smoke execution")
        path = state / "report.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        path.chmod(0o600)
        print(json.dumps(report, ensure_ascii=False), flush=True)
    if not report["candidate_unchanged"]:
        raise SystemExit("Candidate source changed during smoke execution")
    if not report["installed_services_unchanged"]:
        raise SystemExit("Actually installed services changed during smoke execution")


if __name__ == "__main__":
    main()
