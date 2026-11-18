"""Gate 17 `graphify build <code dirs...> ./knowledge_vault --output graph.json` equivalent (the `graphify` CLI is not installed).
Builds ONE structural dependency graph of: Python modules (import edges), Pydantic schemas (classes deriving from Base, with field names),
and Obsidian policy notes ([[wikilink]] edges), per tenant namespace. Runtime traversal (mcp-graphify-vault) is tenant-scoped and
only ever returns that tenant's note nodes: code nodes live in the shared `_code` namespace and are never served to tenants.
Usage: python3 tools/graphify_build.py agent mcp_servers plugins ./knowledge_vault --output graph.json"""
import argparse, ast, json, re, sys
from pathlib import Path
sys.path.insert(0, ".")

ap = argparse.ArgumentParser(); ap.add_argument("paths", nargs="+"); ap.add_argument("--output", default="graph.json")
a = ap.parse_args()
vault = [Path(p) for p in a.paths if "vault" in p]
code = [Path(p) for p in a.paths if "vault" not in p]
g = {"_code": {}, "tenants": {}}
mods = {}
for d in code:
    for f in sorted(d.rglob("*.py")):
        mod = ".".join(f.with_suffix("").parts)
        tree = ast.parse(f.read_text())
        imports = sorted({n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module} | {x.name for n in ast.walk(tree) if isinstance(n, ast.Import) for x in n.names})
        mods[mod] = {"id": mod, "kind": "module", "path": str(f), "links": [i for i in imports if i.split(".")[0] in {c.name for c in code}]}
        for c in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
            bases = [getattr(b, "id", getattr(b, "attr", "")) for b in c.bases]
            if any(b in ("Base", "BaseModel") for b in bases):
                fields = [t.target.id for t in c.body if isinstance(t, ast.AnnAssign) and isinstance(t.target, ast.Name)]
                g["_code"][f"{mod}.{c.name}"] = {"id": f"{mod}.{c.name}", "kind": "pydantic_schema", "fields": fields, "links": [mod]}
g["_code"].update(mods)
from mcp_servers.graphify_server import build_graph
for v in vault:
    for tenant, nodes in build_graph(v).items():
        g["tenants"][tenant] = {k: {**n, "kind": "policy_note"} for k, n in nodes.items()}
Path(a.output).write_text(json.dumps(g, indent=1))
print(f"{a.output}: {sum(1 for n in g['_code'].values() if n['kind']=='module')} modules, "
      f"{sum(1 for n in g['_code'].values() if n['kind']=='pydantic_schema')} schemas, "
      f"{sum(len(v) for v in g['tenants'].values())} policy notes across {len(g['tenants'])} tenants")
