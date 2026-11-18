"""Tests for the gap-closing changes (gaps.md items 1-9, 13, 15, 18)."""
import asyncio, json, time, uuid
import pytest
from PIL import Image
from agent import errors
from agent.models import QueryIn
from agent.telemetry import REC
from tests.conftest import run, ident
from mcp_servers.rag_server import RagServer


def Q(svc, q, t="tenant_pharma_a", u="u1", sid=None): return run(svc.handle(QueryIn(tenant_id=t, user_id=u, query=q, session_id=sid)))


# 1 semantic cache: only in incident mode, only inside the tenant namespace
def test_semantic_cache_incident_mode(svc):
    Q(svc, "What is the maximum daily dose of paracetamol?"); run(svc.drain())
    h = svc.cache_stats["hit"]
    Q(svc, "What is the maximum daily dosage of paracetamol?", u="p1")           # semantic mode OFF by default => exact-only => miss
    assert svc.cache_stats["hit"] == h
    svc.s.semantic_cache = True
    o = Q(svc, "What is the maximum daily dosage of paracetamol for adults?", u="p2")  # new wording, ON => semantic hit
    assert svc.cache_stats["hit"] == h + 1 and o.status == "ANSWERED" and REC.counters["semantic_hit"] >= 1
    o_b = Q(svc, "What is the maximum daily dosage of paracetamol for adults?", t="tenant_pharma_b", u="p3")  # other tenant: never A's entries
    assert "3 g" in o_b.answer and "4 g in 24" not in o_b.answer


# 2 incident automation
def test_incident_tick_latency_runbook(svc):
    now = time.time(); REC.reset()
    for _ in range(40): REC.observe("e2e.all", 900.0)
    assert svc.incident_tick(now) == []  # breach just started: needs 30 s sustained
    acts = svc.incident_tick(now + 31)
    assert any("latency" in a for a in acts) and svc.s.semantic_cache and svc.s.bm25_top_k == 25
    svc.reset_incidents(); assert not svc.s.semantic_cache and svc.s.bm25_top_k == 50
    REC.reset()


# 3 memory read path
def test_memory_read_path(svc):
    sid = uuid.uuid4()
    Q(svc, "What is the maximum daily dose of paracetamol?", sid=sid); run(svc.drain())
    Q(svc, "Which tablet for hay fever?", sid=sid)
    st = svc.last_states[-1]
    assert "Verified facts" in st.memory_text and "paracetamol" in st.memory_text.lower()
    assert st.observation  # compressed observation is kept for the next ReAct step
    assert svc.state.repo.q("SELECT count(*) FROM session_facts WHERE tenant_id='tenant_pharma_a'")[0][0] >= 1


# 4 outbox replay
def test_outbox_replayed_not_dropped(svc):
    before = svc.state.repo.verify_chain("tenant_pharma_a")[1]
    svc.state.repo.q("INSERT INTO outbox(tenant_id, payload) VALUES(?,?)", ("tenant_pharma_a", json.dumps({"kind": "LATE", "detail": {}})))
    assert svc.state.drain_outbox() == 1
    ok, n = svc.state.repo.verify_chain("tenant_pharma_a")
    assert ok and n == before + 1 and not svc.state.repo.q("SELECT * FROM outbox")


# 5 503 + Retry-After
def test_llm_unavailable_503(svc, monkeypatch):
    from fastapi.testclient import TestClient
    from agent import llm as L
    from agent.api import create_app
    async def down(*a, **k): raise L.LLMUnavailable("429")
    monkeypatch.setattr(svc.llm, "generate", down)
    with TestClient(create_app(svc)) as c:
        r = c.post("/api/v1/agent/query", json={"tenant_id": "tenant_pharma_a", "user_id": "u503", "query": "Which tablet for hay fever? 503"}, headers={"X-Tenant-ID": "tenant_pharma_a"})
    assert r.status_code == 503 and r.headers["Retry-After"] == "1"


# 6 FMEA fallbacks
def test_read_replica_failover(svc):
    svc.transports["hybrid-rag"].up = False  # primary MCP worker down
    o = Q(svc, "What is the maximum daily dose of paracetamol?", u="fo")
    assert o.status == "ANSWERED" and svc.host.failovers >= 1
    svc.transports["hybrid-rag"].up = True


