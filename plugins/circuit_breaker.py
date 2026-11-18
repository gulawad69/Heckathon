"""circuit_breaker (TOOL_DISPATCH p10): idempotent breaker keyed by (tool_name, canonical_args_hash); >= 2 failures
=> open => HALTED_BREAKER with a structured remediation prompt (finaldesign.md §4.5)."""
from __future__ import annotations

import hashlib
import json

from plugins.base import Plugin, hookimpl


def args_hash(tool: str, args: dict) -> str:
    return hashlib.sha256((tool + json.dumps(args, sort_keys=True, default=str, separators=(",", ":"))).encode()).hexdigest()


class CircuitBreaker(Plugin):
    name = "circuit_breaker"
    max_identical = 2

    def __init__(self):
        self.fails: dict[tuple, int] = {}
        self.forced_open: set[str] = set()  # manual trip from the workbench (tool names)

    def key(self, tenant, session, tool, args): return (tenant, session, tool, args_hash(tool, args))

    def is_open(self, tenant, session, tool, args) -> bool:
        return tool in self.forced_open or self.fails.get(self.key(tenant, session, tool, args), 0) >= getattr(self, "cfg", {}).get("max_identical_calls", self.max_identical)

    def record_failure(self, tenant, session, tool, args) -> int:
        k = self.key(tenant, session, tool, args)
        self.fails[k] = self.fails.get(k, 0) + 1
        return self.fails[k]

    def record_success(self, tenant, session, tool, args):
        self.fails.pop(self.key(tenant, session, tool, args), None)

    def reset(self):
        self.fails.clear(); self.forced_open.clear()

    @staticmethod
    def remediation(tool: str, args: dict) -> str:
        return (f"The same request to '{tool}' failed twice, so I stopped to avoid a loop. Please check the identifiers you "
                f"provided (for example SKU-123 or LOT-12345) or contact the pharmacist on duty. (breaker: {args_hash(tool, args)[:8]})")


class _Impl:
    @hookimpl
    def register_plugins(self):
        return [CircuitBreaker()]


impl = _Impl()
