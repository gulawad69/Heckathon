"""FastAPI app (finaldesign.md §3.6): POST /api/v1/agent/query, SSE /query/stream, approvals API, /health.
X-Tenant-ID must equal body tenant_id else 403. Identity (X-User-ID for the approver API) is asserted by the gateway
(NGINX/Envoy auth); this service never trusts a tenant it did not receive from the gateway header."""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError

from agent import errors
from agent.models import QueryIn, QueryOut
from agent.service import AgentService
from agent.telemetry import REC


def create_app(service: AgentService | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        app.state.svc = service or AgentService(db_path="state.db")

        async def monitor():  # housekeeping always; incident automation unless INCIDENT_AUTOMATION=0
            import os
            while True:
                await asyncio.sleep(5)
                try:
                    await app.state.svc.housekeeping()
                    if os.environ.get("INCIDENT_AUTOMATION", "1") != "0":
                        app.state.svc.incident_tick()
                except Exception:
                    pass
        task = asyncio.create_task(monitor())
        yield
        task.cancel()
        await app.state.svc.drain()

    app = FastAPI(title="Pharma Formulary Agent", lifespan=lifespan)

    @app.get("/health")
    async def health():  # reserved pool: never starved by retrieval load
        svc = app.state.svc
        async with svc.harness.pools["reserved"]:
            return {"status": "ok", "mcp": await svc.host.status()}

    async def parse(request: Request, x_tenant_id: str | None) -> QueryIn:
        try:
            q = QueryIn.model_validate_json(await request.body())
        except ValidationError:
            raise HTTPException(422, "invalid request")
        if x_tenant_id != q.tenant_id:
            raise HTTPException(403, "X-Tenant-ID must equal body tenant_id")
        return q

    @app.post("/api/v1/agent/query", response_model=QueryOut)
    async def query(request: Request, x_tenant_id: str | None = Header(None)):
        q = await parse(request, x_tenant_id)
        try:
            out = await app.state.svc.handle(q)
        except errors.ServiceUnavailable as e:
            return JSONResponse({"detail": "service unavailable"}, status_code=503, headers={"Retry-After": str(e.retry_after)})
        code = 202 if out.status == "PENDING_APPROVAL" else 200
        return JSONResponse(out.model_dump(mode="json"), status_code=code)

    @app.post("/api/v1/agent/query/stream")
    async def stream(request: Request, x_tenant_id: str | None = Header(None)):
        q = await parse(request, x_tenant_id)
        t0 = time.perf_counter()

        async def gen():
            try:
                out = await app.state.svc.handle(q)  # verification precedes the first token: only grounded text is streamed
            except errors.ServiceUnavailable:
                yield "event: error\ndata: {\"detail\": \"service unavailable\", \"retry_after\": 1}\n\n"
                return
            words = (out.answer or "").split(" ")
            first = True
            for i in range(0, len(words), 4):
                if first:
                    REC.observe("ttft", (time.perf_counter() - t0) * 1000)  # KPI-25
                    first = False
                yield f"data: {json.dumps({'delta': ' '.join(words[i:i+4]) + ' '})}\n\n"
            yield f"event: done\ndata: {out.model_dump_json()}\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.get("/api/v1/notifications")
    async def notifications(x_tenant_id: str = Header(...), x_user_id: str = Header(...), mark_read: bool = False):
        return app.state.svc.state.notifications(x_tenant_id, x_user_id, mark_read)

    @app.get("/api/v1/approvals")
    async def approvals(x_tenant_id: str = Header(...), x_user_id: str = Header(...)):
        svc = app.state.svc
        if svc.state.repo.role(x_tenant_id, x_user_id) != "APPROVER":
            raise HTTPException(403, "approver role required")
        return [dict(zip(("approval_id", "requester", "lot", "from_state", "to_state", "reason", "status"), r)) for r in svc.state.pending(x_tenant_id)]

    @app.post("/api/v1/approvals/{approval_id}/decide")
    async def decide(approval_id: uuid.UUID, body: dict, x_tenant_id: str = Header(...), x_user_id: str = Header(...)):
        try:
            return await app.state.svc.decide(x_tenant_id, x_user_id, approval_id, body.get("decision", ""), body.get("comment", ""))
        except errors.McpError as e:
            raise HTTPException(403 if e.code == errors.RBAC_DENIED else 400, e.message)

    return app
