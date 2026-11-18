"""Streamlit Developer Workbench (Gate 18, finaldesign.md §13). Run:
   source profiles/local_cpu.env && streamlit run app.py
Six tabs. Everything shown is read from the live in-process agent or from evals/results; nothing is mocked."""
import asyncio, json, os, sys, threading, time, uuid
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, os.path.dirname(__file__))
os.environ.setdefault("MCP_MANIFEST_KEY", "dev-only-local-key")

from agent import errors, scorecard
from agent.harness import Identity
from agent.lab import run_lab
from agent.mcp_host import sign_manifests
from agent.models import QueryIn
from agent.service import AgentService
from agent.telemetry import REC, SPAN_NAMES, new_traceparent
from mcp_servers.graphify_server import build_graph
from mcp_servers.rag_server import VAULT, parse_note
from plugins.base import LOCKED
from tools.jobs import Jobs
import subprocess, urllib.request

st.set_page_config(page_title="Pharma Agent Workbench", page_icon="💊", layout="wide", initial_sidebar_state="expanded")

# Presentation only: styling. No behaviour depends on this block.
st.markdown("""
<style>
.block-container {padding-top: 1.6rem; max-width: 1500px;}
.hero {background: linear-gradient(120deg,#0f766e 0%,#0e7490 55%,#1d4ed8 100%); color:#fff; padding:1.1rem 1.4rem;
       border-radius:14px; margin-bottom:1rem; box-shadow:0 4px 14px rgba(0,0,0,.15);}
.hero h1 {margin:0; font-size:1.65rem; font-weight:700; color:#fff; padding:0;}
.hero p {margin:.25rem 0 0 0; opacity:.92; font-size:.95rem;}
.chip {display:inline-block; padding:.12rem .6rem; border-radius:999px; font-size:.78rem; font-weight:600; margin:.1rem .25rem .1rem 0;}
.chip.ok {background:#d1fae5; color:#065f46;} .chip.bad {background:#fee2e2; color:#991b1b;}
.chip.warn {background:#fef3c7; color:#92400e;} .chip.info {background:#e0f2fe; color:#075985;}
div[data-testid="stMetric"] {background: rgba(128,128,128,.08); border:1px solid rgba(128,128,128,.22); padding:.7rem .9rem; border-radius:12px;}
button[data-baseweb="tab"] {font-weight:600; padding:.55rem .9rem;}
h2, h3 {margin-top:.6rem !important;}
div[data-testid="stExpander"] {border-radius:10px;}
section[data-testid="stSidebar"] .stAlert {font-size:.82rem;}
</style>
""", unsafe_allow_html=True)


def chip(text, kind="info"): return f'<span class="chip {kind}">{text}</span>'


@st.cache_resource
def boot():
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    sign_manifests()
    svc = asyncio.run_coroutine_threadsafe(_mk(), loop).result(300)
    return loop, svc


async def _mk():
    svc = AgentService(db_path="state.db")
    svc.state.repo.set_role("tenant_pharma_a", "pharm1", "APPROVER")
    svc.state.repo.set_role("tenant_pharma_b", "pharmB", "APPROVER")
    return svc


LOOP, SVC = boot()


@st.cache_resource
def jobs(): return Jobs()
J = jobs()
def run(coro, timeout=120): return asyncio.run_coroutine_threadsafe(coro, LOOP).result(timeout)
TENANTS = sorted(p.name for p in VAULT.iterdir() if p.is_dir())

st.sidebar.title("💊 Pharma Formulary Agent")
st.sidebar.markdown("**Session**")
tenant = st.sidebar.selectbox("Tenant", TENANTS)
user = st.sidebar.text_input("User id", "demo_user")
approver = {"tenant_pharma_a": "pharm1", "tenant_pharma_b": "pharmB"}.get(tenant, "pharm1")
st.sidebar.caption(f"Approver identity: `{approver}`")
ov = scorecard.build()["overrides"]
if ov:
    st.sidebar.warning(f"LOCAL CPU PROFILE active - budgets overridden: {ov}. Latency KPIs are not certifiable here.")
st.sidebar.info("Demo vault content is synthetic. Not medical advice. Replace with pharmacist-approved monographs.")

st.markdown(f'''<div class="hero"><h1>Pharma Agent Workbench</h1>
<p>Live developer console for the tenant-isolated formulary agent: triage, hybrid RAG, MCP, memory, red-team and KPI scorecard.</p>
{chip(f"tenant: {tenant}", "info")}{chip(f"user: {user}", "info")}{chip("LOCAL CPU PROFILE", "warn") if ov else chip("DESIGN PROFILE", "ok")}</div>''', unsafe_allow_html=True)

