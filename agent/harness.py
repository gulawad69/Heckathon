"""ToolExecutionHarness (finaldesign.md §4.1). Every MCP call, in order:
(1) Pydantic v2 strict validation -> (2) deterministic regex auto-repair -> (3) RBAC tier check -> (4) UUIDv4
idempotency -> circuit breaker -> dispatch (bulkhead pool, per-step timeout = min(120 ms, remaining))."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass

from pydantic import ValidationError

from agent import errors
from agent.config import Settings
from agent.mcp_host import McpHost
from agent.models import (TIER_RANK, ApprovalDecideIn, AuditAppendIn, CacheGetIn, CachePutIn, HybridRetrieveIn,
                          ResolveSubgraphIn, SessionFactsGetIn, SessionFactsPutIn, WorkflowTransitionIn)
from agent.telemetry import REC
from plugins.circuit_breaker import CircuitBreaker

# tool -> (server, model, required tier, pool, allowed roles)
TOOLS = {
    "hybrid_retrieve":    ("hybrid-rag", HybridRetrieveIn, "TIER_1_READ", "rag", {"END_USER", "APPROVER"}),
    "resolve_subgraph":   ("graphify-vault", ResolveSubgraphIn, "TIER_1_READ", "rag", {"END_USER", "APPROVER"}),
    "cache_get":          ("state-store", CacheGetIn, "TIER_1_READ", "db", {"END_USER", "APPROVER"}),
    "cache_put":          ("state-store", CachePutIn, "TIER_1_READ", "db", {"END_USER", "APPROVER"}),
    "session_facts_get":  ("state-store", SessionFactsGetIn, "TIER_1_READ", "db", {"END_USER", "APPROVER"}),
    "session_facts_put":  ("state-store", SessionFactsPutIn, "TIER_2_COMPUTE", "db", {"END_USER", "APPROVER"}),
    "audit_append":       ("state-store", AuditAppendIn, "TIER_1_READ", "db", {"END_USER", "APPROVER", "SYSTEM"}),
    "workflow_transition": ("state-store", WorkflowTransitionIn, "TIER_3_MUTATE", "db", {"END_USER", "APPROVER"}),  # END_USER may only REQUEST
    "approval_decide":    ("state-store", ApprovalDecideIn, "TIER_3_MUTATE", "db", {"APPROVER"}),
}
SUBAGENT_TOOLS = {  # isolation rules (Gate 7)
    "retrieval": {"hybrid_retrieve", "resolve_subgraph"},
    "execution": {"workflow_transition", "session_facts_get", "session_facts_put"},
    "verification": {"audit_append"},
    "orchestrator": {"cache_get", "cache_put", "audit_append", "session_facts_get", "session_facts_put"},
    "approver_api": {"approval_decide", "audit_append"},
}

_SKU_RX = re.compile(r"^\s*(?:sku)?[\s_\-]*(\d{1,3})\s*$", re.I)
_LOT_RX = re.compile(r"^\s*(?:lot)?[\s_\-]*(\d{1,5})\s*$", re.I)


def repair_value(key: str, v):
    """Deterministic, logged, never guesses: SKU99/sku 99 -> SKU-099; lot 44/LOT44 -> LOT-00044. Ambiguous => None."""
    if not isinstance(v, str):
        return v
    if key in ("sku",) or key.endswith("_sku"):
        m = _SKU_RX.match(v)
        return f"SKU-{int(m.group(1)):03d}" if m else None
    if key in ("lot",) or key.endswith("_lot"):
        m = _LOT_RX.match(v)
        return f"LOT-{int(m.group(1)):05d}" if m else None
    return v.strip()


def repair_args(args: dict) -> tuple[dict, list[str]]:
    out, log = {}, []
    for k, v in args.items():
        if isinstance(v, str):
            nv = repair_value(k.lower(), v)
            if nv != v:
                log.append(f"{k}: {v!r} -> {nv!r}")
            out[k] = nv if nv is not None else v
        else:
            out[k] = v
    return out, log


@dataclass
class Identity:
    tenant_id: str
    user_id: str
    session_id: uuid.UUID
    role: str
    traceparent: str


def stable_uuid4(*parts: str) -> uuid.UUID:
    b = bytearray(hashlib.sha256("|".join(parts).encode()).digest()[:16])
    b[6] = (b[6] & 0x0F) | 0x40
    b[8] = (b[8] & 0x3F) | 0x80
    return uuid.UUID(bytes=bytes(b))


class ToolExecutionHarness:
    def __init__(self, host: McpHost, breaker: CircuitBreaker, settings: Settings | None = None):
        self.host, self.breaker, self.s = host, breaker, settings or Settings()
        self.pools = {"rag": asyncio.Semaphore(self.s.rag_pool), "db": asyncio.Semaphore(self.s.db_pool),
                      "llm": asyncio.Semaphore(self.s.llm_pool), "reserved": asyncio.Semaphore(self.s.reserved_pool)}
        self.repair_log: list[str] = []

    async def call(self, subagent: str, tool: str, args: dict, ident: Identity, remaining_ms: float | None = None,
                   idem_key: uuid.UUID | None = None) -> dict:
        spec = TOOLS.get(tool)
        if spec is None:
            raise errors.McpError(errors.METHOD_NOT_FOUND, tool)
        server, model, tier, pool, roles = spec
        # --- (3 pre-check) subagent isolation: wrong subagent => RBAC_DENIED (fail closed) ---
        if tool not in SUBAGENT_TOOLS.get(subagent, set()):
            REC.inc("rbac_denied")
            raise errors.McpError(errors.RBAC_DENIED, f"{subagent} may not call {tool}")
        if "ctx" in args or ("tenant_id" in args and args["tenant_id"] != ident.tenant_id):
            REC.inc("rbac_denied")
            raise errors.McpError(errors.RBAC_DENIED, "caller-supplied context/tenant rejected")
        if self.breaker.is_open(ident.tenant_id, str(ident.session_id), tool, args):
            raise errors.McpError(errors.BREAKER_OPEN, self.breaker.remediation(tool, args))
        ik = idem_key or (stable_uuid4(ident.tenant_id, str(ident.session_id), tool, json.dumps(args, sort_keys=True, default=str))
                          if tier == "TIER_3_MUTATE" else uuid.uuid4())
        ctx = {"tenant_id": ident.tenant_id, "user_id": ident.user_id, "session_id": str(ident.session_id),
               "idempotency_key": str(ik), "traceparent": ident.traceparent, "tier": tier}
        # --- (1) strict validation, first pass ---
        full = {"ctx": ctx, **args}
        REC.inc("schema_total")
        try:
            model.model_validate_json(json.dumps(full))
            REC.inc("schema_first_pass")
        except ValidationError:
            # --- (2) deterministic auto-repair, then re-validate ---
            REC.inc("repair_attempt")
            fixed, log = repair_args(args)
            self.repair_log += log
            try:
                model.model_validate_json(json.dumps({"ctx": ctx, **fixed}))
                REC.inc("repair_success")
                full = {"ctx": ctx, **fixed}
            except ValidationError as e:
                n = self.breaker.record_failure(ident.tenant_id, str(ident.session_id), tool, args)
                code = errors.BREAKER_OPEN if n >= self.breaker.max_identical else errors.INVALID_PARAMS
                msg = self.breaker.remediation(tool, args) if code == errors.BREAKER_OPEN else "invalid arguments"
                raise errors.McpError(code, msg, {"errors": [{"loc": list(x["loc"]), "type": x["type"]} for x in e.errors()][:3]})
        # --- (3) RBAC: role may use tool; role rights vs tier ---
        if ident.role not in roles:
            REC.inc("rbac_denied")
            raise errors.McpError(errors.RBAC_DENIED, f"role {ident.role} may not call {tool}")
        if tier == "TIER_3_MUTATE" and ident.role == "END_USER" and tool != "workflow_transition":
            raise errors.McpError(errors.RBAC_DENIED, "end users may only request T3 actions")
        # --- (4) idempotency enforced via UUIDv4 in ctx (schema) + state-store replay table ---
        # --- dispatch under bulkhead with per-step timeout ---
        step = self.s.step_timeout_ms if remaining_ms is None else min(self.s.step_timeout_ms, remaining_ms)
        if step <= 0:
            raise errors.McpError(errors.TIMEOUT, "loop budget exhausted")
        try:
            async with self.pools[pool]:
                res = await asyncio.wait_for(self.host.call(server, tool, full), timeout=step / 1000)
        except asyncio.TimeoutError:
            n = self.breaker.record_failure(ident.tenant_id, str(ident.session_id), tool, args)
            REC.inc("mcp_timeout")
            raise errors.McpError(errors.BREAKER_OPEN if n >= self.breaker.max_identical else errors.TIMEOUT, "step timeout")
        except errors.McpError as e:
            if e.code not in (errors.ABSTAIN, errors.PENDING_APPROVAL, errors.IDEMPOTENT_REPLAY):
                n = self.breaker.record_failure(ident.tenant_id, str(ident.session_id), tool, args)
                if n >= self.breaker.max_identical:
                    raise errors.McpError(errors.BREAKER_OPEN, self.breaker.remediation(tool, args))
            raise
        self.breaker.record_success(ident.tenant_id, str(ident.session_id), tool, args)
        if isinstance(res, dict) and res.get("replay"):
            REC.inc("idempotent_replay")
        return res
