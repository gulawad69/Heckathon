"""Execution Subagent: schema validation + workflow transitions via mcp-state-store only (no RAG/web).
Tier 3 => request only: status PENDING_APPROVAL, never executed here."""
from __future__ import annotations

import re

from agent import errors
from agent.memory import normalize_entities

VERBS = {"quarantine": ("AVAILABLE", "QUARANTINED"), "hold": ("AVAILABLE", "QUARANTINED"),
         "release": ("QUARANTINED", "RELEASED"), "recall": ("QUARANTINED", "RECALLED")}


class ExecutionSubagent:
    name = "execution"

    def __init__(self, harness):
        self.h = harness

    @staticmethod
    def parse(query: str) -> dict | None:
        lots = [e for e in normalize_entities(query) if e.startswith("LOT-")]
        v = re.search(r"\b(quarantine|release|recall|hold)\b", query, re.I)
        if len(lots) != 1 or not v:  # ambiguous => -32602, never guess
            return None
        fr, to = VERBS[v.group(1).lower()]
        m = re.search(r"\bfrom\s+(available|quarantined|released|recalled)\b", query, re.I)
        if m:
            fr = m.group(1).upper()
        return {"lot": lots[0], "from_state": fr, "to_state": to, "reason": f"requested via assistant: {v.group(1).lower()}"}

    async def run(self, ident, query: str, remaining_ms: float) -> dict:
        args = self.parse(query)
        if args is None:
            raise errors.McpError(errors.INVALID_PARAMS, "could not identify exactly one LOT and one action")
        return await self.h.call("execution", "workflow_transition", args, ident, remaining_ms)
