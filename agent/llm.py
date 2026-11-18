"""Generation. Default = deterministic ExtractiveLLM (quotes sentences from retrieved chunks with chunk-id citations);
OpenAICompatLLM targets a vLLM/TRT-LLM endpoint (VLLM_URL) per §16.3 with FMEA #5 handling (one jittered retry ->
extractive fallback -> else 503). The model never sees tools or other tenants' data; prompt index 0 = GoalAnchorBlock."""
from __future__ import annotations

import asyncio
import os
import random
import re

import httpx

from agent.memory import GoalAnchorBlock
from agent.models import Chunk
from agent.triage import DOMAIN

ABSTAIN_TEMPLATE = ("I don't have sufficient verified information to answer that. Try rephrasing, or contact the policy owner. "
                    "(ref: {trace_id})")
SAFETY_FOOTER = ("This is formulary information, not personal medical advice. Please confirm with a pharmacist before use, "
                 "especially if pregnant, giving medicine to a child, or taking other medicines.")
EMERGENCY_TEXT = ("This may be an emergency. Please call your local emergency number or go to the nearest emergency department now. "
                  "I cannot help with this safely. (ref: {trace_id})")
CLINICAL_TEXT = ("I can't prescribe, dispense, diagnose or start/change treatment: those actions need a licensed prescriber and a pharmacist. "
                 "I can share formulary information, or you can contact the pharmacist on duty. (ref: {trace_id})")
OOS_TEXT = ("That is outside what I can help with. I answer questions about the pharmacy formulary, safety notes and SOPs. (ref: {trace_id})")


class LLMUnavailable(Exception):
    pass


def sentences(text: str) -> list[str]:
    text = re.sub(r"\[\[([^\]]+)\]\]", r"\1", text)
    text = re.sub(r"^#.*$", "", text, flags=re.M)
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if len(s.strip()) > 20]


def overlap(q: str, s: str) -> float:
    qt = set(re.findall(r"[a-z0-9]+", q.lower())) - {"the", "a", "an", "of", "for", "in", "is", "what", "which", "can", "i", "to", "and", "are", "with"}
    st = set(re.findall(r"[a-z0-9]+", s.lower()))
    return len(qt & st) / (len(qt) or 1)


class ExtractiveLLM:
    name = "extractive"

    async def generate(self, anchor: GoalAnchorBlock, chunks: list[Chunk], subgraph_text: str = "", strict: bool = False) -> str:
        q = anchor.original_intent
        out, seen = [], set()
        dosey = bool(re.search(r"\b(dose|dosage|maximum|max|how much|mg|g)\b", q, re.I))
        for ci, c in enumerate(chunks[:4]):
            ss = sentences(c.text)
            ss = [x for x in ss if not x.startswith("DEMO")]
            if not ss:
                continue
            score = lambda i: overlap(q, ss[i]) + (0.5 if dosey and re.search(r"\d", ss[i]) else 0)
            ranked = sorted(range(len(ss)), key=lambda i: (-score(i), i))
            take = 1 if (strict or ci > 0) else 2
            for i in sorted(ranked[:take]):
                if ss[i] not in seen:
                    seen.add(ss[i]); out.append(f"{ss[i]} [{c.chunk_id}]")
        return " ".join(out)


class OpenAICompatLLM:
    name = "openai-compat"

    def __init__(self, url: str, model: str, fallback: ExtractiveLLM):
        self.url, self.model, self.fallback = url.rstrip("/"), model, fallback
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(0.25))
        self.secondary = os.environ.get("VLLM_SECONDARY_URL")

    async def _once(self, base: str, prompt: str) -> str:
        r = await self.client.post(f"{base}/v1/chat/completions", json={"model": self.model, "temperature": 0.0, "max_tokens": 256,
                                   "messages": [{"role": "user", "content": prompt}]})
        if r.status_code in (429,) or r.status_code >= 500:
            raise LLMUnavailable(str(r.status_code))
        return r.json()["choices"][0]["message"]["content"]

    async def generate(self, anchor, chunks, subgraph_text="", strict=False) -> str:
        ctx = "\n".join(f"[{c.chunk_id}] {c.text}" for c in chunks)
        prompt = (f"{anchor.render()}\nUse only the allowed tools for your subagent. Answer only from provided chunks and cite chunk ids. "
                  f"If evidence is insufficient, say so. Never repeat a failed call.\n{'Quote exact sentences only.' if strict else ''}\n"
                  f"Context:\n{ctx}\n{subgraph_text}\nQuestion: {anchor.original_intent}")
        for base in (self.url, self.secondary):  # one jittered retry -> secondary pool
            if not base:
                continue
            try:
                return await self._once(base, prompt)
            except Exception:
                await asyncio.sleep(random.uniform(0.002, 0.01))
        out = await self.fallback.generate(anchor, chunks, subgraph_text, strict)  # extractive answer fallback
        if not out.strip():
            raise LLMUnavailable("no model answer and no extractive fallback")  # => 503 + Retry-After
        return out


def make_llm():
    ex = ExtractiveLLM()
    url = os.environ.get("VLLM_URL")
    return OpenAICompatLLM(url, os.environ.get("VLLM_MODEL", "instruct"), ex) if url else ex
