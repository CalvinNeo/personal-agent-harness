#!/usr/bin/env python3
"""Dynamic originating-chat tools over stdio, backed by one active task spool."""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from task_capabilities import CapabilityError, TaskCapabilitiesClient


def build_server(client: TaskCapabilitiesClient):
    from mcp.server import Server
    from mcp import types

    async def listed():
        return [types.Tool(name=tool["name"], description=tool["description"], inputSchema=tool["inputSchema"])
                for tool in client.list_tools()]

    async def called(name, arguments):
        try:
            result = await client.call(name, arguments or {})
        except CapabilityError as exc:
            result = exc.result()
        if isinstance(result, dict) and set(result) == {"_mcp_result"}:
            try:
                # Preserve native image/audio/resource blocks returned by
                # existing plugins; wrapping them in JSON text loses vision.
                return types.CallToolResult.model_validate(result["_mcp_result"])
            except (TypeError, ValueError):
                result = {"ok": False, "code": "invalid_mcp_result",
                          "error": "Local tool returned an invalid native MCP result"}
        return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False))],
                                    isError=isinstance(result, dict) and result.get("ok") is False)

    instructions = ("These tools execute in the exact originating QQ conversation as the current user. "
                    "Use the advertised tools for memory, reminders, travel, Xiaohongshu and mentions. "
                    "Do not invent owner, sender, conversation IDs or tool results. A successful tool receipt "
                    "is required before claiming an action completed. Tool failures are explicit errors; "
                    "do not replace them with a claim that the user has not granted permission.")
    # Low-level Server never loads FastMCP Settings or a working-directory .env.
    # Both deployed MCP 1.x and the local validation runtime are supported.
    if hasattr(Server, "list_tools"):
        server = Server("current_chat", instructions=instructions)

        @server.list_tools()
        async def list_tools():
            return await listed()

        @server.call_tool(validate_input=False)
        async def call_tool(name, arguments):
            return await called(name, arguments)
    else:
        async def list_tools(ctx, params):
            return types.ListToolsResult(tools=await listed())

        async def call_tool(ctx, params):
            return await called(params.name, params.arguments)

        server = Server("current_chat", instructions=instructions,
                        on_list_tools=list_tools, on_call_tool=call_tool)
    return server


async def main(root: Path) -> None:
    from mcp.server.stdio import stdio_server

    client = TaskCapabilitiesClient(root)
    try:
        server = build_server(client)
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())
    finally:
        client.release()


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    if len(sys.argv) != 2:
        raise SystemExit("usage: task_capabilities_mcp.py /private/workspace/.tmp/current-chat-<job-id>")
    try:
        asyncio.run(main(Path(sys.argv[1])))
    except CapabilityError as error:
        print(json.dumps(error.result(), ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