tabs = st.tabs(["🏗️ Architecture & Workflow", "🔎 Hybrid RAG Lab", "🔌 MCP & Plugins", "🧠 Memory & Graphify",
                "🛡️ Red Team & Breaker", "📊 KPI Scorecard & Load"])

# ------------------------------------------------------------------ Tab 1
with tabs[0]:
    c1, c2 = st.columns([1, 1])
    with c1:
        st.subheader("design.md contract checklist (finaldesign.md)")
        checks = {"finaldesign.md locked": Path("finaldesign.md").exists(), "Gate 2 diagram (300 DPI)": Path("docs/workflow_orchestration.png").exists(),
                  "mcp_config.json": Path("mcp_config.json").exists(), "3 MCP servers": all(Path(f"mcp_servers/{n}").exists() for n in ("rag_server.py", "graphify_server.py", "state_server.py")),
                  "Signed manifests": Path("mcp_servers/manifests.json").exists(), "Golden 250 / Red team 100": Path("evals/golden_250.jsonl").exists() and Path("evals/redteam_100.jsonl").exists(),
                  "k6 + JMeter plans": Path("load_tests/k6_50k_load.js").exists() and Path("load_tests/jmeter_50k_plan.jmx").exists()}
        st.progress(sum(checks.values()) / len(checks), text=f"{sum(checks.values())}/{len(checks)} contract items present")
        st.markdown("".join(chip(("✓ " if v else "✗ ") + k, "ok" if v else "bad") for k, v in checks.items()), unsafe_allow_html=True)
        st.subheader("Live triage routing console")
        q = st.text_input("Ask the agent", "What OTC options are listed for headache in adults?")
        if st.button("Send", type="primary", width="stretch"):
            o = run(SVC.handle(QueryIn(tenant_id=tenant, user_id=user, query=q)))
            run(SVC.drain())
            stt = SVC.last_states[-1] if SVC.last_states else None
            st.session_state["last"] = (o, stt)
        if "last" in st.session_state:
            o, stt = st.session_state["last"]
            (st.success if o.status == "ANSWERED" else st.warning)(f"{o.status}  (trace {o.trace_id[:12]}...)")
            if o.answer: st.write(o.answer)
            if o.remediation: st.info(o.remediation)
            if o.citations: st.caption("Citations: " + ", ".join(o.citations))
            if stt and stt.triage:
                t = stt.triage
                m = st.columns(5)
                for mc, (lbl, val) in zip(m, [("Route", t.route), ("Intent", t.intent), ("Domain", t.domain), ("Risk tier", t.risk_tier), ("Complexity", t.complexity)]):
                    mc.metric(lbl, str(val))
            st.subheader("ReAct step-by-step trace")
            for i, s in enumerate(stt.steps if stt else [], 1):
                with st.expander(f"{i}. {s['state']} - {s['action']} ({s['ms']} ms)"): st.json(s)
    with c2:
        st.image("docs/workflow_orchestration.png", caption="Gate 2: workflow_orchestration.png", width="stretch")
    st.subheader("Notifications for this user (§2.F)")
    nt = SVC.state.notifications(tenant, user)
    st.dataframe(pd.DataFrame(nt) if nt else pd.DataFrame(columns=["approval_id", "message", "created_at", "read"]), width="stretch")
    st.subheader("Tier-3 HITL Approve / Reject queue")
    rows = SVC.state.pending(tenant)
    df = pd.DataFrame(rows, columns=["approval_id", "requester", "lot", "from", "to", "reason", "status"])
    st.dataframe(df, width="stretch")
    pend = df[df.status == "PENDING_APPROVAL"]
    if len(pend):
        sel = st.selectbox("Pending approval", pend.approval_id.tolist())
        cc = st.columns(2)
        for col, dec in zip(cc, ("APPROVE", "REJECT")):
            if col.button(dec):
                try: st.write(run(SVC.decide(tenant, approver, uuid.UUID(sel), dec))); st.rerun()
                except errors.McpError as e: st.error(str(e))
    else:
        st.caption("No pending approvals. Try: 'Please quarantine lot 12345'.")

