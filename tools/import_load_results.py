"""Import a REAL k6 summary (k6 run --summary-export=summary.json ...) into evals/results/load.json for KPI-31..35.
KPI-36 (RSS growth) must be supplied from Prometheus: add  --rss-growth 0.012 .  Refuses smoke runs (< 50,000 peak VUs)."""
import argparse, json
ap = argparse.ArgumentParser(); ap.add_argument("summary"); ap.add_argument("--rss-growth", type=float); ap.add_argument("--allow-smoke", action="store_true")
a = ap.parse_args()
s = json.load(open(a.summary))["metrics"]
vus = s.get("vus_max", {}).get("max") or s.get("vus_max", {}).get("value", 0)
if vus < 50000 and not a.allow_smoke:
    raise SystemExit(f"peak VUs {vus} < 50,000: not a KPI-31..36 run (use --allow-smoke to record it anyway, it will be labelled)")
d = s["http_req_duration"]
out = {"p50": d.get("med", d.get("p(50)")), "p95": d["p(95)"], "p99": d.get("p(99)"), "err": s["http_req_failed"].get("value", s["http_req_failed"].get("rate")),
       "rps": s["http_reqs"]["rate"], "rss": a.rss_growth, "peak_vus": vus, "smoke": vus < 50000}
json.dump(out, open("evals/results/load.json", "w"), indent=2); print(out)
