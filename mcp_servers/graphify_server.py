"""mcp-graphify-vault (TIER_1_READ): resolve the query anchor note and traverse <= depth 2 wikilink edges, returning only
the minimal connected subgraph (finaldesign.md §5). `graphify build` equivalent: tools/graphify_build.py -> graph.json.
The real `graphify` CLI is not installed here; this module implements the same contract (anchor + bounded traversal)."""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _P

_sys.path.insert(0, str(_P(__file__).resolve().parents[1]))  # repo root, so servers run standalone over stdio/HTTP

import json
import os
import re
from pathlib import Path

from agent import errors
from agent.models import GraphNode, ResolveSubgraphIn, ResolveSubgraphOut
from mcp_servers.base import McpServerBase, serve_stdio
from mcp_servers.rag_server import VAULT, parse_note, tokenize


def est_tokens(s: str) -> int:
    return max(1, int(len(s.split()) * 1.3))


def build_graph(vault: Path = VAULT) -> dict:
    g: dict = {}
    for tdir in sorted(p for p in vault.iterdir() if p.is_dir()):
        nodes = {}
        for p in sorted(tdir.glob("*.md")):
            meta, body = parse_note(p)
            nid = meta.get("id", p.stem)
            nodes[nid] = {"id": nid, "domain": meta.get("domain", ""), "risk_tier": int(meta.get("risk_tier", 1)),
                          "version": meta.get("version", ""), "text": re.sub(r"\n> DEMO.*$", "", body, flags=re.S).strip(),
                          "links": re.findall(r"\[\[([A-Za-z0-9_\-]+)\]\]", body)}
        g[tdir.name] = nodes
    return g


class GraphifyServer(McpServerBase):
    name = "graphify-vault"

    def __init__(self, graph: dict | None = None):
        super().__init__()
        self.graph = graph or self._load()
        self.register("resolve_subgraph", ResolveSubgraphIn, self.resolve_subgraph, "TIER_1_READ")

    @staticmethod
    def _load() -> dict:
        """Use the prebuilt graph.json (tools/graphify_build.py) only if its note ids/versions match the vault on disk; else build live."""
        live = build_graph()
        p = Path("graph.json")
        try:
            built = json.loads(p.read_text()).get("tenants", {})
            same = {t: {k: (n["id"], n["version"], n["text"]) for k, n in nodes.items()} for t, nodes in built.items()} == \
                   {t: {k: (n["id"], n["version"], n["text"]) for k, n in nodes.items()} for t, nodes in live.items()}
            return built if same else live
        except Exception:
            return live

    def resolve_subgraph(self, a: ResolveSubgraphIn) -> ResolveSubgraphOut:
        nodes = self.graph.get(a.ctx.tenant_id)
        if nodes is None:
            raise errors.McpError(errors.RBAC_DENIED, "unknown tenant namespace")
        qt = set(tokenize(a.query))
        anchor = a.anchor_hint if a.anchor_hint in nodes else None
        if not anchor:  # lexical anchor resolution over title/id/body
            best, bs = None, 0.0
            for nid, n in nodes.items():
                nt = set(tokenize(nid.replace("-", " ") + " " + n["text"]))
                idt = set(tokenize(nid.replace("-", " ")))
                s = len(qt & nt) / (len(qt) or 1) + 2 * len(qt & idt)
                if s > bs:
                    best, bs = nid, s
            anchor = best if bs > 0 else None
        if not anchor:
            return ResolveSubgraphOut(anchor_id=None, nodes=[], edges=[], token_estimate=0)
        seen, frontier, edges = {anchor}, [anchor], []
        for _ in range(a.depth):
            nxt = []
            for nid in frontier:
                for dst in nodes[nid]["links"]:
                    if dst in nodes:
                        edges.append([nid, dst])
                        if dst not in seen:
                            seen.add(dst); nxt.append(dst)
            frontier = nxt
        out, used = [], 0
        for nid in [anchor] + [n for n in seen if n != anchor]:  # anchor first; stop at max_tokens
            n = nodes[nid]; t = est_tokens(n["text"])
            if used + t > a.max_tokens and out:
                break
            out.append(GraphNode(id=nid, domain=n["domain"], risk_tier=n["risk_tier"], version=n["version"], text=n["text"]))
            used += t
        kept = {n.id for n in out}
        return ResolveSubgraphOut(anchor_id=anchor, nodes=out, edges=[e for e in edges if e[0] in kept and e[1] in kept], token_estimate=used)


if __name__ == "__main__":
    serve_stdio(GraphifyServer())