# ------------------------------------------------------------------ Tab 2
with tabs[1]:
    st.subheader("Stage 1 BM25 | Stage 2 bi-encoder | RRF | Stage 3 cross-encoder")
    q = st.text_input("Lab query", "maximum daily dose of paracetamol", key="labq")
    a, b_, c, d = st.columns(4)
    k1 = a.slider("k1", 0.5, 2.5, 1.25, 0.05); bb = b_.slider("b", 0.0, 1.0, 0.75, 0.05)
    rk = c.slider("RRF k", 1, 200, 60); th = d.slider("threshold", 0.0, 1.0, 0.42, 0.01)
    rows, abst = run_lab(SVC.rag, tenant, q, k1, bb, rk, th)
    st.dataframe(pd.DataFrame(rows), width="stretch")
    (st.error if abst else st.success)("Deterministic abstention (top score below threshold)" if abst else "Evidence passes threshold")
    st.caption("Stand-ins: MiniLM bi-encoder + ms-marco cross-encoder (Platt-calibrated); design models are bge-large / bge-reranker-v2-m3 ONNX FP16.")

# ------------------------------------------------------------------ Tab 3
with tabs[2]:
    st.subheader("MCP servers (JSON-RPC 2.0)")
    stt_ = run(SVC.host.status())
    st.dataframe(pd.DataFrame(stt_).T, width="stretch")
    c1, c2, c3 = st.columns(3)
    for col, nm in zip((c1, c2, c3), ("hybrid-rag", "state-store", "graphify-vault")):
        up = col.toggle(f"{nm} up", SVC.transports[nm].up, key=f"up_{nm}"); SVC.transports[nm].up = up
    SVC.cache_down = c1.toggle("Simulate Redis/cache down (FMEA #1)", SVC.cache_down)
    SVC.state.read_only = c2.toggle("Simulate Postgres failover (T3 refuses, FMEA #6)", SVC.state.read_only)
    st.subheader("JSON-RPC payload inspector")
    tool = st.selectbox("Tool", ["hybrid_retrieve", "resolve_subgraph", "cache_get", "session_facts_get"])
    ctx = {"tenant_id": tenant, "user_id": user, "session_id": str(uuid.uuid4()), "idempotency_key": str(uuid.uuid4()), "traceparent": new_traceparent()[1], "tier": "TIER_1_READ"}
    default = {"hybrid_retrieve": {"ctx": ctx, "query": "ibuprofen with warfarin"}, "resolve_subgraph": {"ctx": ctx, "query": "ibuprofen safety"},
               "cache_get": {"ctx": ctx, "key_hash": "0" * 64}, "session_facts_get": {"ctx": ctx, "limit": 5}}[tool]
    payload = st.text_area("arguments", json.dumps(default, indent=2), height=220)
    if st.button("Send JSON-RPC"):
        srv = {"hybrid_retrieve": "hybrid-rag", "resolve_subgraph": "graphify-vault"}.get(tool, "state-store")
        req = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": json.loads(payload)}}
        t0 = time.perf_counter(); resp = run(SVC.transports[srv].server.dispatch(json.dumps(req)))
        st.caption(f"{(time.perf_counter()-t0)*1000:.1f} ms"); st.code(json.dumps(req)[:600], "json"); st.json(json.loads(resp))
    st.subheader("Plugins (Pre/Post hot-toggles; security plugins locked)")
    for name, p in SVC.plugins.plugins.items():
        cols = st.columns([2, 2, 2, 2])
        cols[0].write(f"**{name}**"); cols[1].write(p.stage); cols[2].write(f"priority {p.priority}, fail_closed={p.fail_closed}")
        if name in LOCKED: cols[3].write("locked ON")
        else:
            on = cols[3].toggle("enabled", p.enabled, key=f"pl_{name}"); SVC.plugins.set_enabled(name, on)
    st.json(SVC.plugins.plugins["otel_exporter"].status())
    st.subheader("Runbooks & housekeeping")
    r1, r2, r3 = st.columns(3)
    if r1.button("Purge this tenant's cache namespace"): st.write({"purged_keys": SVC.purge_cache_namespace(tenant)})
    if r2.button("Run housekeeping (outbox replay, escalate/expire stale approvals)"): st.write(run(SVC.housekeeping()))
    if r3.button("Evaluate incident alerts now / reset mitigations"):
        st.write(SVC.incident_tick()); SVC.reset_incidents() if st.session_state.get("rst") else None
    st.checkbox("Reset mitigations on next click", key="rst")
    st.write({"semantic_cache": SVC.s.semantic_cache, "top_k": SVC.s.bm25_top_k, "rerank_cutoff": SVC.s.score_threshold, "strict_extractive": SVC.s.strict_extractive,
              "mcp_failovers": SVC.host.failovers, "incident_log": [(time.strftime("%H:%M:%S", time.localtime(t)), x) for t, x in SVC.incident_log[-5:]]})

