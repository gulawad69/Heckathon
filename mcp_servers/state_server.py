"""mcp-state-store: cache, session facts, hash-chained audit, T3 workflow + approvals (finaldesign.md §3.4, §11).
Local backend = SQLite (design Gate 13 allows SQLite/Postgres) behind a Repository layer that REQUIRES tenant_id on
every statement (RLS equivalent). A Postgres+RLS backend is a drop-in for `Repository` (not exercised here).
workflow_transition NEVER executes: it writes PENDING_APPROVAL. Only approval_decide(APPROVE) by an Approver of the
same tenant executes, and the approver may not be the requester."""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _P

_sys.path.insert(0, str(_P(__file__).resolve().parents[1]))  # repo root, so servers run standalone over stdio/HTTP

import asyncio
import hashlib
import json
import os
import random
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

from agent import errors
from agent.models import (ApprovalDecideIn, AuditAppendIn, CacheGetIn, CachePutIn, SessionFactsGetIn,
                          SessionFactsPutIn, WorkflowTransitionIn)
from mcp_servers.base import McpServerBase

GENESIS = "0" * 64
LOT_STATES = {"AVAILABLE", "QUARANTINED", "RELEASED", "RECALLED"}
ALLOWED = {("AVAILABLE", "QUARANTINED"), ("QUARANTINED", "RELEASED"), ("QUARANTINED", "RECALLED"),
           ("AVAILABLE", "RECALLED"), ("RELEASED", "QUARANTINED"), ("RELEASED", "RECALLED")}
APPROVAL_TTL_S = int(os.environ.get("APPROVAL_TTL_S", str(24 * 3600)))


