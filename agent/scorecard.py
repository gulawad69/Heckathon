"""36-KPI scorecard (finaldesign.md §7). Reads evals/results/latest.json (+ load.json from a real k6/JMeter run).
Status: PASS | FAIL | NOT_MEASURED (needs infra/real judge that is absent) | PROXY (local stand-in metric, informational,
never counted as a pass). Certification requires all 36 = PASS."""
from __future__ import annotations

import json
from pathlib import Path

LATEST, LOAD = Path("evals/results/latest.json"), Path("evals/results/load.json")


def _get(d, *path):
    for p in path:
        if not isinstance(d, dict) or p not in d: return None
        d = d[p]
    return d


def build() -> dict:
    r = json.loads(LATEST.read_text()) if LATEST.exists() else {}
    ld = json.loads(LOAD.read_text()) if LOAD.exists() else {}
    if ld.get("smoke"): ld = {}  # scaled-down local runs are never KPI-31..36 evidence
    lat = r.get("latency_local", {})
    G = lambda *p: _get(r, *p)
    P = lambda name, q="p95": _get(lat, name, q)
    rows = []

    def k(i, stage, name, thr, op, val, unit="", note="", kind="measured"):
        if val is None: st = "NOT_MEASURED"
        elif kind == "proxy": st = "PROXY"
        else: st = "PASS" if (val <= thr if op == "<=" else val >= thr) else "FAIL"
        rows.append({"id": f"KPI-{i:02d}", "stage": stage, "metric": name, "threshold": f"{op} {thr}{unit}", "value": val, "status": st, "note": note})

    S1, S2, S3, S4, S5, S6 = "1 Ingress & Triage", "2 Hybrid Retrieval", "3 Agent & Loop", "4 Generation & RAGAS", "5 Token & Graphify", "6 Scale & Load"
    k(1, S1, "Pre-hook guardrail latency p95", 15, "<=", P("hook.pre"), " ms")
    k(2, S1, "PII redaction recall", 0.998, ">=", G("redteam", "pii_recall"), "", "n=15 smuggling prompts; enlarge before relying on it")
    k(3, S1, "Prompt-injection block rate", 0.995, ">=", G("redteam", "injection_block_rate"), "", "85 injection prompts (shield is pattern-based)")
    k(4, S1, "Triage routing accuracy", 0.97, ">=", G("triage", "accuracy"), "", "rule-based stand-in for distilled classifier")
    k(5, S1, "Triage latency p95", 25, "<=", G("triage", "p95_ms"), " ms")
    k(6, S1, "False-positive refusal on benign", 0.01, "<=", G("benign", "false_positive_refusal_rate"))
    k(7, S2, "BM25 latency p95", 12, "<=", G("retrieval", "bm25_p95_ms"), " ms")
    k(8, S2, "Bi-encoder + FAISS latency p95", 25, "<=", G("retrieval", "dense_p95_ms"), " ms", "MiniLM stand-in on CPU")
    k(9, S2, "Cross-encoder latency p95", 45, "<=", G("retrieval", "rerank_p95_ms"), " ms", "CPU torch stand-in; design needs GPU FP16 ONNX bge-reranker-v2-m3")
    k(10, S2, "Hybrid Context Recall@50", 0.92, ">=", G("retrieval", "recall_at_50"), "", "corpus <50 chunks: pool = whole corpus")
    k(11, S2, "Post-rerank Context Precision@4", 0.88, ">=", G("retrieval", "context_precision_at_4"), "", "chunks passing 0.42 cutoff")
    k(12, S2, "MRR@4", 0.85, ">=", G("retrieval", "mrr_at_4"))
    k(13, S2, "nDCG@4", 0.86, ">=", G("retrieval", "ndcg_at_4"))
    k(14, S3, "Tool schema first-pass validation", 0.96, ">=", G("harness", "agent_traffic_first_pass_rate"), "", "agent-built calls")
    k(15, S3, "Auto-repair success on malformed args", 0.90, ">=", G("harness", "repair_success_rate"))
    k(16, S3, "Infinite-loop escape rate", 1.0, ">=", G("chaos", "loop_escape_rate"))
    k(17, S3, "Mean ReAct steps per task", 2.2, "<=", G("golden", "mean_steps"))
    k(18, S3, "Context drift (goal cosine)", 0.85, ">=", None, "", "GoalAnchorBlock is a frozen dataclass at prompt index 0 (structural); no embedding drift audit run")
    k(19, S3, "Tier-3 HITL enforcement", 1.0, ">=", G("hitl", "enforcement_rate"))
    k(20, S4, "RAGAS faithfulness", 0.92, ">=", G("golden", "faithfulness_mean"), "", "PROXY: Verification entailment, not the ragas library", "proxy")
    k(21, S4, "RAGAS answer relevancy", 0.90, ">=", G("golden", "answer_relevancy_proxy"), "", "PROXY: token cosine query/answer", "proxy")
    k(22, S4, "RAGAS context entity recall", 0.85, ">=", G("golden", "context_entity_recall_proxy"), "", "PROXY: entity overlap", "proxy")
    k(23, S4, "Unanswerable abstention accuracy", 0.98, ">=", G("golden", "abstention_accuracy"), "", "held-out negatives only")
    k(24, S4, "Hallucination rate", 0.005, "<=", G("golden", "hallucination_rate"), "", "independent verbatim check; valid for extractive generation only")
    k(25, S4, "TTFT p95 (streaming)", 180, "<=", G("ttft_p95_ms"), " ms")
    k(26, S5, "Prompt-token reduction", 0.45, ">=", G("golden", "token_reduction_mean"), "", "vs naive un-pruned top-10 chunk dump")
    k(27, S5, "Semantic cache hit ratio (steady)", 0.35, ">=", G("cache", "hit_ratio_steady_synthetic"), "", "synthetic skewed traffic")
    k(28, S5, "Cross-tenant cache isolation leaks", 0, "<=", G("isolation", "leaks"))
    k(29, S5, "Cost / 1,000 queries", 0.45, "<=", None, " USD", "needs a real LLM + pricing; extractive generation has no token cost")
    k(30, S5, "MCP JSON-RPC serialization p95", 4, "<=", G("mcp_serde_p95_ms"), " ms")
    for i, (nm, thr, op, key, unit) in enumerate([("e2e p50 @ 50K VUs", 240, "<=", "p50", " ms"), ("e2e p95 @ 50K VUs", 450, "<=", "p95", " ms"), ("e2e p99 @ 50K VUs", 590, "<=", "p99", " ms"),
                                                  ("HTTP 5xx rate @ 50K VUs", 0.0005, "<=", "err", ""), ("Sustained throughput", 8500, ">=", "rps", " RPS"),
                                                  ("RSS growth over 1-h soak", 0.03, "<=", "rss", "")], start=31):
        k(i, S6, nm, thr, op, ld.get(key), unit, "no 50K-VU run available" if ld.get(key) is None else "from load.json")
    c = {s: sum(1 for x in rows if x["status"] == s) for s in ("PASS", "FAIL", "NOT_MEASURED", "PROXY")}
    return {"rows": rows, "counts": c, "certified": c["PASS"] == 36, "generated_at": r.get("generated_at"), "overrides": r.get("profile_overrides", {})}


if __name__ == "__main__":
    sc = build()
    for x in sc["rows"]:
        v = x["value"]; v = f"{v:.4g}" if isinstance(v, (int, float)) else "-"
        print(f"{x['id']} {x['status']:12} {x['metric'][:42]:42} {v:>10} (need {x['threshold']})  {x['note']}")
    print(sc["counts"], "CERTIFIED" if sc["certified"] else "NOT CERTIFIED", "| local overrides:", sc["overrides"])
