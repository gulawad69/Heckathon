"""Sign MCP server manifests (HMAC-SHA256 over each server file). Key from MCP_MANIFEST_KEY (secret manager).
Usage: MCP_MANIFEST_KEY=... python3 tools/sign_manifests.py"""
import sys
sys.path.insert(0, ".")
from agent.mcp_host import sign_manifests
print(sign_manifests())
