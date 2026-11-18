# gaps.md: alignment audit of the built agent vs `finaldesign.md` and the 18-gate Word blueprint

Audit date: 2026-11-18. **Update:** items 1-9, 13, 15 and 18 were fixed afterwards (see the Status column and section H). Items 10-12, 14, 16, 17, 19-23 are still open. A full backup of the pre-fix project is at `../H1_backup_before_gaps`.
Extra features added beyond the spec are kept (see the last section).

Legend: **Code** = can be built here without new infrastructure. **Infra** = needs infrastructure/hardware or an owner decision.

## A. Aligned (verified)
- Bounded loop (5 steps, 450 ms total, per-step `min(120 ms, remaining)`), state machine, no recursive self-calls.
- Harness order: strict Pydantic v2 validation, deterministic auto-repair, RBAC tier check, UUIDv4 idempotency, then breaker and dispatch.
- 3-stage RAG parameters (BM25 k1=1.25/b=0.75/top 50, HNSW efSearch 64, RRF k=60 pool 40, rerank batch 32, top 4, 0.42), early-abstain and rerank-abstain paths.
- Circuit breaker, bulkheads (64/128/256), deterministic abstention, Tier-3 HITL (pending, same-tenant approver, expiry, never auto-approve).
- Hash-chained per-tenant audit log, tenant-scoped cache keys and SQL predicates, PII and injection plugins (fail closed, not hot-disable-able), signed manifests.
- Error-code table, public API contract (tenant echo, 403 on header mismatch), 36-KPI scorecard, 6-tab workbench, k6 script and JMeter plan, Gate 2 diagram.
- Reconciliation decisions D1 to D13.

## B. Gaps: behaviour the spec requires that is not wired up (Code)

| # | Spec source | Gap |
|---|---|---|
| 1 | ADR-3, §10 latency runbook, Gate 11 | **Semantic cache missing.** `semantic_cache` flag flips to cosine >= 0.92 but no semantic lookup exists. Only exact-key lookup works. **[FIXED]** |
| 2 | §10, Gate 10-11 | **Incident runbooks never run automatically.** `AgentService.evaluate_incidents` is only called from a test. No timer for "p95 e2e > 550 ms for 30 s", nor for the faithfulness < 0.90 / 5 min window. **[FIXED]** |
| 3 | §4.3, Gate 5 | **Memory read path missing.** `session_facts` are written but `session_facts_get` is never called. The 3-turn window is stored but not used in prompts. The compressed observation (<= 180 tokens) is computed but not passed to the LLM. **[FIXED]** |
| 4 | FMEA #6 | **Outbox never drained.** Failed audit writes go to `outbox` but nothing replays them, so "never dropped" is not met. **[FIXED]** |
| 5 | FMEA #5 | **No `503` + `Retry-After`** as the last LLM fallback (backoff, secondary pool and extractive fallback exist). **[FIXED]** |
| 6 | FMEA #3, #10, #11, #12 | **Partial FMEA fallbacks:** no read-replica index failover or worker restart (#3); no escalation of stale approvals (#10); no namespace purge (#11); no stale-index (`index_version`) mismatch alert (#12). **[FIXED]** |
| 7 | §2.F | **No user notification** when an approver decides. **[FIXED]** |
| 8 | §3.5 | **`-32008 ABSTAIN` never emitted** as an error code (abstention is returned as a flag). `-32004 PII_BLOCK` is raised only on plugin failure and control characters. **[FIXED]** |
| 9 | ADR-6, §6 | **Early-abstain thresholds (cos 0.30 / BM25 0.10) not calibrated** on the negative set or recorded. **[FIXED]** |

## C. Gaps: architecture the docs specify that is absent (mostly Infra)

| # | Spec source | Gap | Type |
|---|---|---|---|
| 10 | Gate 0, §3.7, `mcp_config.json` | **MCP transports:** default run uses in-process JSON-RPC. stdio works and is tested but the `mcpServers` block is never read to launch/connect servers. | Code |
| 11 | Gate 0, §6, Gates 15-16 | **Infrastructure missing:** Redis Cluster, Postgres + pgvector + RLS, Triton/ONNX FP16 reranker, bge-large / bge-reranker-v2-m3, vLLM/TRT-LLM, NGINX/Envoy, Dockerfile / Kubernetes (HPA, replicas), OTel Collector + Prometheus + Tempo + Grafana. | Infra |
| 12 | Gate 12, §7 | **No CI/CD pipeline** running KPI 1-30 per PR and RAGAS gates on every change. | Code (config) |
| 13 | Gate 17, §5 | **Graphify not run:** `graphify build ./src ./knowledge_vault --output graph.json` never executed, no `graph.json`. Graph covers vault notes only, not Python modules / Pydantic schemas. | Code **[FIXED]** |
| 14 | Gate 13, §11 | **Governance:** no secret manager (env var only), no least-privilege DB roles, no nightly chain-verification job. | Infra / Code |
| 15 | Gate 2 | **PNG DPI metadata absent** (rendered with `dpi=300`, 7567x5657 px, but no pHYs DPI tag). | Code **[FIXED]** |
| 16 | ADR-2 | **Quantization guardrail not run:** "<= 1% absolute drop in nDCG@4/MRR@4 vs FP32". | Infra |

## D. Smaller deviations

