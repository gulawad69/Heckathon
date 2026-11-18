"""mcp-hybrid-rag (TIER_1_READ): 3-stage hybrid retrieval (finaldesign.md §4.6).
Stage1 BM25Okapi(k1=1.25,b=0.75,top50, Porter) || Stage2 dense bi-encoder + FAISS HNSW(efSearch=64, top50)
-> RRF(k=60, dedup chunk_hash, pool 40) -> Stage3 cross-encoder rerank (batch 32, top 4, threshold 0.42).

LOCAL STAND-INS (swappable, budgets unchanged; D13/ADR-2): bi-encoder = all-MiniLM-L6-v2, reranker =
ms-marco-MiniLM-L-6-v2 (+ Platt calibration in calibration.json per ADR-6), run via sentence-transformers instead of
ONNX FP16 bge-large / bge-reranker-v2-m3. Set RAG_BI_ENCODER / RAG_RERANKER to swap."""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _P

_sys.path.insert(0, str(_P(__file__).resolve().parents[1]))  # repo root, so servers run standalone over stdio/HTTP

import asyncio
import hashlib
import json
import math
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import torch

torch.set_num_threads(int(os.environ.get("TORCH_THREADS", "4")))

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

from nltk.stem import PorterStemmer
from rank_bm25 import BM25Okapi

from agent import errors
from agent.models import Chunk, HybridRetrieveIn, HybridRetrieveOut
from agent.telemetry import REC, span
from mcp_servers.base import McpServerBase, serve_stdio

VAULT = Path(os.environ.get("VAULT_DIR", "knowledge_vault"))
CAL = Path(__file__).with_name("calibration.json")
STOP = set("a an the of to in on for is are be and or with what which can i me my do does how should any it that this "
           "at by from as about there their you your".split())
_ps = PorterStemmer()
_stem_cache: dict[str, str] = {}


def tokenize(text: str) -> list[str]:
    out = []
    for w in re.findall(r"[a-z0-9]+", text.lower()):
        if w in STOP:
            continue
        s = _stem_cache.get(w)
        if s is None:
            s = _stem_cache[w] = _ps.stem(w)
        out.append(s)
    return out


def parse_note(path: Path) -> tuple[dict, str]:
    raw = path.read_text()
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", raw, re.S)
    meta = dict(l.split(": ", 1) for l in m.group(1).splitlines() if ": " in l) if m else {}
    return meta, (m.group(2) if m else raw)


def chunk_words(text: str, size=512, overlap=64) -> list[str]:
    w = text.split()
    if len(w) <= size:
        return [" ".join(w)]
    out, i = [], 0
    while i < len(w):
        out.append(" ".join(w[i:i + size]))
        if i + size >= len(w):
            break
        i += size - overlap
    return out


class TenantIndex:
    def __init__(self, tenant: str, models: "Models"):
        import faiss
        self.tenant = tenant
        d = VAULT / tenant
        self.chunks: list[dict] = []
        h = hashlib.sha256()
        for p in sorted(d.glob("*.md")):
            meta, body = parse_note(p)
            body = re.sub(r"\n> DEMO.*$", "", body, flags=re.S).strip()  # banner is not content
            body = re.sub(r"^#.*\n+", "", body).strip()  # heading duplicates the frontmatter title
            h.update(p.read_bytes())
            for i, c in enumerate(chunk_words(body)):
                self.chunks.append({"chunk_id": f"{meta.get('id', p.stem)}#{i}", "note_id": meta.get("id", p.stem),
                                    "chunk_hash": hashlib.sha256(c.encode()).hexdigest()[:32], "text": c,
                                    "source_uri": f"vault://{tenant}/{p.name}", "domain": meta.get("domain", ""),
                                    "risk_tier": int(meta.get("risk_tier", 1))})
        self.version = h.hexdigest()[:12]
        self.stale = False
        self.dir_mtime = max((p.stat().st_mtime for p in d.glob("*.md")), default=0)
        self.tokens = [tokenize(c["text"]) for c in self.chunks]  # pre-tokenized
        self.bm25 = BM25Okapi(self.tokens, k1=1.25, b=0.75) if self.chunks else None
        self.models = models
        if self.chunks:
            emb = models.encode([c["text"] for c in self.chunks])
            self.index = faiss.IndexHNSWFlat(emb.shape[1], 32, faiss.METRIC_INNER_PRODUCT)
            self.index.hnsw.efConstruction = 100
            self.index.add(emb)
            self.index.hnsw.efSearch = 64


