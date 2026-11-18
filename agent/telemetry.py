"""Spans named per finaldesign.md §10 (D8). Records durations locally (ring buffer, drop-oldest, count drops --
FMEA #12) and mirrors to OpenTelemetry; OTLP export only if OTEL_EXPORTER_OTLP_ENDPOINT is set.
"""
from __future__ import annotations

import os
import secrets
import time
from collections import defaultdict, deque
from contextlib import contextmanager

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

SPAN_NAMES = ["hook.pre", "cache.lookup", "triage.classify", "mcp.rag.bm25", "mcp.rag.biencoder", "mcp.rag.rrf",
              "mcp.rag.rerank", "agent.react.step", "llm.generate", "hook.post.ragas"]

_provider = TracerProvider()
if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
    try:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        _provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    except Exception:  # exporter failure must never affect serving
        pass
_tracer = _provider.get_tracer("pharma-agent")


class Recorder:
    def __init__(self, cap: int = 20000):
        self.durations: dict[str, deque] = defaultdict(lambda: deque(maxlen=cap))
        self.counters: dict[str, float] = defaultdict(float)
        self.recent: deque = deque(maxlen=2000)  # (trace_id, name, ms, start_perf_counter)
        self.e2e_window: deque = deque(maxlen=50000)  # (wall_ts, ms) for incident alerting (p95 over a window)
        self.dropped = 0

    def window_pct(self, p: float, seconds: float) -> tuple[float | None, int]:
        cut = time.time() - seconds
        xs = sorted(ms for ts, ms in self.e2e_window if ts >= cut)
        return (xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))] if xs else None), len(xs)

    def observe(self, name: str, ms: float, trace_id: str = "", start: float | None = None):
        d = self.durations[name]
        if len(d) == d.maxlen:
            self.dropped += 1
        d.append(ms)
        self.recent.append((trace_id, name, ms, start if start is not None else time.perf_counter() - ms / 1000))
        if name == "e2e.all":
            self.e2e_window.append((time.time(), ms))

    def inc(self, key: str, v: float = 1.0):
        self.counters[key] += v

    def pct(self, name: str, p: float) -> float | None:
        d = sorted(self.durations.get(name, ()))
        if not d:
            return None
        return d[min(len(d) - 1, int(round(p / 100 * (len(d) - 1))))]

    def reset(self):
        self.durations.clear(); self.counters.clear(); self.recent.clear(); self.e2e_window.clear(); self.dropped = 0


REC = Recorder()


def new_traceparent() -> tuple[str, str]:
    tid = secrets.token_hex(16)
    return tid, f"00-{tid}-{secrets.token_hex(8)}-01"


@contextmanager
def span(name: str, trace_id: str = "", **attrs):
    t0 = time.perf_counter()
    with _tracer.start_as_current_span(name) as s:
        for k, v in attrs.items():
            s.set_attribute(k, v if isinstance(v, (str, int, float, bool)) else str(v))
        try:
            yield s
        finally:
            REC.observe(name, (time.perf_counter() - t0) * 1000, trace_id, start=t0)
