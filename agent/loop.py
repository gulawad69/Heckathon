"""Bounded ReAct state machine (finaldesign.md §2.D, §4.2).
INIT -> TRIAGE -> REASONING -> TOOL_CALL -> OBSERVATION -> VERIFY -> DONE | HALTED | HALTED_BREAKER | PENDING_APPROVAL.
Hard limits enforced HERE (not by the LLM): max_iterations=5, per-step timeout=min(120 ms, remaining) (D5),
total_loop_budget=450 ms, no recursive self-calls, breaker on (tool, args_hash)."""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from agent import errors, llm as L
from agent.config import Settings
from agent.harness import Identity, ToolExecutionHarness
from agent.memory import SessionWindow, compress_observation, make_anchor, count_tokens
from agent.subagents.execution import ExecutionSubagent
from agent.subagents.retrieval import RetrievalSubagent
from agent.subagents.verification import VerificationSubagent
from agent.telemetry import REC, span
from agent.triage import classify


@dataclass
class AgentState:
    ident: Identity
    trace_id: str
    query: str                 # already PII-redacted
    pii_found: list = field(default_factory=list)
    state: str = "INIT"
    steps: list = field(default_factory=list)   # trace for the workbench accordion
    triage: object = None
    status: str = "ANSWERED"
    answer: str | None = None
    citations: list = field(default_factory=list)
    faithfulness: float | None = None
    approval_id: str | None = None
    remediation: str | None = None
    degraded: bool = False
    chunks: list = field(default_factory=list)
    prompt_tokens: int = 0
    raw_tokens: int = 0
    early_abstain: bool = False
    abstain_path: str | None = None
    cacheable_shared: bool = False
    subgraph_text: str = ""
    memory_text: str = ""
    observation: str = ""
    unavailable: bool = False