| # | Gap |
|---|---|
| 17 | Repo layout names `evals/run_ragas.py` and `evals/ir_bench.py`; built as one `evals/run_eval.py`. |
| 18 | Tab 6 span waterfall lays spans end to end (misrepresents overlapping spans). Latency histogram shows this session's data, not k6/JMeter results. **[FIXED]** |
| 19 | Tab 4 gauge shows token savings only, no cost-reduction figure. |
| 20 | Platform Engineer persona ("config only, no tenant content access") not enforced. |
| 21 | JMeter plan lacks the JDBC/SQLite persistence validation from Gate 16. |
| 22 | k6 script uses 2 tenants (spec script used 50). |
| 23 | BM25 uses `rank_bm25` with no explicit inverted index. |

## E. Not measurable here (status, not a code gap)
KPI-09 (rerank 45 ms), KPI-08, KPI-25 fail on this CPU. KPI-18, 29, 31-36 not measured. KPI-20/21/22 proxy only. Eval sets are synthetic. See `README.md` "Honest status".

## F. Extra features added beyond the spec (kept)
Emergency redirect, refusal of clinical actions (prescribe / dispense / diagnose), separation of duties on approvals, LOT state-transition allowlist, local-CPU profile, UI job runner (evals, tests, API, k6), Platt calibration of the reranker, PROXY / NOT_MEASURED KPI labels, synthetic demo vault and eval generators.

## G. Proposed order (awaiting owner approval)
1. Items 1 to 9 (behaviour), 13, 15, 18.
2. Item 10 (read `mcpServers` config; run via stdio / HTTP).
3. Items 12 and 14 (CI, nightly verification) and deployable configs for item 11 (compose, NGINX, Prometheus/Grafana). These can be written but not proven on this machine.

## H. What the fixes did (2026-11-18)
| # | Change | Default behaviour | Test |
|---|---|---|---|
| 1 | Semantic cache (`AgentService._sem_*`): tenant-scoped embedding index of shared formulary entries; used only when the latency runbook sets `semantic_cache` (cosine >= 0.92) | OFF, exact-key only, unchanged | `test_semantic_cache_incident_mode` |
| 2 | `incident_tick()` (p95 e2e > 550 ms sustained 30 s over a 60 s window; faithfulness < 0.90 / 5 min) + `housekeeping()` run by a 5 s monitor in the API lifespan. `INCIDENT_AUTOMATION=0` disables the alerts (set in `profiles/local_cpu.env`, because the local CPU always exceeds 550 ms under load) | housekeeping always on; alerts on unless env says 0 | `test_incident_tick_latency_runbook` |
| 3 | Memory read path: `session_facts_get` + last 3 turns go into the generation context (`memory_text`); compressed observation (<= 180 tokens) is passed as "Previous observation" from step 2. Verification still checks claims against chunks only, so memory cannot launder claims | on; extractive generator ignores it, so answers unchanged | `test_memory_read_path` |
| 4 | `StateServer.drain_outbox()` replays queued audit events (deleted only after a successful append); called by `drain()` and the monitor | on | `test_outbox_replayed_not_dropped` |
| 5 | `LLMUnavailable` -> `ServiceUnavailable` -> API `503` + `Retry-After: 1` (stream: `event: error`). Only reachable with a real LLM endpoint whose fallback is also empty | default extractive path unchanged | `test_llm_unavailable_503` |
| 6 | Read-replica failover for `hybrid-rag` (`RagServer(share_from=...)`, `McpHost.replicas`, stdio worker restart hook); stale-approval escalation (`escalate_stale`, once, notifies approvers, stays PENDING); `purge_cache_namespace(tenant)`; stale-index flag `index_stale` on retrieval output | failover only on TIMEOUT/INTERNAL from primary | `test_read_replica_failover`, `test_stale_approval_escalation`, `test_purge_namespace_only_one_tenant`, `test_stale_index_flag` |
| 7 | `notifications` table, written on approve/reject/expire and escalation; `GET /api/v1/notifications`; shown in Tab 1 | on | `test_user_notified_on_decision` |
| 8 | `hybrid_retrieve` now returns `-32008 ABSTAIN` on the wire (payload carries the abstention result); Retrieval Subagent handles it; PII plugin blocks with `-32004` when an address-like residue survives redaction | on | `test_abstain_error_code_on_wire`, `test_pii_residue_blocked` |
| 9 | `tools/calibrate_early_abstain.py` -> `mcp_servers/early_abstain.json` (cos 0.48, BM25 0.11; 0 of 106 calibration positives early-abstained, 10 of 19 negatives). Fitted on the even half only | on; env `EARLY_ABSTAIN_*` still overrides | `test_early_abstain_calibrated_and_recorded` |
| 13 | `tools/graphify_build.py` -> `graph.json`: 31 Python modules, 18 Pydantic schemas, 17 policy notes. Code nodes sit in `_code` and are never served to tenants; runtime uses `graph.json` only if it matches the vault, else builds live | on | `test_graph_json_has_code_schemas_and_notes`, `test_graph_not_serving_code_to_tenants` |
| 15 | PNG carries a 300 DPI tag (7567x5657 px); generator script stamps it | n/a | `test_png_is_300_dpi` |
| 18 | Tab 6 waterfall uses real span start offsets (overlaps visible); latency chart plots imported k6 p50/p95/p99 against the SLA, labelled when the run is local | n/a | `test_span_start_offsets_recorded` |

Not done on purpose: nothing from the "extra features" list was removed. Items 10-12, 14, 16, 17, 19-23 remain open.
