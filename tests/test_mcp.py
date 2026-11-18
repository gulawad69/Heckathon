import asyncio, json, os, shutil, uuid
import pytest
from agent import errors
from agent.mcp_host import verify_manifests, sign_manifests, SERVER_FILES, StdioTransport
from tests.conftest import TP

from tests.conftest import run
CTX = {"tenant_id": "tenant_pharma_a", "user_id": "u", "session_id": str(uuid.uuid4()), "idempotency_key": str(uuid.uuid4()), "traceparent": TP, "tier": "TIER_1_READ"}

def rpc(server, method, params, id_=1):
    return json.loads(run(server.dispatch(json.dumps({"jsonrpc": "2.0", "id": id_, "method": method, "params": params}))))

def test_jsonrpc_error_codes(rag):
    assert json.loads(run(rag.dispatch("not json")))["error"]["code"] == errors.PARSE_ERROR
    assert json.loads(run(rag.dispatch('{"id":1}')))["error"]["code"] == errors.INVALID_REQUEST
    assert rpc(rag, "tools/call", {"name": "nope", "arguments": {}})["error"]["code"] == errors.METHOD_NOT_FOUND
    assert rpc(rag, "tools/call", {"name": "hybrid_retrieve", "arguments": {"query": "x"}})["error"]["code"] == errors.INVALID_PARAMS
    bad = {**CTX, "idempotency_key": str(uuid.uuid1())}
    assert rpc(rag, "tools/call", {"name": "hybrid_retrieve", "arguments": {"ctx": bad, "query": "x"}})["error"]["code"] == errors.INVALID_PARAMS
    extra = {"ctx": CTX, "query": "x", "evil": 1}
    assert rpc(rag, "tools/call", {"name": "hybrid_retrieve", "arguments": extra})["error"]["code"] == errors.INVALID_PARAMS  # extra=forbid

def test_server_enforces_tier(svc):
    a = {"ctx": CTX, "lot": "LOT-00001", "from_state": "AVAILABLE", "to_state": "QUARANTINED", "reason": "x"}
    assert rpc(svc.state, "tools/call", {"name": "workflow_transition", "arguments": a})["error"]["code"] == errors.RBAC_DENIED  # T1 call into T3 tool

def test_cross_tenant_chunk_denied(rag):
    with pytest.raises(errors.McpError) as e:
        rag.get_chunk("tenant_pharma_a", "FORM-ASPIRIN-B#0")
    assert e.value.code == errors.RBAC_DENIED

def test_unknown_tenant_namespace(rag):
    a = {"ctx": {**CTX, "tenant_id": "tenant_nope"}, "query": "x"}
    assert rpc(rag, "tools/call", {"name": "hybrid_retrieve", "arguments": a})["error"]["code"] == errors.RBAC_DENIED

def test_manifest_tamper_refused(tmp_path):
    sign_manifests()
    verify_manifests()
    f = tmp_path / "rag.py"; shutil.copy(SERVER_FILES["hybrid-rag"], f); f.write_text(f.read_text() + "\n# tampered")
    with pytest.raises(RuntimeError):
        verify_manifests(files={"hybrid-rag": str(f)})
    with pytest.raises(RuntimeError):
        verify_manifests(key=b"wrong-key")

def test_graphify_depth_and_tokens(svc):
    a = {"ctx": CTX, "query": "ibuprofen safety", "depth": 2, "max_tokens": 400}
    r = rpc(svc.graph, "tools/call", {"name": "resolve_subgraph", "arguments": a})["result"]
    assert r["anchor_id"] and r["token_estimate"] <= 400
    assert rpc(svc.graph, "tools/call", {"name": "resolve_subgraph", "arguments": {**a, "depth": 3}})["error"]["code"] == errors.INVALID_PARAMS

def test_stdio_transport_hybrid_rag():
    async def go():
        t = StdioTransport("python3", ["mcp_servers/rag_server.py"])
        await t.start()
        try:
            r = await t.call("hybrid_retrieve", {"ctx": CTX, "query": "maximum daily dose of paracetamol", "score_threshold": 0.42})
        finally:
            await t.stop()
        return r
    r = run(asyncio.wait_for(go(), 120))
    assert r["chunks"][0]["chunk_id"] == "FORM-PARACETAMOL#0"
