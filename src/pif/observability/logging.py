"""Структурные логи: читаемые в консоли и JSON Lines в файле прогона.

К каждой записи добавляются run_id и идентификаторы текущего спана, поэтому по логу
можно перейти к трассе и обратно.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

import structlog
from opentelemetry import trace


def _add_trace_context(_: Any, __: str, event: dict[str, Any]) -> dict[str, Any]:
    ctx = trace.get_current_span().get_span_context()
    if ctx.is_valid:
        event["trace_id"] = f"{ctx.trace_id:032x}"
        event["span_id"] = f"{ctx.span_id:016x}"
    return event


def configure_logging(run_id: str, log_dir: Path, level: str = "INFO") -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{run_id}.jsonl"

    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _add_trace_context,
    ]
    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                _drop_ids,
                structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty()),
            ],
            foreign_pre_chain=shared,
        )
    )
    to_file = logging.FileHandler(log_path, encoding="utf-8")
    to_file.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.JSONRenderer(ensure_ascii=False),
            ],
            foreign_pre_chain=shared,
        )
    )

    root = logging.getLogger()
    root.handlers[:] = [console, to_file]
    root.setLevel(level)
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    for noisy in ("pdfminer", "pdfplumber"):
        logging.getLogger(noisy).setLevel(logging.ERROR)

    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(run_id=run_id)
    return log_path


def _drop_ids(_: Any, __: str, event: dict[str, Any]) -> dict[str, Any]:
    """В консоли идентификаторы только мешают: они остаются в JSON-файле."""
    for key in ("run_id", "trace_id", "span_id"):
        event.pop(key, None)
    return event


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