class AgentLoop:
    def __init__(self, harness: ToolExecutionHarness, llm, settings: Settings, window: SessionWindow):
        self.h, self.llm, self.s, self.window = harness, llm, settings, window
        self.retrieval = RetrievalSubagent(harness, settings)
        self.execution = ExecutionSubagent(harness)
        self.verifier = VerificationSubagent()

    # ---- helpers ----
    @staticmethod
    def _step(st, state, action, t0, **kw):
        st.state = state
        st.steps.append({"state": state, "action": action, "ms": round((time.perf_counter() - t0) * 1000, 2), **kw})

    def _abstain(self, st, path: str, early=False):
        st.status, st.answer, st.citations, st.faithfulness = "ABSTAINED", L.ABSTAIN_TEMPLATE.format(trace_id=st.trace_id), [], None
        st.abstain_path, st.early_abstain, st.state = path, early, "DONE"

    async def run(self, st: AgentState) -> AgentState:
        t0 = time.perf_counter()
        remaining = lambda: self.s.total_loop_budget_ms - (time.perf_counter() - t0) * 1000
        # ---- TRIAGE (reserved pool: never starved by a slow reranker) ----
        ts = time.perf_counter()
        async with self.h.pools["reserved"]:
            with span("triage.classify", st.trace_id):
                tri = classify(st.query)
        st.triage = tri
        self._step(st, "TRIAGE", f"route={tri.route} intent={tri.intent} risk={tri.risk_tier}", ts)
        route = tri.route
        if tri.risk_tier >= 3 and route not in ("TRANSACTION_EXEC", "REJECT_OOS"):
            route = "REJECT_OOS"  # tier-3 never rides another route; fail closed
        if route == "REJECT_OOS":
            text = {"emergency": L.EMERGENCY_TEXT, "clinical_action": L.CLINICAL_TEXT}.get(tri.intent, L.OOS_TEXT)
            st.status, st.answer, st.state = "REFUSED", text.format(trace_id=st.trace_id), "DONE"
            st.remediation = {"emergency": "Contact emergency services.", "clinical_action": "Contact the pharmacist on duty."}.get(tri.intent)
            return st
        if route == "DIRECT_ANSWER":
            st.answer = ("Hello. I can answer questions about our pharmacy formulary, medicine safety notes and SOPs, with citations, "
                         "and submit batch-lot state changes for pharmacist approval. I never prescribe or dispense.")
            st.state = "DONE"
            return st
        if route == "TRANSACTION_EXEC":
            return await self._transaction(st, remaining)
        return await self._rag(st, remaining)

    async def _transaction(self, st, remaining):
        s0 = time.perf_counter()
        with span("agent.react.step", st.trace_id, n=1):
            try:
                st.state = "TOOL_CALL"
                res = await self.execution.run(st.ident, st.query, remaining())
                st.status, st.approval_id, st.state = "PENDING_APPROVAL", res["approval_id"], "PENDING_APPROVAL"
                st.answer = (f"Your request was recorded and is PENDING_APPROVAL (approval id {res['approval_id']}). "
                             "A pharmacist approver must approve it before anything changes.")
            except errors.McpError as e:
                self._fail(st, e)
        self._step(st, st.state, f"workflow_transition -> {st.status}", s0)
        return st

    def _fail(self, st, e: errors.McpError):
        if e.code == errors.BREAKER_OPEN:
            st.status, st.state, st.remediation = "HALTED_BREAKER", "HALTED_BREAKER", e.message
            st.answer = None
        elif e.code in (errors.TIMEOUT, errors.INTERNAL_ERROR):
            self._abstain(st, "tool_unavailable")
            st.remediation = "A required service was unavailable. Please retry shortly."
            st.state = "HALTED"
        else:
            st.status, st.state, st.answer = "REFUSED", "HALTED", f"I couldn't complete that request ({e.message})."
            st.remediation = e.message

    async def _rag(self, st, remaining):
        anchor = make_anchor(st.query)  # immutable, index 0 of every prompt
        strict = self.s.strict_extractive
        # ---- dual memory READ path (Gate 5): verified facts (Postgres-equivalent) + last 3 turns (hot RAM) ----
        try:
            fr = await self.h.call("orchestrator", "session_facts_get", {"limit": 5}, st.ident, remaining())
            facts = [f["text"] for f in fr.get("facts", [])]
        except errors.McpError:
            facts = []
        turns = self.window.get(st.ident.tenant_id, str(st.ident.session_id))
        st.memory_text = ""
        if facts or turns:
            st.memory_text = "Verified facts: " + " | ".join(facts) + " Recent turns: " + " | ".join(t.get("q", "") for t in turns)
        for n in range(1, self.s.max_iterations + 1):
            if remaining() <= 0:
                st.state, st.status = "HALTED", "ABSTAINED"
                self._abstain(st, "budget"); st.state = "HALTED"
                st.remediation = "Time budget exhausted."
                return st
            s0 = time.perf_counter()
            with span("agent.react.step", st.trace_id, n=n):
                if not st.chunks:  # TOOL_CALL (only once: never repeat the same call)
                    st.state = "TOOL_CALL"
                    try:
                        ev = await self.retrieval.run(st.ident, st.query, remaining())
                    except errors.McpError as e:
                        self._fail(st, e)
                        self._step(st, st.state, f"retrieval failed {e.code}", s0)
                        return st
                    out = ev["out"]
                    st.degraded = out.degraded
                    st.state = "OBSERVATION"
                    if out.abstain:
                        self._abstain(st, "early" if out.early_abstain else "rerank", early=out.early_abstain)
                        self._step(st, "DONE", f"abstain({st.abstain_path}) top={out.top_score:.2f}", s0)
                        return st
                    st.chunks = ev["chunks"]
                    sub = ev["subgraph"]
                    obs = compress_observation({"chunks": [c.text for c in st.chunks]}, 180)
                    st.observation = obs
                    in_chunks = {c.chunk_id.split("#")[0] for c in st.chunks}
                    extra = [n for n in (sub.nodes if sub else []) if n.id not in in_chunks][:2]  # pruned subgraph adds linked safety notes only
                    st.subgraph_text = " ".join(n.text for n in extra)
                    st.raw_tokens = out.naive_dump_tokens + count_tokens(anchor.render())  # naive RAG prompt: un-pruned top-10 dump
                    st.prompt_tokens = count_tokens(anchor.render()) + count_tokens(" ".join(c.text for c in st.chunks)) + count_tokens(st.subgraph_text)
                    if not st.chunks:  # every chunk dropped as injected
                        self._abstain(st, "injected_context")
                        self._step(st, "DONE", "all chunks dropped (indirect injection)", s0)
                        return st
                    if st.degraded:
                        strict = True  # reranker timeout => extractive-quote-only, flagged degraded (FMEA #2)
                        top = st.chunks[0]
                        if L.overlap(st.query, top.text) < 0.3:  # calibrated RRF floor
                            self._abstain(st, "degraded_floor")
                            self._step(st, "DONE", "degraded abstain", s0)
                            return st
                # REASONING -> generate
                st.state = "REASONING"
                ctx_text = " ".join(x for x in (st.subgraph_text, st.memory_text, ("Previous observation (compressed): " + st.observation) if n > 1 else "") if x)
                try:
                    async with self.h.pools["llm"]:
                        with span("llm.generate", st.trace_id):
                            draft = await self.llm.generate(anchor, st.chunks, ctx_text, strict)
                except L.LLMUnavailable:
                    st.unavailable = True
                    self._abstain(st, "llm_unavailable"); st.state = "HALTED"
                    return st
                st.state = "VERIFY"
                v = self.verifier.check(draft, st.chunks)
                self._step(st, "VERIFY", f"grounded={v.grounded} faithfulness={v.faithfulness:.2f} strict={strict}", s0)
                if v.grounded:
                    st.answer, st.faithfulness, st.state = draft, v.faithfulness, "DONE"
                    st.citations = sorted({c.chunk_id for c in st.chunks if f"[{c.chunk_id}]" in draft})
                    st.answer = f"{draft}\n\n{L.SAFETY_FOOTER}"
                    return st
                if strict:  # even the strict extractive draft failed => deterministic abstention
                    self._abstain(st, "verification_veto")
                    return st
                strict = True  # need more evidence/precision: next step regenerates in strict extractive mode
        st.state = "HALTED"
        self._abstain(st, "max_iterations"); st.state = "HALTED"
        return st
