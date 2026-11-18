"""Shared MCP server core: JSON-RPC 2.0 envelope, tool registry, strict validation, tier check.
Every server exposes `dispatch(raw_json) -> raw_json` (used by the in-process transport and by the stdio/HTTP
wrappers via FastMCP) so the contract is identical across transports."""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any, Callable

from pydantic import BaseModel, ValidationError

from agent import errors
from agent.models import TIER_RANK


@dataclass
class Tool:
    name: str
    model: type[BaseModel]
    handler: Callable[..., Any]
    tier: str  # tool's required tier ("system" for audit_append)
    timeout_ms: float | None = None


class McpServerBase:
    name = "server"

    def __init__(self):
        self.tools: dict[str, Tool] = {}

    def register(self, name, model, handler, tier, timeout_ms=None):
        self.tools[name] = Tool(name, model, handler, tier, timeout_ms)

    @staticmethod
    def _err(id_, code, msg="", data=None) -> str:
        return json.dumps({"jsonrpc": "2.0", "id": id_, "error": errors.McpError(code, msg, data).to_json()})

    async def dispatch(self, raw: str) -> str:
        try:
            req = json.loads(raw)
        except Exception:
            return self._err(None, errors.PARSE_ERROR)
        if not isinstance(req, dict) or req.get("jsonrpc") != "2.0" or "method" not in req:
            return self._err(req.get("id") if isinstance(req, dict) else None, errors.INVALID_REQUEST)
        id_, method, params = req.get("id"), req["method"], req.get("params") or {}
        if method == "tools/list":
            return json.dumps({"jsonrpc": "2.0", "id": id_, "result": {"tools": [
                {"name": t.name, "tier": t.tier} for t in self.tools.values()]}})
        if method != "tools/call":
            return self._err(id_, errors.METHOD_NOT_FOUND, method)
        tool = self.tools.get(params.get("name", ""))
        if not tool:
            return self._err(id_, errors.METHOD_NOT_FOUND, str(params.get("name")))
        try:
            args = tool.model.model_validate_json(json.dumps(params.get("arguments", {})))
        except ValidationError as e:
            return self._err(id_, errors.INVALID_PARAMS, "validation failed", {"errors": [
                {"loc": list(x["loc"]), "type": x["type"]} for x in e.errors()][:5]})
        ctx = getattr(args, "ctx", None)
        if ctx is not None and tool.tier != "system" and TIER_RANK[ctx.tier] < TIER_RANK[tool.tier]:
            return self._err(id_, errors.RBAC_DENIED, "call tier below tool tier")
        try:
            coro = tool.handler(args)
            if asyncio.iscoroutine(coro):
                coro = asyncio.wait_for(coro, tool.timeout_ms / 1000) if tool.timeout_ms else coro
                out = await coro
            else:
                out = coro
            body = out.model_dump(mode="json") if isinstance(out, BaseModel) else out
            return json.dumps({"jsonrpc": "2.0", "id": id_, "result": body})
        except errors.McpError as e:
            return self._err(id_, e.code, e.message, e.data)
        except asyncio.TimeoutError:
            return self._err(id_, errors.TIMEOUT, "tool budget exceeded")
        except Exception as e:  # never leak internals
            return self._err(id_, errors.INTERNAL_ERROR, type(e).__name__)


def make_app(server: McpServerBase):
    """Official MCP SDK server exposing every registered tool (works with mcp 1.x FastMCP and 2.x MCPServer)."""
    try:
        from mcp.server.mcpserver import MCPServer as App
    except ImportError:
        from mcp.server.fastmcp import FastMCP as App
    app = App(server.name)
    for tname in server.tools:
        def make(n):
            async def _tool(arguments: dict) -> dict:
                raw = await server.dispatch(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                                        "params": {"name": n, "arguments": arguments}}))
                return json.loads(raw)
            _tool.__name__ = n
            return _tool
        app.tool(name=tname)(make(tname))
    return app


def serve_stdio(server: McpServerBase):  # pragma: no cover - exercised by tests/test_mcp.py::test_stdio_transport
    make_app(server).run(transport="stdio")