class Models:
    def __init__(self):
        from sentence_transformers import CrossEncoder, SentenceTransformer
        self.bi = SentenceTransformer(os.environ.get("RAG_BI_ENCODER", "sentence-transformers/all-MiniLM-L6-v2"))
        self.ce = CrossEncoder(os.environ.get("RAG_RERANKER", "cross-encoder/ms-marco-MiniLM-L-6-v2"))
        cal = json.loads(CAL.read_text()) if CAL.exists() else {"a": 1.0, "b": 0.0}
        self.a, self.b = cal["a"], cal["b"]
        self.encode(["warmup"]); self.rerank("warmup", ["warmup"])

    def encode(self, texts):
        return np.asarray(self.bi.encode(texts, normalize_embeddings=True, convert_to_numpy=True, batch_size=32), dtype="float32")

    def logits(self, q: str, docs: list[str]) -> list[float]:
        out: list[float] = []
        for i in range(0, len(docs), 32):  # batch 32, max_len 512
            out += [float(x) for x in np.atleast_1d(self.ce.predict([(q, d) for d in docs[i:i + 32]], convert_to_numpy=True))]
        return out

    def rerank(self, q: str, docs: list[str]) -> list[float]:
        """sigmoid(a*logit+b); (a,b) = Platt calibration from calibration.json so that 0.42 is a calibrated cutoff (ADR-6)."""
        return [1 / (1 + math.exp(-(self.a * x + self.b))) for x in self.logits(q, docs)]


