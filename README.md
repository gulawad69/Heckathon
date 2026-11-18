# Pharma Formulary Agent (built to `finaldesign.md` / the 18-gate blueprint)

A multi-tenant pharmacy assistant that **suggests only medicines listed in a tenant's approved formulary**, with cited,
grounded, verified answers. It **abstains** when evidence is weak, **refuses** clinical actions (prescribe / dispense /
diagnose) and **redirects emergencies**. Batch-lot state changes are Tier-3: they only ever create a `PENDING_APPROVAL`
request that a pharmacist approver in the same tenant must approve.

> **Not medical advice. Not certified.** The bundled vault is *synthetic demo content*. A pharmacist must replace it with
> approved monographs before any real use. Scorecard status today: see "Honest status" below.

## Run

```bash
source profiles/local_cpu.env                    # LOCAL profile (scaled budgets, dev manifest key) - see "Honest status"
python3 tools/build_demo_vault.py knowledge_vault   # demo notes (tenant_pharma_a / tenant_pharma_b)
python3 tools/sign_manifests.py                  # re-run after ANY change to mcp_servers/*.py (unsigned/changed => refuses to load)
python3 -m pytest -q tests                       # 79 tests
python3 evals/run_eval.py                        # all locally measurable KPIs -> evals/results/latest.json
python3 -m agent.scorecard                       # 36-KPI scorecard
uvicorn agent.api:create_app --factory --port 8000   # API: POST /api/v1/agent/query  (+ /query/stream, /approvals, /health)
streamlit run app.py                             # 6-tab developer workbench (Gate 18)
python3 mcp_servers/state_server.py              # optional: state-store over Streamable HTTP :8081 ; rag/graphify run over stdio
```

```bash
curl -s localhost:8000/api/v1/agent/query -H 'content-type: application/json' -H 'X-Tenant-ID: tenant_pharma_a' \
  -d '{"tenant_id":"tenant_pharma_a","user_id":"u1","query":"What is the maximum daily dose of paracetamol?"}'
```

## Gate → code map

| Gate | Where |
|---|---|
| 0 stack, `mcp_config.json`, plugins | `mcp_config.json`, `plugins/`, `agent/mcp_host.py`, `mcp_servers/` |
| 1 design lock | `finaldesign.md` (this repo's contract; `design.md` kept as audit trail) |
| 2 workflow image (300 DPI) | `docs/workflow_orchestration.png` (+ generator), rendered in Tab 1 |
| 3 & 8 3-stage RAG, RAGAS | `mcp_servers/rag_server.py`, `evals/run_eval.py`, `tools/calibrate_threshold.py` |
| 4 harness + bounded loop | `agent/harness.py`, `agent/loop.py` |
| 5 context retention | `agent/memory.py` (GoalAnchorBlock, ≤180-token compression, 3-turn window, `session_facts`) |
| 6–7 triage + subagents | `agent/triage.py`, `agent/subagents/{retrieval,execution,verification}.py` |
| 9 reliability | `plugins/circuit_breaker.py`, bulkheads in harness, abstention in loop/rag, HITL in `state_server.py` |
| 10–11 observability | `agent/telemetry.py` (span names per Gate 10), `plugins/otel_exporter.py`, incident runbooks in `AgentService.evaluate_incidents` |
| 12 KPIs | `agent/scorecard.py` (36 rows), Tab 6 |
| 13–14 governance, red team | hash-chained audit + tenant-scoped repository (`state_server.py`), signed manifests, `evals/redteam_100.jsonl` |
| 15–16 load | `load_tests/k6_50k_load.js`, `load_tests/jmeter_50k_plan.jmx` |
| 17 tokens | `knowledge_vault/`, `mcp_servers/graphify_server.py` |
| 18 workbench | `app.py` |

## Pharma-specific rules (inside the gates, not beyond them)
* Only formulary OTC products are listed; prescription-only, pediatric dosing and pregnancy questions are answered **only**
  from the vault's pharmacist-referral notes. Every answer ends with a fixed (non-LLM) safety footer.
* `prescribe / dispense / diagnose / start patient on…` → `REFUSED` with a pharmacist referral (hard boundary 1: no autonomous clinical execution).
* Emergency language (overdose, chest pain, anaphylaxis, self-harm…) → deterministic emergency redirect, no retrieval.
* Lots (`LOT-#####`, a natural fit for batch numbers) transition only via `workflow_transition` → `PENDING_APPROVAL`; approval
  needs an `APPROVER` of the **same tenant**, never the requester; expiry ⇒ `REJECTED`, never auto-approve.

## Honest status (what is real, what is a stand-in, what is not proven)

Latest local run: see `python3 -m agent.scorecard`. At the time of writing: **21 PASS, 4 FAIL, 8 NOT_MEASURED, 3 PROXY → NOT CERTIFIED.**

| Design requirement | What runs here | Consequence |
|---|---|---|
| bge-large / bge-reranker-v2-m3, ONNX FP16, GPU | MiniLM bi-encoder + ms-marco cross-encoder (torch CPU), Platt-calibrated so 0.42 stays the cutoff | rerank ≈ 490 ms vs 45 ms budget ⇒ **KPI-09 FAIL**; an ONNX-INT8 trial gave only ~25 % |
| 450 ms loop / 120 ms step budgets | **scaled by env in `profiles/local_cpu.env`** (every override is printed in Tab 6 and the scorecard) | e2e here ≈ 0.5 s; design budgets need the GPU stack |
| vLLM / TRT-LLM | deterministic extractive generator (quotes chunks with citations); `VLLM_URL` switches to an OpenAI-compatible endpoint with the FMEA #5 fallbacks | KPI-29 (cost) NOT_MEASURED; KPI-24 independent check is only valid for extractive output |
| Redis Cluster + Postgres/pgvector (+RLS) | in-memory cache + SQLite behind a tenant-scoped `Repository` | isolation logic is tested; RLS/Redis not exercised |
| Distilled triage classifier | rule/lexicon classifier, same interface | KPI-04 measured on a templated set |
| `ragas` library / LLM judge | proxies (labelled PROXY, never counted as PASS) | KPI-20/21/22 not certified |
| 250-question golden set, 100-prompt red team | **synthetic, templated from the demo vault** (`tools/build_evals.py`); threshold calibrated on the even half, reported on the held-out half | replace with expert-written sets before trusting numbers |
| 50K-VU k6/JMeter, 1-h soak | scripts + importer provided, **not run at scale** | KPI-31..36 NOT_MEASURED |
| `graphify` CLI | built-in equivalent (anchor + ≤ depth 2 traversal) | same contract |
| KPI-18 drift | structural only (frozen `GoalAnchorBlock`) | NOT_MEASURED |

Implementation choices where the source left room (all conservative): separation of duties on approvals; approver identity is
asserted by the gateway (`X-User-ID`) — real authn must be added at NGINX/Envoy; manifest key comes from `MCP_MANIFEST_KEY`
(dev key only in `profiles/local_cpu.env`); `workflow_transition` allows only AVAILABLE/QUARANTINED/RELEASED/RECALLED edges.