# ------------------------------------------------------------------ Tab 4
with tabs[3]:
    st.subheader("Graphify subgraph (<= depth 2, <= 400 tokens)")
    qg = st.text_input("Anchor query", "ibuprofen safety", key="gq")
    ctxg = {"tenant_id": tenant, "user_id": user, "session_id": str(uuid.uuid4()), "idempotency_key": str(uuid.uuid4()), "traceparent": new_traceparent()[1], "tier": "TIER_1_READ"}
    sub = json.loads(run(SVC.graph.dispatch(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "resolve_subgraph", "arguments": {"ctx": ctxg, "query": qg}}}))))
    if "result" in sub and sub["result"]["nodes"]:
        r = sub["result"]; dot = "digraph G {rankdir=LR; node[shape=box,style=rounded];" + "".join(f'"{n["id"]}" [label="{n["id"]}\\nrisk {n["risk_tier"]}"{",style=filled,fillcolor=lightblue" if n["id"]==r["anchor_id"] else ""}];' for n in r["nodes"]) + "".join(f'"{a}"->"{b}";' for a, b in r["edges"]) + "}"
        st.graphviz_chart(dot); st.caption(f"anchor={r['anchor_id']} tokens~{r['token_estimate']}")
    st.subheader("Obsidian atomic note viewer")
    notes = sorted((VAULT / tenant).glob("*.md")); pick = st.selectbox("Note", [n.stem for n in notes]); st.code((VAULT / tenant / f"{pick}.md").read_text(), "markdown")
    st.subheader("Session state browser (tenant-scoped)")
    c1, c2 = st.columns(2)
    c1.write("Redis-equivalent cache keys"); c1.write([k for k in SVC.state.cache if k.startswith(f"t:{tenant}:")][:20])
    c1.write("Hot window (last 3 turns, per session)"); c1.write({str(k[1])[:8]: list(v) for k, v in SVC.window.w.items() if k[0] == tenant})
    c2.write("session_facts (Postgres-equivalent)"); c2.dataframe(pd.DataFrame(SVC.state.repo.q("SELECT session_id, text, verified_at FROM session_facts WHERE tenant_id=? ORDER BY verified_at DESC LIMIT 20", (tenant,)), columns=["session", "fact", "verified_at"]))
    c2.write("audit tail (hash-chained)"); c2.dataframe(pd.DataFrame(SVC.state.repo.audit_rows(tenant, 8), columns=["seq", "hash", "payload"]))
    ok, n = SVC.state.repo.verify_chain(tenant); c2.caption(f"chain verify: {'OK' if ok else 'BROKEN'} ({n} events)")
    tr = list(REC.durations.get("token_reduction", []))
    st.subheader("Token savings gauge")
    if tr: st.progress(min(1.0, max(0.0, sum(tr) / len(tr))), text=f"prompt-token reduction vs naive top-10 dump: {sum(tr)/len(tr):.0%} (gate 45%, target 55%, n={len(tr)})")
    else: st.caption("Ask a question in Tab 1 to populate.")

