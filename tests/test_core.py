"""Gate 4/9/13 unit tests: repair rules, RBAC, idempotency, loop limits, breaker, audit chain, HITL, isolation."""
import asyncio, json, uuid
import pytest
from agent import errors
from agent.harness import repair_value, stable_uuid4
from agent.models import QueryIn, CallContext
from agent.telemetry import REC
from tests.conftest import ident, TP


from tests.conftest import run


# ---------- harness ----------
def test_repair_rules():
    assert repair_value("sku", "SKU99") == "SKU-099" and repair_value("sku", "sku 99") == "SKU-099"
    assert repair_value("lot", "lot 44") == "LOT-00044" and repair_value("lot", "LOT44") == "LOT-00044"
    assert repair_value("lot", "banana") is None  # ambiguous: never guesses


def test_uuid_v4_enforced():
    with pytest.raises(Exception):
        CallContext(tenant_id="t", user_id="u", session_id=uuid.uuid4(), idempotency_key=uuid.uuid1(), traceparent=TP, tier="TIER_1_READ")
    assert stable_uuid4("a", "b").version == 4


def test_autorepair_then_pending(svc):
    r = run(svc.harness.call("execution", "workflow_transition", {"lot": "lot 44", "from_state": "AVAILABLE", "to_state": "QUARANTINED", "reason": "x"}, ident()))
    assert r["status"] == "PENDING_APPROVAL" and svc.harness.repair_log


def test_subagent_isolation(svc):
    for sub, tool, args in [("retrieval", "workflow_transition", {"lot": "LOT-00044", "from_state": "AVAILABLE", "to_state": "QUARANTINED", "reason": "x"}),
                            ("execution", "hybrid_retrieve", {"query": "x"})]:
        with pytest.raises(errors.McpError) as e:
            run(svc.harness.call(sub, tool, args, ident()))
        assert e.value.code == errors.RBAC_DENIED


def test_caller_cannot_supply_ctx_or_other_tenant(svc):
    with pytest.raises(errors.McpError) as e:
        run(svc.harness.call("retrieval", "hybrid_retrieve", {"query": "x", "ctx": {"tenant_id": "tenant_pharma_b"}}, ident()))
    assert e.value.code == errors.RBAC_DENIED


def test_end_user_cannot_approve(svc):
    with pytest.raises(errors.McpError) as e:
        run(svc.harness.call("approver_api", "approval_decide", {"approval_id": str(uuid.uuid4()), "decision": "APPROVE"}, ident()))
    assert e.value.code == errors.RBAC_DENIED


def test_idempotent_replay(svc):
    a = {"lot": "LOT-00077", "from_state": "AVAILABLE", "to_state": "QUARANTINED", "reason": "x"}
    sid = uuid.uuid4()
    r1 = run(svc.harness.call("execution", "workflow_transition", a, ident(sid=sid)))
    r2 = run(svc.harness.call("execution", "workflow_transition", a, ident(sid=sid)))
    assert r1["approval_id"] == r2["approval_id"] and r2.get("replay")
    assert len(svc.state.pending("tenant_pharma_a")) == 1


# ---------- breaker (KPI-16) ----------
def test_breaker_trips_on_second_identical_failure(svc):
    sid = uuid.uuid4()
    bad = {"lot": "banana", "from_state": "AVAILABLE", "to_state": "QUARANTINED", "reason": "x"}
    codes = []
    for _ in range(6):
        try:
            run(svc.harness.call("execution", "workflow_transition", bad, ident(sid=sid)))
        except errors.McpError as e:
            codes.append(e.code)
    assert codes[0] == errors.INVALID_PARAMS and codes[1:] == [errors.BREAKER_OPEN] * 5  # infinite loop escaped after 2


