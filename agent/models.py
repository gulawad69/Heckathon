"""Pydantic v2 strict contracts (finaldesign.md §3.1-3.6). All models extra='forbid'."""
from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

SKU = Annotated[str, Field(pattern=r"^SKU-\d{3}$")]
LOT = Annotated[str, Field(pattern=r"^LOT-\d{5}$")]
TenantId = Annotated[str, Field(pattern=r"^[a-z0-9_]{1,64}$")]
Tier = Literal["TIER_1_READ", "TIER_2_COMPUTE", "TIER_3_MUTATE"]
TIER_RANK = {"TIER_1_READ": 1, "TIER_2_COMPUTE": 2, "TIER_3_MUTATE": 3}


class Base(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class CallContext(Base):  # attached by the harness to EVERY tool call
    tenant_id: TenantId
    user_id: str = Field(max_length=64)
    session_id: UUID
    idempotency_key: UUID  # UUIDv4 enforced
    traceparent: str = Field(pattern=r"^00-[0-9a-f]{32}-[0-9a-f]{16}-0[01]$")
    tier: Tier

    @field_validator("idempotency_key")
    @classmethod
    def _v4(cls, v: UUID) -> UUID:
        if v.version != 4:
            raise ValueError("idempotency_key must be UUIDv4")
        return v


# ---- mcp-hybrid-rag ----
class HybridRetrieveIn(Base):
    ctx: CallContext
    query: str = Field(min_length=1, max_length=2000)
    bm25_top_k: int = Field(50, ge=5, le=50)
    dense_top_k: int = Field(50, ge=5, le=50)
    rrf_k: int = Field(60, ge=1, le=200)
    fusion_pool: int = Field(40, ge=4, le=100)
    rerank_k: int = Field(4, ge=1, le=10)
    score_threshold: float = Field(0.42, ge=0.0, le=1.0)


class Chunk(Base):
    chunk_id: str
    chunk_hash: str
    text: str
    source_uri: str
    bm25: float | None = None
    cosine: float | None = None
    rrf_rank: int | None = None
    rerank: float | None = None


class HybridRetrieveOut(Base):
    chunks: list[Chunk]
    top_score: float
    abstain: bool
    stage_ms: dict[str, float]
    early_abstain: bool = False
    degraded: bool = False
    index_version: str = ""
    index_stale: bool = False
    naive_dump_tokens: int = 0  # baseline for KPI-26: tokens of an un-pruned top-10 chunk dump


# ---- mcp-graphify-vault ----
class ResolveSubgraphIn(Base):
    ctx: CallContext
    query: str = Field(min_length=1, max_length=2000)
    anchor_hint: str | None = None
    depth: int = Field(2, ge=1, le=2)
    max_tokens: int = Field(400, ge=1, le=400)


class GraphNode(Base):
    id: str
    domain: str
    risk_tier: int
    version: str
    text: str


class ResolveSubgraphOut(Base):
    anchor_id: str | None
    nodes: list[GraphNode]
    edges: list[list[str]]
    token_estimate: int


# ---- mcp-state-store ----
class CacheGetIn(Base):
    ctx: CallContext
    key_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class CachePutIn(Base):
    ctx: CallContext
    key_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    value: str
    ttl_s: int = Field(300, ge=255, le=345)


class SessionFactsGetIn(Base):
    ctx: CallContext
    limit: int = Field(50, ge=1, le=50)


class SessionFactsPutIn(Base):
    ctx: CallContext
    text: str = Field(max_length=500)
    evidence_chunk_ids: list[str]


class AuditEvent(Base):
    kind: str = Field(max_length=64)
    detail: dict[str, Any] = Field(default_factory=dict)


class AuditAppendIn(Base):
    ctx: CallContext
    event: AuditEvent


class WorkflowTransitionIn(Base):
    ctx: CallContext
    lot: LOT
    from_state: str = Field(max_length=32)
    to_state: str = Field(max_length=32)
    reason: str = Field(max_length=300)


class ApprovalDecideIn(Base):
    ctx: CallContext
    approval_id: UUID
    decision: Literal["APPROVE", "REJECT"]
    comment: str = Field("", max_length=300)


# ---- Public API ----
class QueryIn(Base):
    tenant_id: TenantId
    user_id: str = Field(max_length=64)
    query: str = Field(max_length=4000)
    session_id: UUID | None = None


Status = Literal["ANSWERED", "ABSTAINED", "REFUSED", "PENDING_APPROVAL", "HALTED_BREAKER"]


class QueryOut(Base):
    tenant_id: TenantId
    session_id: UUID
    trace_id: str
    status: Status
    answer: str | None = None
    citations: list[str] = Field(default_factory=list)
    faithfulness: float | None = None
    approval_id: UUID | None = None
    remediation: str | None = None
