"""otel_exporter (ALL_SPANS). Spans are created in agent/telemetry.py; OTLP export is attached when
OTEL_EXPORTER_OTLP_ENDPOINT is set and uses a bounded ring buffer (drop-oldest, count drops): exporter failure never
affects serving (FMEA #12). This plugin exposes status for the workbench."""
from __future__ import annotations

import os

from agent.telemetry import REC
from plugins.base import HookResult, Plugin, hookimpl


class OtelExporter(Plugin):
    name = "otel_exporter"

    async def run(self, state):
        return HookResult("ALLOW")

    def status(self) -> dict:
        return {"endpoint": getattr(self, "cfg", {}).get("endpoint"), "otlp_active": bool(os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")),
                "dropped_spans": REC.dropped}


class _Impl:
    @hookimpl
    def register_plugins(self):
        return [OtelExporter()]


impl = _Impl()
