"""MCP host: transports + signed-manifest verification (finaldesign.md §3, §11).
InProcessTransport speaks real JSON-RPC 2.0 text frames to a server object (serialization is timed for KPI-30).
StdioTransport uses the official `mcp` SDK (ClientSession over stdio) for out-of-process servers."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from pathlib import Path

from agent import errors
from agent.telemetry import REC

MANIFEST = Path("mcp_servers/manifests.json")
SERVER_FILES = {"hybrid-rag": "mcp_servers/rag_server.py", "state-store": "mcp_servers/state_server.py",
                "graphify-vault": "mcp_servers/graphify_server.py"}


def _sig(path: str, key: bytes) -> str:
    return hmac.new(key, Path(path).read_bytes(), hashlib.sha256).hexdigest()


def sign_manifests(key: bytes | None = None) -> dict:
    key = key or os.environ["MCP_MANIFEST_KEY"].encode()
    m = {n: _sig(f, key) for n, f in SERVER_FILES.items()}
    MANIFEST.write_text(json.dumps(m, indent=2))
    return m


def verify_manifests(key: bytes | None = None, files: dict | None = None):
    """Unsigned/changed manifests refuse to load."""
    k = os.environ.get("MCP_MANIFEST_KEY")
    if key is None and not k:
        raise RuntimeError("MCP_MANIFEST_KEY (secret manager) is required to verify server manifests")
    key = key or k.encode()
    if not MANIFEST.exists():
        raise RuntimeError("unsigned MCP manifests: run tools/sign_manifests.py")
    m = json.loads(MANIFEST.read_text())
    for n, f in (files or SERVER_FILES).items():
        if not hmac.compare_digest(m.get(n, ""), _sig(f, key)):
            raise RuntimeError(f"MCP server manifest verification failed for {n}")


class InProcessTransport:
    kind = "in-process"

    def __init__(self, server):
        self.server = server
        self.up = True
        self._id = 0

    async def call(self, tool: str, arguments: dict) -> dict:
        if not self.up:
            raise errors.McpError(errors.TIMEOUT, "server down")
        self._id += 1
        t0 = time.perf_counter()
        raw = json.dumps({"jsonrpc": "2.0", "id": self._id, "method": "tools/call", "params": {"name": tool, "arguments": arguments}})
        t1 = time.perf_counter()
        resp = await self.server.dispatch(raw)
        t2 = time.perf_counter()
        msg = json.loads(resp)
        t3 = time.perf_counter()
        REC.observe("mcp.jsonrpc.serde", ((t1 - t0) + (t3 - t2)) * 1000)
        if "error" in msg:
            e = msg["error"]
            raise errors.McpError(e["code"], e.get("message", ""), e.get("data"))
        return msg["result"]

    async def list_tools(self):
        return json.loads(await self.server.dispatch(json.dumps({"jsonrpc": "2.0", "id": 0, "method": "tools/list"})))["result"]["tools"]


class StdioTransport:
    """Out-of-process server via the official MCP SDK."""
    kind = "stdio"

    def __init__(self, command: str, args: list[str]):
        self.command, self.args = command, args
        self._stack = None
        self.session = None

    async def start(self):
        from contextlib import AsyncExitStack
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        self._stack = AsyncExitStack()
        r, w = await self._stack.enter_async_context(stdio_client(StdioServerParameters(command=self.command, args=self.args, env=dict(os.environ))))
        self.session = await self._stack.enter_async_context(ClientSession(r, w))
        await self.session.initialize()

    async def call(self, tool: str, arguments: dict) -> dict:
        res = await self.session.call_tool(tool, {"arguments": arguments})
        msg = json.loads(res.content[0].text) if res.content and hasattr(res.content[0], "text") else res.structuredContent
        msg = msg.get("result", msg) if isinstance(msg, dict) else msg
        if isinstance(msg, dict) and "error" in msg:
            e = msg["error"]
            raise errors.McpError(e["code"], e.get("message", ""), e.get("data"))
        return msg["result"] if isinstance(msg, dict) and "result" in msg else msg

    async def stop(self):
        if self._stack:
            await self._stack.aclose()

    async def restart(self):
        try:
            await self.stop()
        finally:
            await self.start()


class McpHost:
    def __init__(self, transports: dict, replicas: dict | None = None):
        self.transports = transports
        self.replicas = replicas or {}  # server name -> read-replica transport (cached read-replica index, FMEA #3)
        self.failovers = 0

    async def call(self, server: str, tool: str, arguments: dict) -> dict:
        try:
            return await self.transports[server].call(tool, arguments)
        except errors.McpError as e:
            rep = self.replicas.get(server)
            if rep is None or e.code not in (errors.TIMEOUT, errors.INTERNAL_ERROR):
                raise
            self.failovers += 1
            REC.inc("mcp_failover")
            t = self.transports[server]
            if hasattr(t, "restart"):  # out-of-process worker: restart it in the background while the replica serves
                import asyncio
                asyncio.create_task(t.restart())
            return await rep.call(tool, arguments)

    async def status(self) -> dict:
        out = {}
        for n, t in self.transports.items():
            try:
                tools = await t.list_tools() if hasattr(t, "list_tools") else []
                out[n] = {"up": getattr(t, "up", True), "transport": t.kind, "tools": [x["name"] for x in tools]}
            except Exception as e:
                out[n] = {"up": False, "transport": t.kind, "error": type(e).__name__}
        return out