def test_breaker_at_service_level(svc):
    sid = uuid.uuid4()
    q = "release lot 44 from recalled"  # RECALLED->RELEASED is not an allowed transition: identical failing call
    outs = [run(svc.handle(QueryIn(tenant_id="tenant_pharma_a", user_id="u1", session_id=sid, query=q))) for _ in range(4)]
    assert [o.status for o in outs][0] == "REFUSED" and all(o.status == "HALTED_BREAKER" for o in outs[1:])
    assert "pharmacist" in outs[-1].remediation


# ---------- HITL / audit (KPI-19) ----------
def test_t3_never_executes_without_approval(svc):
    o = run(svc.handle(QueryIn(tenant_id="tenant_pharma_a", user_id="u1", query="Please quarantine lot 12345")))
    assert o.status == "PENDING_APPROVAL" and o.approval_id
    assert svc.state.lot_state("tenant_pharma_a", "LOT-12345") is None  # not executed
    with pytest.raises(errors.McpError):  # requester cannot self-approve even if approver
        svc.state.repo.set_role("tenant_pharma_a", "u1", "APPROVER")
        run(svc.decide("tenant_pharma_a", "u1", o.approval_id, "APPROVE"))
    svc.state.repo.set_role("tenant_pharma_a", "u1", "END_USER")
    assert svc.state.lot_state("tenant_pharma_a", "LOT-12345") is None
    with pytest.raises(errors.McpError):  # other tenant's approver cannot see/decide it
        run(svc.decide("tenant_pharma_b", "pharmB", o.approval_id, "APPROVE"))
    run(svc.decide("tenant_pharma_a", "pharm1", o.approval_id, "APPROVE"))
    assert svc.state.lot_state("tenant_pharma_a", "LOT-12345") == "QUARANTINED"
    run(svc.drain())
    ok, n = svc.state.repo.verify_chain("tenant_pharma_a")
    assert ok and n >= 3


def test_reject_does_not_execute_and_expiry_never_approves(svc, monkeypatch):
    o = run(svc.handle(QueryIn(tenant_id="tenant_pharma_a", user_id="u2", query="recall lot 22222")))
    run(svc.decide("tenant_pharma_a", "pharm1", o.approval_id, "REJECT"))
    assert svc.state.lot_state("tenant_pharma_a", "LOT-22222") is None
    o2 = run(svc.handle(QueryIn(tenant_id="tenant_pharma_a", user_id="u2", query="quarantine lot 33333")))
    assert svc.state.expire_stale("tenant_pharma_a", now=9e12) == 1
    with pytest.raises(errors.McpError):
        run(svc.decide("tenant_pharma_a", "pharm1", o2.approval_id, "APPROVE"))
    assert svc.state.lot_state("tenant_pharma_a", "LOT-33333") is None


def test_audit_tamper_detected(svc):
    for i in range(3):
        svc.state.repo.audit_append("tenant_pharma_a", {"kind": f"e{i}"})
    assert svc.state.repo.verify_chain("tenant_pharma_a")[0]
    svc.state.repo.q("UPDATE audit SET payload='{}' WHERE tenant_id='tenant_pharma_a' AND seq=2")
    assert not svc.state.repo.verify_chain("tenant_pharma_a")[0]


def test_t3_refuses_when_store_failover(svc):
    svc.state.read_only = True
    o = run(svc.handle(QueryIn(tenant_id="tenant_pharma_a", user_id="u1", query="quarantine lot 99999")))
    assert o.status != "PENDING_APPROVAL" and not svc.state.pending("tenant_pharma_a")


# ---------- loop limits ----------
def test_loop_budget_halts_not_hangs(svc):
    svc.s.total_loop_budget_ms = 0.0
    o = run(svc.handle(QueryIn(tenant_id="tenant_pharma_a", user_id="u1", query="What is the maximum daily dose of paracetamol?")))
    assert o.status == "ABSTAINED"


def test_max_iterations_constant(svc):
    assert svc.s.max_iterations == 5 and svc.s.breaker_max_identical == 2 and svc.s.score_threshold == 0.42
