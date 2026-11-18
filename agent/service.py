"""Request pipeline: pre plugins (PII -> injection, fail-closed) -> tenant-scoped cache -> bounded loop -> post plugins.
e2e = request received -> last byte of non-streamed response (D9)."""
from __future__ import annotations

import asyncio
import hashlib
import json
import random
import re
import time
import uuid
from collections import OrderedDict

from agent import errors, llm as L
from agent.config import Settings
from agent.harness import Identity, ToolExecutionHarness
from agent.loop import AgentLoop, AgentState
from agent.mcp_host import InProcessTransport, McpHost, verify_manifests
from agent.memory import SessionWindow
from agent.models import QueryIn, QueryOut
from agent.telemetry import REC, new_traceparent, span
import numpy as np
from mcp_servers.graphify_server import GraphifyServer
from mcp_servers.rag_server import RagServer
from mcp_servers.state_server import StateServer
from plugins import circuit_breaker, ragas_auditor, otel_exporter, pii_redactor, injection_shield
from plugins.base import PluginRegistry

SHARED = "*"


def norm_query(q: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s\[\]\-]", "", q.lower())).strip()


def cache_key(tenant: str, user: str, q: str) -> str:
    return hashlib.sha256(f"{tenant}:{user}:{norm_query(q)}".encode()).hexdigest()


class LocalLRU:
    """Fallback when the cache backend is down (FMEA #1): tenant-keyed, 60 s, never unscoped keys."""

    def __init__(self, cap=5000, ttl=60):
        self.d: OrderedDict = OrderedDict(); self.cap, self.ttl = cap, ttl

    def get(self, tenant, h):
        v = self.d.get((tenant, h))
        return v[1] if v and v[0] > time.time() else None

    def put(self, tenant, h, val):
        self.d[(tenant, h)] = (time.time() + self.ttl, val)
        while len(self.d) > self.cap:
            self.d.popitem(last=False)