def test_stale_approval_escalation(svc):
    o = Q(svc, "quarantine lot 41414", u="esc")
    assert svc.state.escalate_stale("tenant_pharma_a", sla_s=0) == 1 and svc.state.escalate_stale("tenant_pharma_a", sla_s=0) == 0
    assert svc.state.pending("tenant_pharma_a")[0][6] == "PENDING_APPROVAL"  # escalated, never auto-approved
    assert any("ESCALATION" in n["message"] for n in svc.state.notifications("tenant_pharma_a", "pharm1"))


def test_purge_namespace_only_one_tenant(svc):
    q = "What is the maximum daily dose of paracetamol?"
    Q(svc, q, t="tenant_pharma_a"); Q(svc, q, t="tenant_pharma_b"); run(svc.drain())
    assert svc.purge_cache_namespace("tenant_pharma_a") >= 1
    assert not any(k.startswith("t:tenant_pharma_a:") for k in svc.state.cache) and any(k.startswith("t:tenant_pharma_b:") for k in svc.state.cache)


def test_stale_index_flag(rag, tmp_path, monkeypatch):
    ix = rag.indexes["tenant_pharma_a"]
    assert rag.index_stale("tenant_pharma_a") is False
    ix.version = "different"; rag.stale_checked.clear()
    assert rag.index_stale("tenant_pharma_a") is True  # served but flagged
    ix.version = ix.version if False else __import__("hashlib").sha256(b"").hexdigest()[:12]
    from pathlib import Path
    import hashlib
    h = hashlib.sha256()
    for p in sorted(Path("knowledge_vault/tenant_pharma_a").glob("*.md")): h.update(p.read_bytes())
    ix.version = h.hexdigest()[:12]; rag.stale_checked.clear(); ix.stale = False


# 7 notification on decision
def test_user_notified_on_decision(svc):
    o = Q(svc, "recall lot 52525", u="req9")
    run(svc.decide("tenant_pharma_a", "pharm1", o.approval_id, "REJECT", "wrong lot"))
    n = svc.state.notifications("tenant_pharma_a", "req9")
    assert n and "REJECTED" in n[0]["message"] and "wrong lot" in n[0]["message"]
    assert svc.state.notifications("tenant_pharma_b", "req9") == []  # tenant-scoped


# 8 -32008 ABSTAIN and -32004 PII_BLOCK
def test_abstain_error_code_on_wire(rag):
    from tests.test_mcp import rpc, CTX
    r = rpc(rag, "tools/call", {"name": "hybrid_retrieve", "arguments": {"ctx": CTX, "query": "chemotherapy regimen for lung cancer", "score_threshold": 0.42}})
    assert r["error"]["code"] == errors.ABSTAIN and r["error"]["data"]["abstain"] is True


def test_pii_residue_blocked(svc):
    o = Q(svc, "contact bob @ x about ibuprofen")
    assert o.status == "REFUSED" and "PII_BLOCK" in o.answer


# 9 early-abstain calibration recorded
def test_early_abstain_calibrated_and_recorded():
    cal = json.load(open("mcp_servers/early_abstain.json"))
    from mcp_servers import rag_server
    assert (rag_server.EARLY_COS, rag_server.EARLY_BM25) == (cal["cos"], cal["bm25"]) and cal["positives_early_abstained"] == 0


# 13 graphify build / 15 DPI / 18 waterfall start offsets
def test_graph_json_has_code_schemas_and_notes():
    g = json.load(open("graph.json"))
    kinds = {n["kind"] for n in g["_code"].values()}
    assert {"module", "pydantic_schema"} <= kinds and all(n["kind"] == "policy_note" for t in g["tenants"].values() for n in t.values())
    assert "agent.models.HybridRetrieveIn" in g["_code"]


def test_graph_not_serving_code_to_tenants(svc):
    from tests.test_mcp import rpc, CTX
    r = rpc(svc.graph, "tools/call", {"name": "resolve_subgraph", "arguments": {"ctx": CTX, "query": "HybridRetrieveIn schema module"}})["result"]
    assert all(not n["id"].startswith("agent.") for n in r["nodes"])


def test_png_is_300_dpi():
    Image.MAX_IMAGE_PIXELS = None
    dpi = Image.open("docs/workflow_orchestration.png").info.get("dpi")
    assert dpi and round(dpi[0]) == 300


def test_span_start_offsets_recorded(svc):
    o = Q(svc, "Which tablet is listed for heartburn? offsets", u="wf")
    sp = [(n, s0, ms) for t, n, ms, s0 in REC.recent if t == o.trace_id]
    assert len(sp) >= 3 and all(isinstance(s0, float) for _, s0, _ in sp)