# ------------------------------------------------------------------ Tab 5
with tabs[4]:
    st.subheader("One-click adversarial runner")
    rt = [json.loads(l) for l in Path("evals/redteam_100.jsonl").read_text().splitlines()]
    from plugins.injection_shield import scan, scan_chunk
    from plugins.pii_redactor import redact
    def sh(cat, n=5):
        out = []
        for r in [x for x in rt if x["cat"] == cat][:n]:
            if cat in ("direct_jailbreak", "arg_overflow_sqli"):
                o = run(SVC.handle(QueryIn(tenant_id=tenant, user_id="rt", query=r["prompt"][:3900]))); out.append((r["prompt"][:70], o.status))
            elif cat == "indirect_injection": out.append((r["chunk"][:70], "DROPPED" if scan_chunk(r["chunk"]) else "PASSED (BAD)"))
            else: out.append((r["prompt"][:70], redact(r["prompt"])[0][:70]))
        return pd.DataFrame(out, columns=["input", "result"])
    c = st.columns(4)
    if c[0].button("Direct injections"): st.dataframe(sh("direct_jailbreak"))
    if c[1].button("Indirect (poisoned chunks)"): st.dataframe(sh("indirect_injection"))
    if c[2].button("Obfuscated PII"): st.dataframe(sh("pii_smuggling"))
    if c[3].button("SQLi / overflow"): st.dataframe(sh("arg_overflow_sqli"))
    c = st.columns(2)
    if c[0].button("Infinite SKU retry loop"):
        sid = uuid.uuid4(); idn = Identity(tenant, "rt", sid, "END_USER", new_traceparent()[1]); log = []
        for i in range(6):
            try: run(SVC.harness.call("execution", "workflow_transition", {"lot": "SKU-xyz", "from_state": "AVAILABLE", "to_state": "QUARANTINED", "reason": "loop"}, idn)); log.append((i + 1, "ok"))
            except errors.McpError as e: log.append((i + 1, f"{e.code} {errors.NAMES.get(e.code)}"))
        st.dataframe(pd.DataFrame(log, columns=["attempt", "result"]))
    if c[1].button("Cross-tenant cache probe"):
        qq = "What is the maximum daily dose of paracetamol?"
        outs = [(t, run(SVC.handle(QueryIn(tenant_id=t, user_id="probe", query=qq)))) for t in TENANTS]
        st.dataframe(pd.DataFrame([(t, o.tenant_id, "4 g" in (o.answer or ""), "3 g" in (o.answer or "")) for t, o in outs], columns=["asked as", "echoed tenant", "has A figure (4 g)", "has B figure (3 g)"]))
        st.caption("Each tenant must only ever see its own formulary figure.")
    st.subheader("Circuit breaker")
    b = SVC.breaker; st.write({"open_keys": sum(1 for v in b.fails.values() if v >= 2), "forced_open_tools": sorted(b.forced_open)})
    c = st.columns(3)
    tool_t = c[0].selectbox("Tool", ["hybrid_retrieve", "workflow_transition"])
    if c[1].button("Trip breaker"): b.forced_open.add(tool_t)
    if c[2].button("Reset all"): b.reset()

