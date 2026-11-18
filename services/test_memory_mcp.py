"""Exercise the real MCP stdio protocol with the production server module."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from memory_tools import write_snapshot, unavailable


class MemoryMCPTests(unittest.IsolatedAsyncioTestCase):
    def payload(self, result):
        self.assertFalse(result.isError, result)
        return result.structuredContent or json.loads("\n".join(part.text for part in result.content if part.type == "text"))

    async def exchange(self, snapshot, check):
        with tempfile.TemporaryDirectory(dir=ROOT) as raw:
            path = write_snapshot(Path(raw), "1" * 16, snapshot)
            # Only this test launcher supplies a synthetic input. Production
            # MCP has no path argument and always reads the bwrap-bound file.
            launcher = (
                "import runpy,sys; from pathlib import Path; "
                f"sys.path.insert(0,{str(ROOT)!r}); "
                "import memory_tools; "
                f"memory_tools.read_snapshot.__defaults__=(Path({str(path)!r}),); "
                f"runpy.run_path({str(ROOT / 'memory_mcp.py')!r},run_name='__main__')"
            )
            params = StdioServerParameters(command=sys.executable, args=["-c", launcher],
                                          env={"PYTHONPATH": str(ROOT / ".test-deps"),
                                               "PATH": os.environ.get("PATH", "")}, cwd=str(ROOT))
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    await check(session)

    async def test_real_tool_discovery_and_calls(self):
        source = {"status": "ok", "observed_at": "2026-11-03T14:00:00+08:00", "total": 6,
                  "items": [{"id": i, "content": f"fact {i}"} for i in range(1, 7)]}

        async def check(session):
            tools = (await session.list_tools()).tools
            self.assertEqual({tool.name for tool in tools},
                             {"list_my_long_term_memories", "search_my_long_term_memories"})
            for tool in tools:
                self.assertTrue(tool.annotations.readOnlyHint)
                self.assertFalse(tool.annotations.destructiveHint)
                self.assertFalse(tool.annotations.openWorldHint)
                self.assertLessEqual(set(tool.inputSchema.get("properties", {})), {"page", "query"})
            first = await session.call_tool("list_my_long_term_memories", {"page": 1})
            second = await session.call_tool("list_my_long_term_memories", {"page": 2})
            a, b = self.payload(first), self.payload(second)
            self.assertEqual((a["total"], a["pages"], len(a["items"]), len(b["items"])), (6, 2, 5, 1))
            found = await session.call_tool("search_my_long_term_memories", {"query": "fact 6"})
            self.assertEqual(self.payload(found)["items"], [{"id": 6, "content": "fact 6"}])
            malicious = await session.call_tool("list_my_long_term_memories", {"owner": "other-user"})
            # FastMCP ignores surplus arguments; they cannot change the
            # server's fixed snapshot or broaden the returned records.
            self.assertEqual(self.payload(malicious), a)

        await asyncio.wait_for(self.exchange(source, check), timeout=20)

    async def test_real_protocol_reports_unavailable(self):
        async def check(session):
            result = await session.call_tool("list_my_long_term_memories", {})
            value = self.payload(result)
            self.assertFalse(value["ok"])
            self.assertEqual(value["status"], "unavailable")
            self.assertEqual(value["code"], "read_failed")
            self.assertIsNone(value["total"])

        await asyncio.wait_for(self.exchange(unavailable("read_failed"), check), timeout=20)

    async def test_display_text_and_legacy_records_survive_real_stdio_protocol(self):
        source = {"status": "ok", "observed_at": "2026-11-03T14:00:00+08:00", "total": 6,
                  "items": [{"id": i, "content": f"raw account_{i}",
                             "display_content": f"展示内容 {i}"} for i in range(1, 6)]
                           + [{"id": 6, "content": "legacy fact"}]}

        async def check(session):
            first = self.payload(await session.call_tool("list_my_long_term_memories", {"page": 1}))
            second = self.payload(await session.call_tool("list_my_long_term_memories", {"page": 2}))
            self.assertEqual(first["items"] + second["items"], source["items"])
            for needle, index in (("account_2", 1), ("展示内容 2", 1), ("legacy fact", 5)):
                found = self.payload(await session.call_tool("search_my_long_term_memories", {"query": needle}))
                self.assertEqual(found["items"], [source["items"][index]])
            scoped = self.payload(await session.call_tool("list_my_long_term_memories", {"owner": "other-user"}))
            self.assertEqual(scoped, first)

        await asyncio.wait_for(self.exchange(source, check), timeout=20)


if __name__ == "__main__":
    unittest.main()