def canonical(o) -> str:
    return json.dumps(o, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


class Repository:
    """Every method takes tenant_id first and puts it in the WHERE clause."""

    def __init__(self, path: str = ":memory:"):
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.lock = threading.RLock()
        with self.lock:
            self.db.executescript("""
            CREATE TABLE IF NOT EXISTS audit(tenant_id TEXT NOT NULL, seq INTEGER NOT NULL, prev_hash TEXT NOT NULL,
                hash TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(tenant_id, seq));
            CREATE TABLE IF NOT EXISTS session_facts(tenant_id TEXT NOT NULL, session_id TEXT NOT NULL, fact_id TEXT NOT NULL,
                text TEXT NOT NULL, evidence TEXT NOT NULL, verified_at TEXT NOT NULL, PRIMARY KEY(tenant_id, fact_id));
            CREATE TABLE IF NOT EXISTS approvals(tenant_id TEXT NOT NULL, approval_id TEXT NOT NULL, requester TEXT NOT NULL,
                lot TEXT NOT NULL, from_state TEXT NOT NULL, to_state TEXT NOT NULL, reason TEXT NOT NULL,
                status TEXT NOT NULL, created_at REAL NOT NULL, decided_by TEXT, comment TEXT, PRIMARY KEY(tenant_id, approval_id));
            CREATE TABLE IF NOT EXISTS lots(tenant_id TEXT NOT NULL, lot TEXT NOT NULL, state TEXT NOT NULL, PRIMARY KEY(tenant_id, lot));
            CREATE TABLE IF NOT EXISTS users(tenant_id TEXT NOT NULL, user_id TEXT NOT NULL, role TEXT NOT NULL, PRIMARY KEY(tenant_id, user_id));
            CREATE TABLE IF NOT EXISTS idem(tenant_id TEXT NOT NULL, key TEXT NOT NULL, result TEXT NOT NULL, PRIMARY KEY(tenant_id, key));
            CREATE TABLE IF NOT EXISTS notifications(tenant_id TEXT NOT NULL, id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,
                approval_id TEXT NOT NULL, message TEXT NOT NULL, created_at REAL NOT NULL, read INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS escalations(tenant_id TEXT NOT NULL, approval_id TEXT NOT NULL, at REAL NOT NULL, PRIMARY KEY(tenant_id, approval_id));
            CREATE TABLE IF NOT EXISTS outbox(tenant_id TEXT NOT NULL, id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL);
            """)

    def q(self, sql, args=()):
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    # ---- users / roles ----
    def role(self, tenant_id, user_id) -> str:
        r = self.q("SELECT role FROM users WHERE tenant_id=? AND user_id=?", (tenant_id, user_id))
        return r[0][0] if r else "END_USER"

    def set_role(self, tenant_id, user_id, role):
        self.q("INSERT OR REPLACE INTO users VALUES(?,?,?)", (tenant_id, user_id, role))

    # ---- audit chain: hash_n = SHA256(hash_{n-1} || canonical_json(event_n)), per-tenant, fixed genesis ----
    def audit_append(self, tenant_id, event: dict) -> tuple[int, str]:
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                r = self.db.execute("SELECT seq, hash FROM audit WHERE tenant_id=? ORDER BY seq DESC LIMIT 1", (tenant_id,)).fetchone()
                seq, prev = (r[0] + 1, r[1]) if r else (1, GENESIS)
                h = hashlib.sha256((prev + canonical(event)).encode()).hexdigest()
                self.db.execute("INSERT INTO audit VALUES(?,?,?,?,?)", (tenant_id, seq, prev, h, canonical(event)))
                self.db.execute("COMMIT")
                return seq, h
            except Exception:
                self.db.execute("ROLLBACK")
                raise

    def verify_chain(self, tenant_id) -> tuple[bool, int]:
        prev, n = GENESIS, 0
        for seq, ph, h, payload in self.q("SELECT seq, prev_hash, hash, payload FROM audit WHERE tenant_id=? ORDER BY seq", (tenant_id,)):
            if ph != prev or hashlib.sha256((prev + payload).encode()).hexdigest() != h or seq != n + 1:
                return False, n
            prev, n = h, n + 1
        return True, n

    def audit_rows(self, tenant_id, limit=50):
        return self.q("SELECT seq, hash, payload FROM audit WHERE tenant_id=? ORDER BY seq DESC LIMIT ?", (tenant_id, limit))


class StateServer(McpServerBase):
    name = "state-store"

    def __init__(self, db_path: str = ":memory:"):
        super().__init__()
        self.repo = Repository(db_path)
        self.cache: dict[str, tuple[float, str]] = {}  # key = t:{tenant}:{hash}
        self.cache_cos: dict[str, list] = {}
        self.read_only = False  # Postgres-failover simulation (FMEA #6): T3 refuses
        R = self.register
        R("cache_get", CacheGetIn, self.cache_get, "TIER_1_READ")
        R("cache_put", CachePutIn, self.cache_put, "TIER_1_READ")
        R("session_facts_get", SessionFactsGetIn, self.facts_get, "TIER_1_READ")
        R("session_facts_put", SessionFactsPutIn, self.facts_put, "TIER_2_COMPUTE")
        R("audit_append", AuditAppendIn, self.audit_append, "system")
        R("workflow_transition", WorkflowTransitionIn, self.workflow_transition, "TIER_3_MUTATE")
        R("approval_decide", ApprovalDecideIn, self.approval_decide, "TIER_3_MUTATE")

    # tenant-scoped key; the 64-hex hash already folds tenant:user:query (ADR-3)
    @staticmethod
    def _k(ctx, key_hash): return f"t:{ctx.tenant_id}:{key_hash}"

    def cache_get(self, a: CacheGetIn):
        k = self._k(a.ctx, a.key_hash)
        v = self.cache.get(k)
        if v and v[0] > time.time():
            return {"hit": True, "value": v[1]}
        self.cache.pop(k, None)
        return {"hit": False}

    def cache_put(self, a: CachePutIn):
        # ttl_s is already jittered by the caller (300 +/- 45 s => 255..345, validated by the schema)
        self.cache[self._k(a.ctx, a.key_hash)] = (time.time() + a.ttl_s, a.value)
        return {"ok": True}

    def facts_get(self, a: SessionFactsGetIn):
        rows = self.repo.q("SELECT fact_id, text, verified_at FROM session_facts WHERE tenant_id=? AND session_id=? ORDER BY verified_at DESC LIMIT ?",
                           (a.ctx.tenant_id, str(a.ctx.session_id), a.limit))
        return {"facts": [{"fact_id": r[0], "text": r[1], "verified_at": r[2]} for r in rows]}

    def facts_put(self, a: SessionFactsPutIn):
        if not a.evidence_chunk_ids:
            raise errors.McpError(errors.INVALID_PARAMS, "facts need evidence (verified facts only)")
        fid = str(uuid.uuid4())
        self.repo.q("INSERT INTO session_facts VALUES(?,?,?,?,?,?)", (a.ctx.tenant_id, str(a.ctx.session_id), fid, a.text,
                    json.dumps(a.evidence_chunk_ids), datetime.now(timezone.utc).isoformat()))
        return {"fact_id": fid}

    def audit_append(self, a: AuditAppendIn):
        seq, h = self.repo.audit_append(a.ctx.tenant_id, {"kind": a.event.kind, "detail": a.event.detail,
                                                          "user": a.ctx.user_id, "trace": a.ctx.traceparent.split("-")[1]})
        return {"seq": seq, "hash": h}

    def _audit(self, ctx, kind, **detail):
        return self.repo.audit_append(ctx.tenant_id, {"kind": kind, "detail": detail, "user": ctx.user_id,
                                                      "trace": ctx.traceparent.split("-")[1]})

    def workflow_transition(self, a: WorkflowTransitionIn):
        c = a.ctx
        if self.read_only:  # FMEA #6: fail closed
            raise errors.McpError(errors.INTERNAL_ERROR, "state store failover: T3 refused (fail closed)")
        prior = self.repo.q("SELECT result FROM idem WHERE tenant_id=? AND key=?", (c.tenant_id, str(c.idempotency_key)))
        if prior:  # -32006 IDEMPOTENT_REPLAY: return original result
            return {**json.loads(prior[0][0]), "replay": True}
        if (a.from_state, a.to_state) not in ALLOWED or a.from_state not in LOT_STATES:
            raise errors.McpError(errors.INVALID_PARAMS, f"transition {a.from_state}->{a.to_state} not allowed")
        aid = str(uuid.uuid4())
        self.repo.q("INSERT INTO approvals VALUES(?,?,?,?,?,?,?,?,?,NULL,NULL)", (c.tenant_id, aid, c.user_id, a.lot, a.from_state,
                    a.to_state, a.reason, "PENDING_APPROVAL", time.time()))
        res = {"status": "PENDING_APPROVAL", "approval_id": aid}
        self.repo.q("INSERT INTO idem VALUES(?,?,?)", (c.tenant_id, str(c.idempotency_key), json.dumps(res)))
        self._audit(c, "T3_REQUESTED", approval_id=aid, lot=a.lot, to=a.to_state)
        return res

    def expire_stale(self, tenant_id: str, now: float | None = None) -> int:
        now = now or time.time()
        rows = self.repo.q("SELECT approval_id FROM approvals WHERE tenant_id=? AND status='PENDING_APPROVAL' AND created_at<?",
                           (tenant_id, now - APPROVAL_TTL_S))
        for (aid,) in rows:  # expire to REJECTED, never auto-approve
            self.repo.q("UPDATE approvals SET status='REJECTED', comment='expired' WHERE tenant_id=? AND approval_id=?", (tenant_id, aid))
            who = self.repo.q("SELECT requester FROM approvals WHERE tenant_id=? AND approval_id=?", (tenant_id, aid))
            if who: self.notify(tenant_id, who[0][0], aid, "Your approval request expired and was REJECTED (never auto-approved).")
            self.repo.audit_append(tenant_id, {"kind": "T3_EXPIRED", "detail": {"approval_id": aid}, "user": "system", "trace": ""})
        return len(rows)

    def approval_decide(self, a: ApprovalDecideIn):
        c = a.ctx
        if self.read_only:
            raise errors.McpError(errors.INTERNAL_ERROR, "state store failover: T3 refused (fail closed)")
        if self.repo.role(c.tenant_id, c.user_id) != "APPROVER":
            self._audit(c, "T3_DECIDE_DENIED", approval_id=str(a.approval_id))
            raise errors.McpError(errors.RBAC_DENIED, "approver role required")
        self.expire_stale(c.tenant_id)
        r = self.repo.q("SELECT requester, lot, from_state, to_state, status FROM approvals WHERE tenant_id=? AND approval_id=?",
                        (c.tenant_id, str(a.approval_id)))
        if not r:
            raise errors.McpError(errors.RBAC_DENIED, "approval not found in tenant")  # cross-tenant ids look absent
        requester, lot, fr, to, status = r[0]
        if status != "PENDING_APPROVAL":
            raise errors.McpError(errors.INVALID_PARAMS, f"approval already {status}")
        if requester == c.user_id:
            self._audit(c, "T3_SELF_APPROVAL_DENIED", approval_id=str(a.approval_id))
            raise errors.McpError(errors.RBAC_DENIED, "requester cannot approve own request")
        new = "APPROVED" if a.decision == "APPROVE" else "REJECTED"
        self.repo.q("UPDATE approvals SET status=?, decided_by=?, comment=? WHERE tenant_id=? AND approval_id=?",
                    (new, c.user_id, a.comment, c.tenant_id, str(a.approval_id)))
        if a.decision == "APPROVE":  # execute only if APPROVE
            self.repo.q("INSERT OR REPLACE INTO lots VALUES(?,?,?)", (c.tenant_id, lot, to))
        self._audit(c, f"T3_{new}", approval_id=str(a.approval_id), lot=lot, to=to if a.decision == "APPROVE" else fr)
        self.notify(c.tenant_id, requester, str(a.approval_id),  # §2.F: S-->>U: notification
                    f"Your request for {lot} ({fr} -> {to}) was {new} by an approver." + (f" Comment: {a.comment}" if a.comment else ""))
        return {"status": new}

    # ---- notifications (§2.F), escalation (FMEA #10), outbox replay (FMEA #6), namespace purge (FMEA #11) ----
    def notify(self, tenant_id, user_id, approval_id, message):
        self.repo.q("INSERT INTO notifications(tenant_id, user_id, approval_id, message, created_at) VALUES(?,?,?,?,?)",
                    (tenant_id, user_id, approval_id, message, time.time()))

    def notifications(self, tenant_id, user_id, mark_read=False):
        rows = self.repo.q("SELECT id, approval_id, message, created_at, read FROM notifications WHERE tenant_id=? AND user_id=? ORDER BY id DESC LIMIT 50", (tenant_id, user_id))
        if mark_read:
            self.repo.q("UPDATE notifications SET read=1 WHERE tenant_id=? AND user_id=?", (tenant_id, user_id))
        return [{"approval_id": r[1], "message": r[2], "created_at": r[3], "read": bool(r[4])} for r in rows]

    def escalate_stale(self, tenant_id: str, sla_s: float | None = None, now: float | None = None) -> int:
        """Pending older than the reviewer SLA (default 50% of the TTL): escalate once (audit + notify approvers). Stays PENDING; never auto-approves."""
        now, sla = now or time.time(), sla_s if sla_s is not None else APPROVAL_TTL_S / 2
        rows = self.repo.q("SELECT approval_id, lot FROM approvals WHERE tenant_id=? AND status='PENDING_APPROVAL' AND created_at<? AND approval_id NOT IN (SELECT approval_id FROM escalations WHERE tenant_id=?)",
                           (tenant_id, now - sla, tenant_id))
        for aid, lot in rows:
            self.repo.q("INSERT INTO escalations VALUES(?,?,?)", (tenant_id, aid, now))
            self.repo.audit_append(tenant_id, {"kind": "T3_ESCALATED", "detail": {"approval_id": aid, "lot": lot}, "user": "system", "trace": ""})
            for (u,) in self.repo.q("SELECT user_id FROM users WHERE tenant_id=? AND role='APPROVER'", (tenant_id,)):
                self.notify(tenant_id, u, aid, f"ESCALATION: approval for {lot} has been pending beyond the reviewer SLA.")
        return len(rows)

    def drain_outbox(self) -> int:
        """Replay audit events that could not be appended (durable outbox); delete only after a successful append."""
        n = 0
        for oid, tenant, payload in self.repo.q("SELECT id, tenant_id, payload FROM outbox ORDER BY id"):
            try:
                self.repo.audit_append(tenant, json.loads(payload))
            except Exception:
                break  # keep it queued, retry on the next tick
            self.repo.q("DELETE FROM outbox WHERE id=?", (oid,)); n += 1
        return n

    def purge_namespace(self, tenant_id: str) -> int:
        """Cache-stampede / key-collision runbook: drop every cache key of ONE tenant namespace."""
        pre = f"t:{tenant_id}:"
        keys = [k for k in self.cache if k.startswith(pre)]
        for k in keys: self.cache.pop(k, None)
        return len(keys)

    # helpers for UI/tests (not MCP tools)
    def pending(self, tenant_id):
        self.expire_stale(tenant_id)
        return self.repo.q("SELECT approval_id, requester, lot, from_state, to_state, reason, status FROM approvals WHERE tenant_id=? ORDER BY created_at DESC", (tenant_id,))

    def lot_state(self, tenant_id, lot):
        r = self.repo.q("SELECT state FROM lots WHERE tenant_id=? AND lot=?", (tenant_id, lot))
        return r[0][0] if r else None


if __name__ == "__main__":  # Streamable HTTP wrapper (http://localhost:8081/mcp)
    from mcp_servers.base import make_app
    app = make_app(StateServer(os.environ.get("STATE_DB", "state.db")))
    try:
        app.run(transport="streamable-http", host="127.0.0.1", port=8081)  # mcp 2.x
    except TypeError:
        app.settings.port = 8081  # mcp 1.x FastMCP
        app.run(transport="streamable-http")