class RagServer(McpServerBase):
    name = "hybrid-rag"

    def __init__(self, rerank_budget_ms: float | None = None, share_from: "RagServer | None" = None):
        """share_from => read-replica: reuses the primary's loaded models/indexes (FMEA #3 failover target), no reload."""
        super().__init__()
        self.models = share_from.models if share_from else Models()
        self.indexes: dict[str, TenantIndex] = share_from.indexes if share_from else {}
        self.pool = share_from.pool if share_from else ThreadPoolExecutor(max_workers=4)
        self.stale_checked: dict[str, float] = {}
        self.rerank_budget_ms = rerank_budget_ms or float(os.environ.get("RERANK_BUDGET_MS", "45"))
        self.register("hybrid_retrieve", HybridRetrieveIn, self.hybrid_retrieve, "TIER_1_READ",
                      timeout_ms=float(os.environ.get("MCP_RAG_TIMEOUT_MS", "150")))
        for d in (sorted(VAULT.iterdir()) if VAULT.is_dir() and not share_from else []):  # warm: never build an index inside the 150 ms budget
            if d.is_dir():
                self.index_for(d.name)

    def index_for(self, tenant: str) -> TenantIndex:
        ix = self.indexes.get(tenant)
        if ix is None:
            if not (VAULT / tenant).is_dir():
                raise errors.McpError(errors.RBAC_DENIED, "unknown tenant namespace")
            ix = self.indexes[tenant] = TenantIndex(tenant, self.models)
        return ix

    def index_stale(self, tenant: str) -> bool:
        """FMEA #12: serve the loaded index but flag it when the vault on disk changed (checked at most every 5 s)."""
        now = time.time()
        if now - self.stale_checked.get(tenant, 0) < 5:
            return self.indexes[tenant].stale if tenant in self.indexes else False
        self.stale_checked[tenant] = now
        ix = self.indexes.get(tenant)
        if ix is None: return False
        h = hashlib.sha256()
        for p in sorted((VAULT / tenant).glob("*.md")): h.update(p.read_bytes())
        ix.stale = h.hexdigest()[:12] != ix.version
        if ix.stale: REC.inc("index_stale")
        return ix.stale

    def get_chunk(self, tenant: str, chunk_id: str) -> dict:  # resource rag://chunks/{chunk_id}
        for c in self.index_for(tenant).chunks:
            if c["chunk_id"] == chunk_id:
                return c
        raise errors.McpError(errors.RBAC_DENIED, "chunk not in tenant namespace")  # cross-tenant id => -32003

    async def hybrid_retrieve(self, a: HybridRetrieveIn) -> HybridRetrieveOut:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self.pool, self._retrieve, a)

    def _retrieve(self, a: HybridRetrieveIn) -> HybridRetrieveOut:
        out = self._retrieve_inner(a)
        out.index_stale = self.index_stale(a.ctx.tenant_id)
        if out.abstain:  # -32008 ABSTAIN: below-threshold evidence; payload carries the full abstention result
            raise errors.McpError(errors.ABSTAIN, "evidence below threshold", out.model_dump(mode="json"))
        return out

    def _retrieve_inner(self, a: HybridRetrieveIn) -> HybridRetrieveOut:
        tid = a.ctx.traceparent.split("-")[1]
        ix = self.index_for(a.ctx.tenant_id)
        ms: dict[str, float] = {}
        stale = False
        if not ix.chunks:
            return HybridRetrieveOut(chunks=[], top_score=0.0, abstain=True, stage_ms={}, early_abstain=True, index_version=ix.version)
        q = a.query
        # ---- Stage 1: BM25 ----
        with span("mcp.rag.bm25", tid):
            t = time.perf_counter()
            qt = tokenize(q)
            scores = ix.bm25.get_scores(qt) if qt else np.zeros(len(ix.chunks))
            order = [i for i in np.argsort(-scores)[: a.bm25_top_k] if scores[i] > 0]
            bm = {int(i): float(scores[i]) for i in order}
            ms["bm25"] = (time.perf_counter() - t) * 1000
        # ---- Stage 2: dense ----
        with span("mcp.rag.biencoder", tid):
            t = time.perf_counter()
            qv = self.models.encode([q])
            k = min(a.dense_top_k, len(ix.chunks))
            sims, ids = ix.index.search(qv, k)
            dn = {int(i): float(s) for i, s in zip(ids[0], sims[0]) if i >= 0}
            ms["dense"] = (time.perf_counter() - t) * 1000
        # ---- RRF ----
        with span("mcp.rag.rrf", tid):
            t = time.perf_counter()
            fused: dict[str, float] = {}
            rank_of: dict[int, int] = {}
            for lst in (list(bm), sorted(dn, key=lambda i: -dn[i])):
                for r, i in enumerate(lst, 1):
                    fused[ix.chunks[i]["chunk_hash"]] = fused.get(ix.chunks[i]["chunk_hash"], 0.0) + 1.0 / (a.rrf_k + r)
            by_hash = {c["chunk_hash"]: i for i, c in enumerate(ix.chunks)}
            pool = sorted(fused, key=lambda h: -fused[h])[: a.fusion_pool]  # dedup by chunk_hash
            for r, h in enumerate(pool, 1):
                rank_of[by_hash[h]] = r
            ms["rrf"] = (time.perf_counter() - t) * 1000
        # early-abstain (D12): empty pool, or weak lexical AND semantic signal
        max_cos = max(dn.values(), default=0.0)
        max_bm_norm = max((s / (s + 5.0) for s in bm.values()), default=0.0)
        if not pool or (max_cos < EARLY_COS and max_bm_norm < EARLY_BM25):
            return HybridRetrieveOut(chunks=[], top_score=0.0, abstain=True, stage_ms=ms, early_abstain=True,
                                     index_version=ix.version)
        idxs = [by_hash[h] for h in pool]
        naive = sum(int(len(ix.chunks[i]["text"].split()) * 1.3) for i in idxs[:10])
        # ---- Stage 3: rerank (budgeted; timeout => degraded RRF top-k, FMEA #2) ----
        degraded = False
        with span("mcp.rag.rerank", tid):
            t = time.perf_counter()
            fut = self.pool.submit(self.models.rerank, q, [ix.chunks[i]["text"] for i in idxs])
            try:
                rr = fut.result(timeout=self.rerank_budget_ms / 1000)
            except Exception:
                rr, degraded = None, True
                REC.inc("rerank_timeout")
            ms["rerank"] = (time.perf_counter() - t) * 1000
        if degraded:
            top = idxs[: a.rerank_k]
            chunks = [self._chunk(ix, i, bm, dn, rank_of, None) for i in top]
            return HybridRetrieveOut(chunks=chunks, top_score=0.0, abstain=False, stage_ms=ms, degraded=True,
                                     index_version=ix.version, naive_dump_tokens=naive)
        ranked = sorted(zip(idxs, rr), key=lambda x: -x[1])
        top_score = ranked[0][1]
        if top_score < a.score_threshold:
            return HybridRetrieveOut(chunks=[], top_score=top_score, abstain=True, stage_ms=ms, index_version=ix.version)
        chunks = [self._chunk(ix, i, bm, dn, rank_of, s) for i, s in ranked[: a.rerank_k] if s >= a.score_threshold]
        return HybridRetrieveOut(chunks=chunks, top_score=top_score, abstain=False, stage_ms=ms, index_version=ix.version,
                                 naive_dump_tokens=naive)

    @staticmethod
    def _chunk(ix, i, bm, dn, rank_of, rerank):
        c = ix.chunks[i]
        return Chunk(chunk_id=c["chunk_id"], chunk_hash=c["chunk_hash"], text=c["text"], source_uri=c["source_uri"],
                     bm25=bm.get(i), cosine=dn.get(i), rrf_rank=rank_of.get(i), rerank=rerank)


_EA = Path(__file__).with_name("early_abstain.json")  # written by tools/calibrate_early_abstain.py (ADR-6)
_ea = json.loads(_EA.read_text()) if _EA.exists() else {}
EARLY_COS = float(os.environ.get("EARLY_ABSTAIN_COS", _ea.get("cos", "0.30")))
EARLY_BM25 = float(os.environ.get("EARLY_ABSTAIN_BM25", _ea.get("bm25", "0.10")))

if __name__ == "__main__":
    serve_stdio(RagServer())
