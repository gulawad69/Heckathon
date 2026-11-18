"""Plugin interface + registry (finaldesign.md §3.7). Pluggy hookspec supplies plugin instances; lower priority
runs first; fail_closed + plugin error/timeout => request refused. Security plugins cannot be hot-disabled."""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import pluggy

from agent import errors

hookspec = pluggy.HookspecMarker("pharma_agent")
hookimpl = pluggy.HookimplMarker("pharma_agent")
LOCKED = {"pii_redactor", "injection_shield"}


@dataclass
class HookResult:
    action: Literal["ALLOW", "MODIFY", "BLOCK"] = "ALLOW"
    query: str | None = None
    code: int | None = None
    reason: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


class Plugin:
    name = "plugin"
    stage: str = "PRE_EXECUTION"
    priority = 100
    fail_closed = False
    enabled = True
    timeout_ms = 15.0

    async def run(self, state) -> HookResult:  # pragma: no cover
        return HookResult()


class Spec:
    @hookspec
    def register_plugins(self) -> list[Plugin]: ...


class PluginRegistry:
    def __init__(self, config_path: str | Path = "mcp_config.json"):
        self.pm = pluggy.PluginManager("pharma_agent")
        self.pm.add_hookspecs(Spec)
        self.config = json.loads(Path(config_path).read_text())["plugins"]
        self.plugins: dict[str, Plugin] = {}

    def load(self, modules: list) -> "PluginRegistry":
        for m in modules:
            self.pm.register(m)
        by_name = {c["name"]: c for c in self.config}
        for group in self.pm.hook.register_plugins():
            for p in group:
                c = by_name.get(p.name)
                if not c:
                    continue
                p.stage, p.priority = c["stage"], c.get("priority", 100)
                p.fail_closed = c.get("fail_closed", False)
                p.cfg = c
                self.plugins[p.name] = p
        return self

    def set_enabled(self, name: str, on: bool):
        if name in LOCKED and not on:
            raise PermissionError(f"{name} is a security plugin and cannot be disabled")
        self.plugins[name].enabled = on

    def stage(self, stage: str) -> list[Plugin]:
        return sorted((p for p in self.plugins.values() if p.stage == stage and p.enabled), key=lambda p: p.priority)

    async def run_stage(self, stage: str, state) -> HookResult:
        """Run plugins in priority order. Returns final HookResult (BLOCK short-circuits)."""
        last = HookResult()
        for p in self.stage(stage):
            try:
                res = await asyncio.wait_for(p.run(state), timeout=p.timeout_ms / 1000)
            except Exception as e:  # error or timeout
                if p.fail_closed:
                    code = errors.PII_BLOCK if p.name == "pii_redactor" else errors.INJECTION_BLOCKED if p.name == "injection_shield" else errors.INTERNAL_ERROR
                    return HookResult("BLOCK", code=code, reason=f"{p.name} failed closed: {type(e).__name__}")
                continue
            if res.action == "BLOCK":
                return res
            if res.action == "MODIFY" and res.query is not None:
                state.query = res.query
            last = res
        return HookResult("ALLOW")
