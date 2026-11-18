"""Retrieval Subagent: read-only; hybrid_retrieve || resolve_subgraph (overlapped), citation grounding.
Drops any retrieved chunk carrying an indirect prompt injection (never forwarded to the LLM)."""
from __future__ import annotations

import asyncio
import re

from agent import errors
from agent.models import Chunk, HybridRetrieveOut, ResolveSubgraphOut
from plugins.injection_shield import scan_chunk


class RetrievalSubagent:
    name = "retrieval"

    def __init__(self, harness, settings):
        self.h, self.s = harness, settings

    async def run(self, ident, query: str, remaining_ms: float) -> dict:
        query = re.sub(r"\[[A-Z_]+\]", " ", query).strip() or query  # redaction tokens carry no retrieval signal
        rag = self.h.call("retrieval", "hybrid_retrieve", {"query": query, "bm25_top_k": self.s.bm25_top_k,
                          "dense_top_k": self.s.dense_top_k, "score_threshold": self.s.score_threshold}, ident, remaining_ms)
        graph = self.h.call("retrieval", "resolve_subgraph", {"query": query}, ident, remaining_ms)
        r, g = await asyncio.gather(rag, graph, return_exceptions=True)
        if isinstance(r, errors.McpError) and r.code == errors.ABSTAIN:  # -32008: deterministic abstention carries the result
            r = r.data
        if isinstance(r, errors.McpError) and r.code == errors.BREAKER_OPEN:
            raise r
        if isinstance(r, BaseException):
            raise r if isinstance(r, errors.McpError) else errors.McpError(errors.INTERNAL_ERROR, type(r).__name__)
        out = HybridRetrieveOut.model_validate(r)
        sub = ResolveSubgraphOut.model_validate(g) if not isinstance(g, BaseException) else None  # graph is an optimizer, not required
        dropped = [c.chunk_id for c in out.chunks if scan_chunk(c.text)]
        chunks = [c for c in out.chunks if c.chunk_id not in dropped]
        return {"out": out, "chunks": chunks, "subgraph": sub, "dropped_injected": dropped}
