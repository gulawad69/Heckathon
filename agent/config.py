"""Runtime settings. Defaults are the design budgets (finaldesign.md §4.2, §6). The local-CPU profile overrides
only via env (e.g. RERANK_BUDGET_MS, STEP_TIMEOUT_MS) and every override is reported in the KPI scorecard."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _f(k, d): return float(os.environ.get(k, d))


@dataclass
class Settings:
    max_iterations: int = 5
    total_loop_budget_ms: float = field(default_factory=lambda: _f("LOOP_BUDGET_MS", 450))
    step_timeout_ms: float = field(default_factory=lambda: _f("STEP_TIMEOUT_MS", 120))
    score_threshold: float = 0.42
    breaker_max_identical: int = 2
    rag_pool: int = 64
    db_pool: int = 128
    llm_pool: int = 256
    reserved_pool: int = 32
    cache_ttl_s: int = 300
    cache_jitter_s: int = 45
    semantic_cosine: float = 0.92
    local_lru_ttl_s: int = 60
    bm25_top_k: int = 50
    dense_top_k: int = 50
    strict_extractive: bool = False
    semantic_cache: bool = False  # enabled by latency incident runbook (cosine >= 0.92)
    min_faithfulness: float = 0.92
    approval_ttl_s: int = field(default_factory=lambda: int(_f("APPROVAL_TTL_S", 86400)))

    def overrides(self) -> dict:
        return {k: os.environ[k] for k in ("LOOP_BUDGET_MS", "STEP_TIMEOUT_MS", "RERANK_BUDGET_MS", "MCP_RAG_TIMEOUT_MS") if k in os.environ}
