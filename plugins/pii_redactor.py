"""pii_redactor (PRE p10, fail-closed). Redacts before cache/logs/traces/DB. KPI-02 target recall >= 99.8%.
Handles email, phone, SSN, obfuscated 'name[at]domain', Unicode homoglyphs (NFKC + confusable fold), patient IDs."""
from __future__ import annotations

import re
import unicodedata

from plugins.base import HookResult, Plugin, hookimpl

_CONFUSABLES = str.maketrans({"а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "х": "x", "у": "y", "і": "i",
                              "ѕ": "s", "ј": "j", "Α": "A", "Ε": "E", "Ο": "O", "ο": "o", "ａ": "a"})
_AT = r"(?:@|\s*\[\s*at\s*\]\s*|\s*\(\s*at\s*\)\s*|\s+at\s+)"
_DOT = r"(?:\.|\s*\[\s*dot\s*\]\s*|\s*\(\s*dot\s*\)\s*|\s+dot\s+)"
PATTERNS = [
    ("EMAIL", re.compile(rf"[A-Za-z0-9._%+\-]+(?:{_DOT}[A-Za-z0-9._%+\-]+)*{_AT}[A-Za-z0-9\-]+(?:{_DOT}[A-Za-z0-9\-]+)+", re.I)),
    ("SSN", re.compile(r"\b\d{3}[-\s.]?\d{2}[-\s.]?\d{4}\b")),
    ("SSN_WORDS", re.compile(r"\b(?:ssn|social security(?: number)?)\W{0,6}(?:\w+[\s-]+){2,8}\w+\b", re.I)),
    ("PHONE", re.compile(r"(?<!\w)(?:\+?\d{1,3}[\s.\-]?)?(?:\(\d{3}\)|\d{3})[\s.\-]?\d{3}[\s.\-]?\d{4}(?!\w)")),
    ("PATIENT_ID", re.compile(r"\b(?:MRN|patient\s*id|dob)\s*[:#]?\s*[\w/\-]{4,}\b", re.I)),
    ("CARD", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
]


def normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).translate(_CONFUSABLES)


def redact(text: str) -> tuple[str, list[str]]:
    t = normalize(text)
    found: list[str] = []
    for label, rx in PATTERNS:
        def sub(m, label=label):
            found.append(label)
            return f"[{label}]"
        t = rx.sub(sub, t)
    return t, found


class PiiRedactor(Plugin):
    name = "pii_redactor"
    timeout_ms = 15.0

    async def run(self, state):
        red, found = redact(state.query)
        state.pii_found = found
        if "[" in red and re.search(r"[\u0000-\u0008\u000b\u000c\u000e-\u001f]", state.query):
            return HookResult("BLOCK", code=-32004, reason="unredactable control characters")
        if re.search(r"[\w.\-]+\s*(?:@|\[\s*at\s*\]|\(\s*at\s*\))\s*[\w\-]+", red, re.I):  # an address-like residue survived redaction
            return HookResult("BLOCK", code=-32004, reason="unredactable PII residue")
        return HookResult("MODIFY", query=red, meta={"pii": found}) if found or red != state.query else HookResult("ALLOW")


class _Impl:
    @hookimpl
    def register_plugins(self):
        return [PiiRedactor()]


impl = _Impl()
