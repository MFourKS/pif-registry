"""Трассировка прогона на OpenTelemetry.

Дерево спанов: прогон -> этап -> УК -> фонд -> HTTP-запрос. Спаны всегда пишутся в
out/traces/<run_id>.jsonl. Если задан OTEL_EXPORTER_OTLP_ENDPOINT и установлен extra `otlp`,
они дополнительно уходят в Jaeger, Tempo или другой OTLP-приёмник.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from pathlib import Path
from typing import IO

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult


class JsonlSpanExporter(SpanExporter):
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file: IO[str] = path.open("a", encoding="utf-8")

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        for span in spans:
            ctx = span.get_span_context()
            start, end = span.start_time or 0, span.end_time or 0
            record = {
                "trace_id": f"{ctx.trace_id:032x}",
                "span_id": f"{ctx.span_id:016x}",
                "parent_id": f"{span.parent.span_id:016x}" if span.parent else None,
                "name": span.name,
                "start_ns": start,
                "duration_ms": round((end - start) / 1e6, 2),
                "status": span.status.status_code.name,
                "attributes": dict(span.attributes or {}),
            }
            self._file.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        self._file.flush()
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        self._file.close()


def configure_tracing(run_id: str, trace_dir: Path) -> TracerProvider:
    provider = TracerProvider(
        resource=Resource.create({"service.name": "pif-registry", "pif.run_id": run_id})
    )
    provider.add_span_processor(BatchSpanProcessor(JsonlSpanExporter(trace_dir / f"{run_id}.jsonl")))
    if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    return provider


def tracer() -> trace.Tracer:
    return trace.get_tracer("pif")
