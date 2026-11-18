"""Generate docs/workflow_orchestration.png (Gate 2, 300 DPI) from finaldesign.md section 2.

Usage: python3 -I docs/make_workflow_diagram.py <output_dir>
"""
import sys
from graphviz import Digraph

out_dir = sys.argv[1] if len(sys.argv) > 1 else "."

g = Digraph("workflow_orchestration", format="png", directory=out_dir)
g.attr(rankdir="TB", dpi="300", fontname="Helvetica", fontsize="20", labelloc="t",
       label="The Unreliable Assistant -> Enterprise Agent: Workflow Orchestration",
       nodesep="0.4", ranksep="0.55", compound="true", pad="0.4")
g.attr("node", shape="box", style="rounded,filled", fontname="Helvetica", fontsize="11",
       fillcolor="#ffffff", color="#4a4a4a")
g.attr("edge", fontname="Helvetica", fontsize="9", color="#555555")

# ---- Ingress & Security Layer ----
with g.subgraph(name="cluster_ingress") as c:
    c.attr(label="1. Ingress & Security Layer", style="filled,rounded", fillcolor="#e8f1fb", color="#3b73b9")
    c.node("client", "Client\n(k6 / JMeter / UI)")
    c.node("gw", "NGINX / Envoy\nTLS + rate limit")
    c.node("api", "FastAPI (async, stateless)\nPOST /api/v1/agent/query")
    c.node("pii", "PII Redactor\n(hook.pre, p10, fail-closed)")
    c.node("inj", "Injection Classifier\n(p20, fail-closed)")
    c.node("cache", "Redis Semantic Cache Check\nkey = SHA256(tenant:user:query)\nTTL 300s +/- 45s", shape="diamond", fillcolor="#fff4cc")
    c.edges([("client", "gw"), ("gw", "api"), ("api", "pii"), ("pii", "inj"), ("inj", "cache")])

# ---- Triage & Subagent Routing ----
with g.subgraph(name="cluster_triage") as c:
    c.attr(label="2. Triage & Subagent Routing", style="filled,rounded", fillcolor="#eaf7ea", color="#3d8b3d")
    c.node("triage", "Triage Agent\ntemperature = 0.0, < 25 ms\n(intent, domain, risk_tier, complexity)", fillcolor="#d6f0d6")
    c.node("direct", "Direct Response\n(DIRECT_ANSWER)")
    c.node("oos", "Reject\n(REJECT_OOS)")
    c.node("retr", "Retrieval Subagent\nread-only")
    c.node("exec", "Execution Subagent\nno RAG / no web")
    c.node("ver", "Verification (Audit) Subagent\nNLI claim check, vetoes ungrounded")
    c.edge("triage", "direct", label="DIRECT_ANSWER")
    c.edge("triage", "oos", label="REJECT_OOS")
    c.edge("triage", "retr", label="HYBRID_RAG")
    c.edge("triage", "exec", label="TRANSACTION_EXEC")

# ---- MCP & 3-stage RAG ----
with g.subgraph(name="cluster_mcp") as c:
    c.attr(label="3. MCP (JSON-RPC 2.0) & 3-Stage RAG Pipeline", style="filled,rounded", fillcolor="#f6ecfa", color="#8a4bb0")
    c.node("rag", "mcp-hybrid-rag (stdio, 150 ms)", fillcolor="#e6d3f2")
    c.node("s1", "Stage 1  BM25Okapi\nk1=1.25 b=0.75 top50  < 12 ms")
    c.node("s2", "Stage 2  Bi-Encoder bge-large + FAISS HNSW\nefSearch=64 top50  < 25 ms")
    c.node("rrf", "RRF  k=60\ndedup chunk_hash, pool 40  < 3 ms")
    c.node("s3", "Stage 3  Cross-Encoder bge-reranker-v2-m3\nONNX FP16 batch 32, top 4  < 45 ms")
    c.node("thr", "score >= 0.42 ?", shape="diamond", fillcolor="#fff4cc")
    c.node("gv", "mcp-graphify-vault (stdio)\nObsidian wikilinks, depth <= 2\n~320 vs ~1,400 tokens", fillcolor="#e6d3f2")
    c.node("ss", "mcp-state-store (Streamable HTTP)\nPostgres+pgvector / Redis\naudit, session_facts, approvals", fillcolor="#e6d3f2")
    c.edge("rag", "s1"); c.edge("rag", "s2")
    c.edge("s1", "rrf"); c.edge("s2", "rrf")
    c.edge("rrf", "s3"); c.edge("s3", "thr")

# ---- Control plane & egress ----
with g.subgraph(name="cluster_egress") as c:
    c.attr(label="4. Control Plane & Egress", style="filled,rounded", fillcolor="#fdeee6", color="#c0602b")
    c.node("react", "Bounded ReAct Loop\nmax_steps = 5, budget 450 ms\nINIT>TRIAGE>REASONING>TOOL_CALL>OBSERVATION>VERIFY>DONE/HALTED")
    c.node("cb", "Circuit Breaker\n(tool, args_hash) fails x2 -> HALTED_BREAKER", fillcolor="#ffd9c7")
    c.node("hitl", "Tier 3 HITL Gate\nPENDING_APPROVAL -> reviewer approves", fillcolor="#ffd9c7")
    c.node("llm", "LLM Generate\nvLLM / TRT-LLM", fillcolor="#ffffff")
    c.node("abst", "Deterministic Abstention\n< 90 ms, no LLM", fillcolor="#fff4cc")
    c.node("resp", "Response\n(tenant_id, citations, faithfulness)", fillcolor="#d6f0d6")
    c.node("post", "Post-Execution\nRAGAS Faithfulness >= 0.92 (async)", fillcolor="#ffffff")
    c.node("otel", "OTel Export\nCollector > Prometheus / Tempo / Grafana", fillcolor="#ffffff")
    c.edge("react", "cb"); c.edge("cb", "hitl", label="tier 3")
    c.edge("llm", "ver"); c.edge("resp", "post"); c.edge("post", "otel")

# ---- Cross-cluster flow ----
g.edge("cache", "resp", label="hit", style="dashed")
g.edge("cache", "triage", label="miss")
g.edge("direct", "llm")
g.edge("retr", "rag", label="JSON-RPC")
g.edge("retr", "gv", label="JSON-RPC")
g.edge("exec", "ss", label="JSON-RPC")
g.edge("exec", "react", style="dashed")
g.edge("retr", "react", style="dashed")
g.edge("thr", "llm", label="yes")
g.edge("thr", "abst", label="no")
g.edge("abst", "resp")
g.edge("gv", "llm", style="dashed", label="subgraph")
g.edge("hitl", "ss", label="write PENDING_APPROVAL")
g.edge("ver", "resp", label="grounded")
g.edge("oos", "resp")
g.edge("api", "otel", label="traceparent (all hops)", style="dotted", constraint="false")

path = g.render(cleanup=True)  # writes <name>.gv.png; rename to workflow_orchestration.png
print(path)

# Gate 2: stamp 300 DPI into the PNG metadata (graphviz renders 300 DPI pixels but writes no pHYs tag)
from PIL import Image
Image.MAX_IMAGE_PIXELS = None
_im = Image.open(path); _im.load(); _im.save(path, dpi=(300, 300))