class AgentService:
    def __init__(self, settings: Settings | None = None, db_path: str = ":memory:", rag: RagServer | None = None,
                 verify: bool = True, llm=None, transports: dict | None = None):
        self.s = settings or Settings()
        if verify:
            verify_manifests()
        self.rag = rag or RagServer()
        self.graph = GraphifyServer()
        self.state = StateServer(db_path)
        self.transports = transports or {"hybrid-rag": InProcessTransport(self.rag), "graphify-vault": InProcessTransport(self.graph),
                                         "state-store": InProcessTransport(self.state)}
        replicas = {"hybrid-rag": InProcessTransport(RagServer(share_from=self.rag))} if transports is None else {}  # FMEA #3 read-replica
        self.host = McpHost(self.transports, replicas)
        self.plugins = PluginRegistry().load([pii_redactor.impl, injection_shield.impl, circuit_breaker.impl, ragas_auditor.impl, otel_exporter.impl])
        self.breaker = self.plugins.plugins["circuit_breaker"]
        self.ragas = self.plugins.plugins["ragas_auditor"]
        self.harness = ToolExecutionHarness(self.host, self.breaker, self.s)
        self.window = SessionWindow()
        self.llm = llm or L.make_llm()
        self.loop = AgentLoop(self.harness, self.llm, self.s, self.window)
        self.local_lru = LocalLRU(ttl=self.s.local_lru_ttl_s)
        self.cache_down = False  # fault injection (FMEA #1)
        self.inflight: dict[str, asyncio.Future] = {}
        self.bg: set = set()
        self.cache_stats = {"hit": 0, "miss": 0}
        self.sem: dict[str, list] = {}  # tenant -> [(unit embedding, shared key hash)]; only tenant-shared formulary entries
        self.incident_log: list = []
        self.breach_since: float | None = None
        from collections import deque
        self.last_states: deque = deque(maxlen=50)  # for the workbench ReAct trace accordion

    # ---- helpers ----
    def _ident(self, q: QueryIn, sid, traceparent) -> Identity:
        return Identity(q.tenant_id, q.user_id, sid, self.state.repo.role(q.tenant_id, q.user_id), traceparent)

    def _bg(self, coro):
        t = asyncio.create_task(coro); self.bg.add(t); t.add_done_callback(self.bg.discard)

    async def drain(self):
        if self.bg:
            await asyncio.gather(*list(self.bg), return_exceptions=True)
        self.state.drain_outbox()

    async def _audit(self, ident, kind, **detail):
        try:
            await self.harness.call("orchestrator", "audit_append", {"event": {"kind": kind, "detail": detail}}, ident)
        except Exception:  # audit append is retried via durable outbox, never dropped (FMEA #6)
            self.state.repo.q("INSERT INTO outbox(tenant_id, payload) VALUES(?,?)", (ident.tenant_id, json.dumps({"kind": kind, "detail": detail})))

    async def _cache_get(self, ident, h):
        if self.cache_down:
            return self.local_lru.get(ident.tenant_id, h)
        try:
            r = await self.harness.call("orchestrator", "cache_get", {"key_hash": h}, ident, 5.0 + 20)
            return r.get("value") if r.get("hit") else None
        except errors.McpError:
            return self.local_lru.get(ident.tenant_id, h)

    async def _cache_put(self, ident, h, value: str):
        ttl = self.s.cache_ttl_s + int(random.uniform(-self.s.cache_jitter_s, self.s.cache_jitter_s))  # 300 +/- 45 s
        self.local_lru.put(ident.tenant_id, h, value)
        if self.cache_down:
            return
        try:
            await self.harness.call("orchestrator", "cache_put", {"key_hash": h, "value": value, "ttl_s": ttl}, ident)
        except errors.McpError:
            pass

    # ---- semantic cache (ADR-3; only active in the latency-incident mode, cosine >= 0.92, inside ONE tenant namespace) ----
    async def _embed(self, text: str):
        return (await asyncio.get_running_loop().run_in_executor(None, self.rag.models.encode, [text]))[0]

    async def _sem_register(self, tenant: str, query: str, key_hash: str):
        v = await self._embed(norm_query(query))
        lst = self.sem.setdefault(tenant, [])
        lst.append((v, key_hash))
        del lst[:-2000]

    async def _sem_lookup(self, ident, query: str):
        lst = self.sem.get(ident.tenant_id)
        if not lst:
            return None
        v = await self._embed(norm_query(query))
        best, bk = max(((float(np.dot(v, e)), k) for e, k in lst), key=lambda x: x[0])
        if best >= self.s.semantic_cosine:
            hit = await self._cache_get(ident, bk)
            if hit: REC.inc("semantic_hit")
            return hit
        return None

    # ---- housekeeping + incident automation (Gate 10-11, FMEA #6/#10) ----
    async def housekeeping(self):
        n_out = self.state.drain_outbox()
        tenants = [r[0] for r in self.state.repo.q("SELECT DISTINCT tenant_id FROM approvals")]
        esc = sum(self.state.escalate_stale(t) for t in tenants)
        for t in tenants: self.state.expire_stale(t)
        return {"outbox_replayed": n_out, "escalated": esc}

    def incident_tick(self, now: float | None = None) -> list:
        """Alert rules: p95 e2e > 550 ms sustained 30 s (60 s window) => latency runbook; faithfulness < 0.90 over 5 min => hallucination runbook."""
        now = now or time.time()
        p95, n = REC.window_pct(95, 60)
        if p95 is not None and n >= 20 and p95 > 550:
            self.breach_since = self.breach_since or now
        else:
            self.breach_since = None
        acts = self.evaluate_incidents(p95 if (self.breach_since and now - self.breach_since >= 30) else None)
        for a in acts: self.incident_log.append((now, a))
        return acts

    def reset_incidents(self):
        d = Settings()
        self.s.semantic_cache, self.s.bm25_top_k, self.s.dense_top_k = False, d.bm25_top_k, d.dense_top_k
        self.s.score_threshold, self.s.strict_extractive = d.score_threshold, d.strict_extractive
        self.ragas.window.clear(); self.breach_since = None

    def purge_cache_namespace(self, tenant: str) -> int:
        """Stampede / key-collision runbook: purge ONE tenant's namespace (state-store, local LRU, semantic index)."""
        n = self.state.purge_namespace(tenant)
        for k in [k for k in self.local_lru.d if k[0] == tenant]: self.local_lru.d.pop(k, None)
        self.sem.pop(tenant, None)
        return n

    # ---- main ----
    async def handle(self, q: QueryIn) -> QueryOut:
        t_start = time.perf_counter()
        tid, tp = new_traceparent()
        sid = q.session_id or uuid.uuid4()
        ident = self._ident(q, sid, tp)
        st = AgentState(ident=ident, trace_id=tid, query=q.query)
        # --- pre plugins (fail-closed) ---
        with span("hook.pre", tid):
            res = await self.plugins.run_stage("PRE_EXECUTION", st)
        if res.action == "BLOCK":
            REC.inc(f"block_{res.code}")
            self._bg(self._audit(ident, "REFUSED_PRE", code=res.code, reason=res.reason))
            code_txt = {errors.PII_BLOCK: "PII_BLOCK", errors.INJECTION_BLOCKED: "INJECTION_BLOCKED"}.get(res.code, "BLOCKED")
            return QueryOut(tenant_id=q.tenant_id, session_id=sid, trace_id=tid, status="REFUSED",
                            answer=f"I can't process that request ({code_txt}). (ref: {tid})",
                            remediation="Remove personal data or instructions that try to change my rules, then retry.")
        # --- cache (tenant-scoped; per-user key, then tenant-shared key for allow-listed non-personal queries) ---
        own, shared = cache_key(q.tenant_id, q.user_id, st.query), cache_key(q.tenant_id, SHARED, st.query)
        with span("cache.lookup", tid):
            hit = await self._cache_get(ident, shared) or await self._cache_get(ident, own)
            if not hit and self.s.semantic_cache:
                hit = await self._sem_lookup(ident, st.query)
        if hit:
            self.cache_stats["hit"] += 1
            d = json.loads(hit)
            REC.observe("e2e.cache_hit", (time.perf_counter() - t_start) * 1000)
            return QueryOut(tenant_id=q.tenant_id, session_id=sid, trace_id=tid, status="ANSWERED", answer=d["answer"],
                            citations=d["citations"], faithfulness=d["faithfulness"])
        self.cache_stats["miss"] += 1
        # --- single-flight on identical misses ---
        fl_key = f"{q.tenant_id}:{own}"
        if fl_key in self.inflight:
            await asyncio.shield(self.inflight[fl_key])
            hit = await self._cache_get(ident, shared) or await self._cache_get(ident, own)
            if hit:
                d = json.loads(hit)
                return QueryOut(tenant_id=q.tenant_id, session_id=sid, trace_id=tid, status="ANSWERED", answer=d["answer"],
                                citations=d["citations"], faithfulness=d["faithfulness"])
        fut = asyncio.get_running_loop().create_future(); self.inflight[fl_key] = fut
        try:
            st = await self.loop.run(st)
        finally:
            self.inflight.pop(fl_key, None); fut.set_result(None)
        # --- cache write: only verified, grounded, non-degraded answers (never abstentions/pending) ---
        if st.status == "ANSWERED" and st.faithfulness is not None and st.faithfulness >= self.s.min_faithfulness and not st.degraded:
            val = json.dumps({"answer": st.answer, "citations": st.citations, "faithfulness": st.faithfulness})
            key = shared if (st.triage.domain == "formulary" and not st.pii_found) else own
            self._bg(self._cache_put(ident, key, val))
            if key == shared:
                self._bg(self._sem_register(q.tenant_id, st.query, shared))
        self.last_states.append(st)
        if st.unavailable:
            raise errors.ServiceUnavailable(retry_after=1)
        out = QueryOut(tenant_id=q.tenant_id, session_id=sid, trace_id=tid, status=st.status, answer=st.answer, citations=st.citations,
                       faithfulness=st.faithfulness, approval_id=uuid.UUID(st.approval_id) if st.approval_id else None, remediation=st.remediation)
        e2e = (time.perf_counter() - t_start) * 1000
        REC.observe(f"e2e.{st.status}" + (f".{st.abstain_path}" if st.abstain_path else ""), e2e)
        REC.observe("e2e.all", e2e)
        REC.observe("agent.steps", len([s for s in st.steps if s["state"] in ("VERIFY", "DONE", "PENDING_APPROVAL", "HALTED_BREAKER")]) or 1)
        if st.prompt_tokens and st.raw_tokens:
            REC.observe("token_reduction", 1 - st.prompt_tokens / st.raw_tokens)
        # --- post plugins async + audit + window ---
        self.window.add(q.tenant_id, str(sid), {"q": st.query[:200], "q_hash": hashlib.sha256(st.query.encode()).hexdigest()[:12], "status": st.status})
        self._bg(self._post(st))
        self._bg(self._audit(ident, f"QUERY_{st.status}", route=st.triage.route if st.triage else None, path=st.abstain_path,
                             q_sha=hashlib.sha256(st.query.encode()).hexdigest()[:16], pii=sorted(set(st.pii_found))))
        return out

    async def _post(self, st: AgentState):
        await self.plugins.run_stage("POST_EXECUTION", st)
        if st.status == "ANSWERED" and st.faithfulness is not None and st.chunks:  # verified fact -> session_facts (T2)
            try:
                await self.harness.call("orchestrator", "session_facts_put", {"text": (st.answer or "")[:480].split("\n")[0],
                                        "evidence_chunk_ids": st.citations or [c.chunk_id for c in st.chunks[:1]]}, st.ident)
            except Exception:
                pass

    # ---- Approver API (T3 decide); identity supplied by the gateway ----
    async def decide(self, tenant_id: str, user_id: str, approval_id: uuid.UUID, decision: str, comment: str = "") -> dict:
        tid, tp = new_traceparent()
        ident = Identity(tenant_id, user_id, uuid.uuid4(), self.state.repo.role(tenant_id, user_id), tp)
        return await self.harness.call("approver_api", "approval_decide", {"approval_id": str(approval_id), "decision": decision, "comment": comment[:300]}, ident)

    # ---- Incident runbooks (§10) ----
    def evaluate_incidents(self, p95_e2e_ms: float | None = None):
        actions = []
        if p95_e2e_ms and p95_e2e_ms > 550 and not self.s.semantic_cache:
            self.s.semantic_cache, self.s.bm25_top_k, self.s.dense_top_k = True, 25, 25
            actions.append("latency: semantic cache cosine>=0.92, top_k 50->25")
        f = self.ragas.window_faithfulness()
        if f is not None and f < 0.90 and self.s.score_threshold < 0.55:
            self.s.score_threshold, self.s.strict_extractive = 0.55, True
            actions.append("hallucination: cutoff 0.42->0.55, strict extractive")
        return actions
