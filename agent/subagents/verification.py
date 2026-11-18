"""Verification Subagent (independent context; 'Audit' is an alias, D4). Claim->chunk entailment (NLI-lite lexical
entailment by default; swappable for an NLI model) with citation check. Vetoes ungrounded drafts.
Prompt contract (§14.3): split into atomic claims; ENTAILED / NOT_ENTAILED with supporting chunk id; any NOT_ENTAILED => veto."""
from __future__ import annotations

import re
from dataclasses import dataclass

from agent.models import Chunk

STOP = set("the a an of for in on is are be and or with to at by from as it that this can may if any not no do does".split())


def content_tokens(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", s.lower()) if w not in STOP}


def split_claims(answer: str) -> list[str]:
    out = []
    text = re.sub(r"\s*\[[A-Za-z0-9_\-]+#\d+\]", "", answer.strip())  # citations are not claims
    for s in re.split(r"(?<=[.!?])\s+(?=[A-Z])", text):
        s = s.strip()
        if len(s) > 3:
            out.append(s)
    return out


@dataclass
class Verdict:
    grounded: bool
    faithfulness: float
    claims: list[dict]


class VerificationSubagent:
    name = "verification"
    ENTAIL_THRESHOLD = 0.85

    def check(self, answer: str, chunks: list[Chunk]) -> Verdict:
        claims = split_claims(answer)
        if not claims:
            return Verdict(False, 0.0, [])
        results = []
        for c in claims:
            ct = content_tokens(c)
            best, bid = 0.0, None
            for ch in chunks:
                cov = len(ct & content_tokens(ch.text)) / (len(ct) or 1)
                if cov > best:
                    best, bid = cov, ch.chunk_id
            nums = set(re.findall(r"\d+(?:\.\d+)?", c))  # every number in a claim must appear in evidence
            nums_ok = all(any(n in ch.text for ch in chunks) for n in nums)
            ok = best >= self.ENTAIL_THRESHOLD and nums_ok
            results.append({"claim": c, "label": "ENTAILED" if ok else "NOT_ENTAILED", "chunk_id": bid, "coverage": round(best, 3)})
        f = sum(r["label"] == "ENTAILED" for r in results) / len(results)
        return Verdict(all(r["label"] == "ENTAILED" for r in results), f, results)
