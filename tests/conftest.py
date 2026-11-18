import os, sys, uuid
os.environ.update(MCP_MANIFEST_KEY="dev-only-local-key", HF_HUB_OFFLINE="1", RERANK_BUDGET_MS="1500", MCP_RAG_TIMEOUT_MS="2000",
                  STEP_TIMEOUT_MS="2000", LOOP_BUDGET_MS="4000")
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import asyncio
import pytest

LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(LOOP)


def run(c):
    return LOOP.run_until_complete(c)

from agent.harness import Identity
from agent.mcp_host import sign_manifests
from mcp_servers.rag_server import RagServer

TP = "00-" + "a" * 32 + "-" + "b" * 16 + "-01"


@pytest.fixture(scope="session")
def rag():
    sign_manifests()
    return RagServer()


@pytest.fixture
def svc(rag):
    import asyncio
    from agent.service import AgentService
    s = AgentService(rag=rag)
    s.state.repo.set_role("tenant_pharma_a", "pharm1", "APPROVER")
    s.state.repo.set_role("tenant_pharma_b", "pharmB", "APPROVER")
    return s


def ident(tenant="tenant_pharma_a", user="u1", role="END_USER", sid=None):
    return Identity(tenant, user, sid or uuid.uuid4(), role, TP)
