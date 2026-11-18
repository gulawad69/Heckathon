"""3-Stage RAG Lab helper for the workbench: recomputes every stage with adjustable k1, b, RRF k and threshold and returns
side-by-side scores (BM25 | cosine | RRF rank | cross-encoder). Read-only; uses the tenant index of the running RagServer."""
from __future__ import annotations

import numpy as np
from rank_bm25 import BM25Okapi

from mcp_servers.rag_server import tokenize


def run_lab(rag, tenant: str, query: str, k1=1.25, b=0.75, rrf_k=60, threshold=0.42, top_k=50):
    ix = rag.index_for(tenant)
    bm = BM25Okapi(ix.tokens, k1=k1, b=b)
    sc = bm.get_scores(tokenize(query))
    qv = rag.models.encode([query])
    sims, ids = ix.index.search(qv, min(top_k, len(ix.chunks)))
    cos = {int(i): float(s) for i, s in zip(ids[0], sims[0]) if i >= 0}
    bm_rank = {int(i): r for r, i in enumerate(np.argsort(-sc)[:top_k], 1) if sc[i] > 0}
    dn_rank = {i: r for r, i in enumerate(sorted(cos, key=lambda x: -cos[x]), 1)}
    fused = {i: (1 / (rrf_k + bm_rank[i]) if i in bm_rank else 0) + (1 / (rrf_k + dn_rank[i]) if i in dn_rank else 0) for i in set(bm_rank) | set(dn_rank)}
    order = sorted(fused, key=lambda i: -fused[i])[:40]
    rr = rag.models.rerank(query, [ix.chunks[i]["text"] for i in order]) if order else []
    rows = []
    for rk, (i, s) in enumerate(sorted(zip(order, rr), key=lambda x: -x[1]), 1):
        rows.append({"chunk": ix.chunks[i]["chunk_id"], "BM25": round(float(sc[i]), 3), "cosine": round(cos.get(i, 0.0), 3),
                     "RRF rank": order.index(i) + 1, "rerank": round(float(s), 3), "final rank": rk, "passes threshold": s >= threshold})
    return rows, (max(rr) if rr else 0.0) < threshold