# ------------------------------------------------------------------ Tab 6
with tabs[5]:
    sc = scorecard.build()
    cn = sc["counts"]
    st.subheader("36-KPI release scorecard")
    st.metric("Certification", "CERTIFIED" if sc["certified"] else "NOT CERTIFIED", f"{cn['PASS']} pass / {cn['FAIL']} fail / {cn['NOT_MEASURED']} not measured / {cn['PROXY']} proxy")
    if sc["overrides"]: st.warning(f"Measured under LOCAL CPU PROFILE overrides {sc['overrides']} - latency rows are not design-hardware numbers.")
    df = pd.DataFrame(sc["rows"])[["id", "metric", "stage", "threshold", "value", "status", "note"]].rename(columns={"id": "KPI ID", "metric": "KPI name"})
    st.dataframe(df.style.map(lambda v: {"PASS": "background-color:#c8e6c9", "FAIL": "background-color:#ffcdd2", "NOT_MEASURED": "background-color:#eeeeee", "PROXY": "background-color:#fff9c4"}.get(v, ""), subset=["status"]), width="stretch", height=600)
    st.caption(f"Source: evals/results/latest.json ({sc['generated_at']}). Refresh with: `source profiles/local_cpu.env && python3 evals/run_eval.py`")
    st.subheader("Live OpenTelemetry span waterfall (latest trace)")
    if SVC.last_states:
        tid = SVC.last_states[-1].trace_id
        sp = sorted([(n, ms, s0) for t, n, ms, s0 in REC.recent if t == tid], key=lambda x: x[2])
        if sp:
            base = sp[0][2]; rows = [{"span": n, "start_ms": round((s0 - base) * 1000, 2), "ms": round(ms, 2), "end_ms": round((s0 - base) * 1000 + ms, 2)} for n, ms, s0 in sp]
            import altair as alt
            df_w = pd.DataFrame(rows)
            st.altair_chart(alt.Chart(df_w).mark_bar().encode(x=alt.X("start_ms:Q", title="ms since first span"), x2="end_ms:Q", y=alt.Y("span:N", sort=None), tooltip=["span", "start_ms", "ms"]), use_container_width=True)
            st.dataframe(df_w)
    st.subheader("RAGAS triad (PROXY gauges)")
    c = st.columns(3)
    for col, (k, lbl) in zip(c, [("ragas.faithfulness", "Faithfulness"), ("ragas.answer_relevancy", "Answer relevancy"), ("ragas.context_entity_recall", "Context entity recall")]):
        v = list(REC.durations.get(k, [])); col.metric(lbl, f"{sum(v)/len(v):.2f}" if v else "-")
    st.subheader("Latency histogram vs 200-600 ms SLA")
    e2e = list(REC.durations.get("e2e.all", []))
    if e2e:
        h = pd.cut(pd.Series(e2e), bins=[0, 60, 200, 450, 600, 5000]).value_counts().sort_index(); h.index = h.index.astype(str); st.bar_chart(h)
        st.write({"p50": REC.pct("e2e.all", 50), "p95": REC.pct("e2e.all", 95), "p99": REC.pct("e2e.all", 99), "n": len(e2e), "scope": "this session, local CPU profile"})
    ld = Path("evals/results/load.json")
    if ld.exists():
        L = json.loads(ld.read_text())
        st.write("k6/JMeter latency vs SLA" + (" (LOCAL scaled run, not a KPI-31..36 run)" if L.get("smoke") else ""))
        pts = pd.DataFrame({"percentile": ["p50", "p95", "p99"], "measured_ms": [L.get("p50"), L.get("p95"), L.get("p99")], "SLA_ms": [240, 450, 590]}).set_index("percentile")
        st.bar_chart(pts)
    st.info("50K-VU k6/JMeter results: " + (ld.read_text()[:300] if ld.exists() else "no load run imported (KPI-31..36 NOT_MEASURED)."))

    st.divider()
    st.subheader("Run everything from here")
    st.caption("Launches the same commands as the README as background jobs (logs below). Results land in evals/results/ and refresh the table above.")
    lp = st.checkbox("Use LOCAL CPU profile (scaled budgets; required on this machine)", True, key="lp")
    c1, c2, c3 = st.columns(3)
    mode = c1.radio("KPI evaluation", ["Quick (~2 min)", "Full (~10 min)"], key="evmode")
    if c1.button("Run KPI evaluation", disabled=J.running("eval")):
        J.start("eval", "python3 evals/run_eval.py" + (" --quick" if mode.startswith("Quick") else ""), lp)
    if c2.button("Run unit tests (79)", disabled=J.running("tests")):
        J.start("tests", "python3 -m pytest -q tests 2>&1 | tail -15", lp)
    api_up = False
    try: api_up = urllib.request.urlopen("http://localhost:8000/health", timeout=1).status == 200
    except Exception: pass
    c3.write(f"API server on :8000 - **{'UP' if api_up else 'DOWN'}**")
    if c3.button("Start API (2 workers)", disabled=api_up or J.running("api")):
        J.start("api", "uvicorn agent.api:create_app --factory --port 8000 --workers 2", lp)
    if c3.button("Stop API", disabled=not J.running("api")): J.stop("api")
    st.markdown("**k6 load test**")
    k1, k2, k3 = st.columns(3)
    vus = k1.number_input("Virtual users", 1, 50000, 10, help="This machine handles ~10. 50,000 needs the distributed infrastructure.")
    base = k2.text_input("Target base URL", "http://localhost:8000")
    rss = k3.text_input("RSS growth (soak, from Prometheus; optional)", "")
    if st.button("Run k6", disabled=J.running("k6") or (not api_up and "localhost" in base)):
        flags = "" if vus >= 50000 else "--allow-smoke"
        env = f"-e LOCAL_VUS={int(vus)} " if vus < 50000 else ""
        imp = f"python3 tools/import_load_results.py /tmp/k6_ui.json {flags}" + (f" --rss-growth {rss}" if rss else "")
        J.start("k6", f"k6 run -q {env}-e BASE={base} --summary-export=/tmp/k6_ui.json load_tests/k6_50k_load.js; {imp}", lp)
    if not api_up and "localhost" in base: st.caption("Start the API first.")
    if vus < 50000: st.caption("Runs below 50,000 VUs are recorded but labelled local; they never count toward KPI-31..36.")

    @st.fragment(run_every=3)
    def status():
        if "eval" in J.p and not J.running("eval") and "eval" not in J.__dict__.setdefault("done", set()):
            J.done.add("eval"); st.rerun()  # eval finished: reload the whole page so the scorecard table above shows new numbers
        for n in ("eval", "tests", "api", "k6"):
            if n in J.p:
                s = "running" if J.running(n) else f"finished (exit {J.code(n)})"
                with st.expander(f"{n}: {s}", expanded=J.running(n)):
                    st.code(J.tail(n) or "(no output yet)")
        if "k6" in J.p and not J.running("k6") and Path("evals/results/load.json").exists():
            st.write("Last imported load result:"); st.json(json.loads(Path("evals/results/load.json").read_text()))
    status()
