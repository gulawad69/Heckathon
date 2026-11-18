"""ADR-6: calibrate the cross-encoder score so that the fixed 0.42 cutoff separates relevant from irrelevant chunks.
Fits Platt (a,b) on the EVEN-indexed half of evals/golden_250.jsonl (balanced classes; recall/false-pass at 0.42 are printed). evals/run_eval.py reports on the held-out ODD half. Recalibrate after any model/index change."""
import json, sys
sys.path.insert(0, ".")
import numpy as np
from sklearn.linear_model import LogisticRegression
from mcp_servers.rag_server import CAL, Models, TenantIndex, VAULT

models = Models(); models.a, models.b = 1.0, 0.0
G = [json.loads(l) for l in open("evals/golden_250.jsonl")][0::2]
idx = {t: TenantIndex(t, models) for t in {g["tenant_id"] for g in G}}
X, y = [], []
for g in G:
    ix = idx[g["tenant_id"]]
    lg = models.logits(g["question"], [c["text"] for c in ix.chunks])
    for c, l in zip(ix.chunks, lg):
        X.append(l); y.append(int(c["chunk_id"] in g["expected_chunks"]))
X, y = np.array(X).reshape(-1, 1), np.array(y)
lr = LogisticRegression(class_weight={0: 1.0, 1: 1.0}, C=10.0).fit(X, y)
a, b = float(lr.coef_[0][0]), float(lr.intercept_[0])
CAL.write_text(json.dumps({"a": a, "b": b, "fit_on": "golden_250 even half", "n": int(len(y)), "pos": int(y.sum())}))
p = 1 / (1 + np.exp(-(a * X.ravel() + b)))
print(f"a={a:.3f} b={b:.3f} pos={int(y.sum())} neg={int((1-y).sum())}  recall@0.42={(p[y==1]>=0.42).mean():.3f}  false-pass@0.42={(p[y==0]>=0.42).mean():.3f}")
