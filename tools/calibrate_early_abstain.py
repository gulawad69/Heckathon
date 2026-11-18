"""ADR-6 / §6: calibrate the early-abstain rule  (max cosine < C  AND  max normalised BM25 < B)  on the EVEN half of the golden set.
Constraint: no answerable calibration question may early-abstain (with a safety margin). Objective: maximise early-abstained negatives.
Writes mcp_servers/early_abstain.json (read by rag_server.py). Held-out odd half is untouched. Re-sign manifests is NOT needed (json is data)."""
import json, sys
sys.path.insert(0, ".")
import numpy as np
from mcp_servers.rag_server import CAL, Models, TenantIndex, tokenize

models = Models()
G = [json.loads(l) for l in open("evals/golden_250.jsonl")][0::2]
idx = {t: TenantIndex(t, models) for t in {g["tenant_id"] for g in G}}
rows = []
for g in G:
    ix = idx[g["tenant_id"]]
    qt = tokenize(g["question"]); sc = ix.bm25.get_scores(qt) if qt else np.zeros(len(ix.chunks))
    sims, _ = ix.index.search(models.encode([g["question"]]), min(50, len(ix.chunks)))
    bm = max((s / (s + 5.0) for s in sc if s > 0), default=0.0)
    rows.append((float(sims[0].max()), bm, g["answerable"]))
pos = [(c, b) for c, b, a in rows if a]; neg = [(c, b) for c, b, a in rows if not a]
best = (0, 0.30, 0.10)
for B in np.arange(0.05, 0.41, 0.01):
    for C in np.arange(0.10, 0.61, 0.01):
        if any(c < C and b < B for c, b in pos): continue   # would early-abstain a legitimate question
        k = sum(c < C and b < B for c, b in neg)
        if k > best[0] or (k == best[0] and C * B < best[1] * best[2]): best = (k, float(C), float(B))
k, C, B = best
margin = 0.02  # safety margin below the tightest positive
C, B = max(0.05, C - margin), max(0.02, B - margin / 2)
out = {"cos": round(C, 3), "bm25": round(B, 3), "fit_on": "golden_250 even half", "early_abstained_negatives": int(sum(c < C and b < B for c, b in neg)),
       "negatives": len(neg), "positives": len(pos), "positives_early_abstained": int(sum(c < C and b < B for c, b in pos))}
open("mcp_servers/early_abstain.json", "w").write(json.dumps(out))
print(out)
