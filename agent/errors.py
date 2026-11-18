"""Error-code table (finaldesign.md §3.5). JSON-RPC 2.0 codes + project codes."""
from __future__ import annotations

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
TIMEOUT = -32001
BREAKER_OPEN = -32002
RBAC_DENIED = -32003
PII_BLOCK = -32004
PENDING_APPROVAL = -32005
IDEMPOTENT_REPLAY = -32006
INJECTION_BLOCKED = -32007
ABSTAIN = -32008

NAMES = {
    PARSE_ERROR: "PARSE_ERROR", INVALID_REQUEST: "INVALID_REQUEST", METHOD_NOT_FOUND: "METHOD_NOT_FOUND",
    INVALID_PARAMS: "INVALID_PARAMS", INTERNAL_ERROR: "INTERNAL_ERROR", TIMEOUT: "TIMEOUT",
    BREAKER_OPEN: "BREAKER_OPEN", RBAC_DENIED: "RBAC_DENIED", PII_BLOCK: "PII_BLOCK",
    PENDING_APPROVAL: "PENDING_APPROVAL", IDEMPOTENT_REPLAY: "IDEMPOTENT_REPLAY",
    INJECTION_BLOCKED: "INJECTION_BLOCKED", ABSTAIN: "ABSTAIN",
}


class McpError(Exception):
    def __init__(self, code: int, message: str = "", data: dict | None = None):
        self.code, self.message, self.data = code, message or NAMES.get(code, "ERROR"), data or {}
        super().__init__(f"{code} {NAMES.get(code, '')}: {self.message}")

    def to_json(self) -> dict:
        return {"code": self.code, "message": self.message, "data": self.data}


class ServiceUnavailable(Exception):
    """FMEA #5 terminal fallback: LLM 429/5xx and no extractive answer => HTTP 503 + Retry-After."""
    def __init__(self, retry_after: int = 1):
        self.retry_after = retry_after
        super().__init__("service unavailable")
