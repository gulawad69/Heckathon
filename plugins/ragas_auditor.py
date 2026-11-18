"""ragas_auditor (POST_EXECUTION, async, off the critical path). `ragas` is not installed here, so the triad is
computed with local PROXIES (labelled as such in the scorecard): faithfulness = Verification entailment fraction,
answer_relevancy = token/embedding cosine(query, answer), context_entity_recall = share of query entities/numbers
present in retrieved chunks. Rolling 5-min window drives the Hallucination-Surge runbook (cutoff 0.42 -> 0.55 +
strict extractive)."""
from __future__ import annotations

import re
import time
from collections import deque

from agent.memory import token_cosine
from agent.telemetry import REC, span
from plugins.base import Plugin, hookimpl


class RagasAuditor(Plugin):
    name = "ragas_auditor"
    timeout_ms = 1000.0

    def __init__(self):
        self.window: deque = deque()  # (ts, faithfulness)

    def score(self, query: str, answer: str, chunks, faithfulness: float | None) -> dict:
        ctx = " ".join(c.text for c in chunks)
        ents = set(re.findall(r"[A-Za-z][A-Za-z\-]{4,}|\d+(?:\.\d+)?", query))
        ents = {e.lower() for e in ents if e.lower() not in {"which", "where", "should", "listed", "adults", "options", "about"}}
        rec = (sum(e in ctx.lower() for e in ents) / len(ents)) if ents else 1.0
        return {"faithfulness": faithfulness if faithfulness is not None else 0.0,
                "answer_relevancy": token_cosine(query, answer), "context_entity_recall": rec}

    async def run(self, state) -> "HookResult":
        from plugins.base import HookResult
        with span("hook.post.ragas", state.trace_id):
            m = self.score(state.query, state.answer or "", state.chunks, state.faithfulness)
        now = time.time()
        if state.status == "ANSWERED" and state.faithfulness is not None:
            self.window.append((now, m["faithfulness"]))
            REC.observe("ragas.faithfulness", m["faithfulness"]); REC.observe("ragas.answer_relevancy", m["answer_relevancy"])
            REC.observe("ragas.context_entity_recall", m["context_entity_recall"])
        while self.window and self.window[0][0] < now - 300:
            self.window.popleft()
        return HookResult("ALLOW", meta=m)

    def window_faithfulness(self) -> float | None:
        return sum(f for _, f in self.window) / len(self.window) if self.window else None


class _Impl:
    @hookimpl
    def register_plugins(self):
        return [RagasAuditor()]


impl = _Impl()
