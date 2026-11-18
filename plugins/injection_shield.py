"""injection_shield (PRE p20, fail-closed). Direct jailbreak / prompt-extraction / tool-arg overflow / SQLi.
Indirect injection inside retrieved chunks is handled by `scan_chunk` (called by the Retrieval Subagent)."""
from __future__ import annotations

import re

from plugins.base import HookResult, Plugin, hookimpl
from plugins.pii_redactor import normalize

_DIRECT = [
    r"ignore (?:all |any )?(?:the )?(?:previous|prior|above|earlier) (?:instructions|rules|prompts?)",
    r"disregard (?:all |any )?(?:the )?(?:previous|prior|above|system)",
    r"(?:reveal|show|print|repeat|output|leak|dump)\b.{0,40}\b(?:system prompt|hidden prompt|initial prompt|your instructions|developer message)",
    r"\b(?:system prompt|developer message)\b.{0,30}\b(?:verbatim|word for word|exactly)",
    r"you are now\b.{0,40}\b(?:dan|jailbroken|unrestricted|developer mode|no (?:rules|restrictions))",
    r"\b(?:dan mode|developer mode|jailbreak|do anything now)\b",
    r"pretend (?:you (?:have|are)|there (?:is|are)) no (?:rules|restrictions|guidelines|policy)",
    r"(?:bypass|disable|turn off|override)\b.{0,30}\b(?:safety|guardrails?|filters?|policy|approval|hitl|rbac)",
    r"act as (?:an? )?(?:unrestricted|uncensored|evil)",
    r"(?:ssh|exfiltrate|send|post)\b.{0,40}\b(?:to|at) https?://",
    r"</?(?:system|assistant|tool)>|\[/?INST\]|<\|im_(?:start|end)\|>",
    r"as (?:the )?(?:admin|administrator|root|developer|pharmacist in charge)\b.{0,30}\b(?:i authorize|override|approve)",
    r"(?:without|skip|no)\s+(?:the\s+)?(?:human\s+)?(?:approval|review|hitl)",
]
_SQLI = [r"(?:'|\")\s*(?:or|and)\s+['\"\d]", r";\s*(?:drop|delete|update|insert|alter|truncate)\b", r"\bunion\s+select\b",
         r"--\s*$", r"/\*.*\*/", r"\bxp_cmdshell\b", r"\.\./\.\./", r"\$\{jndi:", r"<script\b"]
_RX = [re.compile(p, re.I | re.S) for p in _DIRECT]
_SQLX = [re.compile(p, re.I | re.S) for p in _SQLI]
MAX_TOKEN_RUN = 200  # tool-argument overflow heuristic


def scan(text: str) -> str | None:
    t = normalize(text)
    t = re.sub(r"[​-‏⁠﻿]", "", t)  # zero-width smuggling
    for rx in _RX:
        if rx.search(t):
            return "direct_injection"
    for rx in _SQLX:
        if rx.search(t):
            return "sqli_or_traversal"
    if re.search(r"\S{%d,}" % MAX_TOKEN_RUN, t):
        return "argument_overflow"
    return None


def scan_chunk(text: str) -> bool:
    """True if a retrieved chunk carries an indirect injection (chunk must be dropped, never forwarded)."""
    t = normalize(text)
    return scan(t) is not None or bool(re.search(
        r"(?:note to|instruction for) (?:the )?(?:ai|assistant|model|llm)|when (?:you )?(?:read|see) this", t, re.I))


class InjectionShield(Plugin):
    name = "injection_shield"
    timeout_ms = 15.0

    async def run(self, state):
        hit = scan(state.query)
        if hit:
            return HookResult("BLOCK", code=-32007, reason=hit)
        return HookResult("ALLOW")


class _Impl:
    @hookimpl
    def register_plugins(self):
        return [InjectionShield()]


impl = _Impl()
