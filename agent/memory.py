"""Context retention (finaldesign.md §4.3): immutable GoalAnchorBlock at prompt index 0, extractive observation
compression (<= 180 tokens), last-3-turn sliding window + verified facts in `session_facts`."""
from __future__ import annotations

import re
from collections import OrderedDict, deque
from dataclasses import dataclass, field

from agent.harness import repair_value

BOILERPLATE_KEYS = {"chunk_hash", "stage_ms", "index_version", "source_uri", "bm25", "cosine", "rrf_rank", "edges",
                    "token_estimate", "abstain", "early_abstain", "degraded", "version", "risk_tier", "domain"}


@dataclass(frozen=True)
class GoalAnchorBlock:
    original_intent: str
    entities: tuple[str, ...]
    constraints: tuple[str, ...]

    def render(self) -> str:
        return (f"Goal: {self.original_intent}. Entities: {', '.join(self.entities) or 'none'}. "
                f"Constraints: {' '.join(self.constraints)}")


POLICY_CONSTRAINTS = (
    "Answer only from provided formulary chunks and cite chunk ids.",
    "Never recommend prescription-only medicines or pediatric doses; refer to a pharmacist.",
    "Any state change is TIER_3 and needs human approval.",
)


def normalize_entities(text: str) -> tuple[str, ...]:
    ents = []
    for m in re.finditer(r"\b(sku|lot)[\s_\-]*(\d{1,5})\b", text, re.I):
        ents.append(repair_value(m.group(1).lower(), m.group(2)))
    return tuple(dict.fromkeys(e for e in ents if e))


def make_anchor(query: str) -> GoalAnchorBlock:
    return GoalAnchorBlock(query.strip()[:300], normalize_entities(query), POLICY_CONSTRAINTS)


def count_tokens(s: str) -> int:
    return max(1, int(len(s.split()) * 1.3))


def compress_observation(obj, max_tokens: int = 180) -> str:
    """Extractive: drop boilerplate keys, keep the sentences, truncate to <= max_tokens."""
    parts: list[str] = []

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k in BOILERPLATE_KEYS:
                    continue
                walk(v)
        elif isinstance(o, (list, tuple)):
            for v in o:
                walk(v)
        elif isinstance(o, str):
            parts.append(o)
    walk(obj)
    words = " ".join(parts).split()
    budget = int(max_tokens / 1.3)
    return " ".join(words[:budget])


class SessionWindow:
    """Last 3 turns hot in RAM, keyed by (tenant, session); LRU-bounded."""

    def __init__(self, cap_sessions: int = 10000):
        self.w: "OrderedDict[tuple, deque]" = OrderedDict()
        self.cap = cap_sessions

    def add(self, tenant: str, session: str, turn: dict):
        k = (tenant, session)
        d = self.w.setdefault(k, deque(maxlen=3))
        d.append(turn)
        self.w.move_to_end(k)
        while len(self.w) > self.cap:
            self.w.popitem(last=False)

    def get(self, tenant: str, session: str) -> list[dict]:
        return list(self.w.get((tenant, session), ()))


def token_cosine(a: str, b: str) -> float:
    from collections import Counter
    import math
    ca, cb = Counter(re.findall(r"[a-z0-9]+", a.lower())), Counter(re.findall(r"[a-z0-9]+", b.lower()))
    num = sum(ca[t] * cb[t] for t in ca)
    den = math.sqrt(sum(v * v for v in ca.values())) * math.sqrt(sum(v * v for v in cb.values()))
    return num / den if den else 0.0
