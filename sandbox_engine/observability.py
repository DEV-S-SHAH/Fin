"""Production observability for FinGraph using stdlib only.

Provides:
- Request ID generation and context propagation
- Structured JSON logging with bounded volume
- Stage timers for pipeline observability
- Counters for requests, failures, rate limits, retries
- No external dependencies (stdlib only)
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import random
import threading
import time
import uuid
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

# ──────────────────────────────────────────────────────────────────────────────
# Request ID context propagation
# ──────────────────────────────────────────────────────────────────────────────

_REQUEST_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id", default=None
)
_SPAN_ID: contextvars.ContextVar[int] = contextvars.ContextVar("span_id", default=0)


def generate_request_id() -> str:
    """Generate a new request ID using UUID4 (128-bit, collision-resistant)."""
    return uuid.uuid4().hex[:16]  # 16 hex chars = 64 bits, enough for request tracing


def get_request_id() -> str | None:
    """Get the current request ID from context."""
    return _REQUEST_ID.get()


def set_request_id(request_id: str | None) -> contextvars.Token:
    """Set the request ID in context, returning a token for restoration."""
    return _REQUEST_ID.set(request_id or generate_request_id())


def clear_request_id(token: contextvars.Token | None = None) -> None:
    """Clear or restore the request ID context."""
    if token is not None:
        _REQUEST_ID.reset(token)
    else:
        _REQUEST_ID.set(None)


def new_span_id() -> int:
    """Generate a new span ID within the current request."""
    current = _SPAN_ID.get()
    next_id = current + 1
    _SPAN_ID.set(next_id)
    return next_id


def get_span_id() -> int:
    """Get the current span ID."""
    return _SPAN_ID.get()


# ──────────────────────────────────────────────────────────────────────────────
# Structured logging
# ──────────────────────────────────────────────────────────────────────────────

class StructuredFormatter(logging.Formatter):
    """JSON log formatter with request context and sensitive field filtering."""

    SENSITIVE_FIELDS = frozenset({
        "api_key", "apikey", "authorization", "bearer", "token", "secret",
        "password", "credential", "api_token", "access_token", "refresh_token",
        "oauth_token", "client_secret", "private_key", "signing_key",
        "nvapi-", "sk-", "Bearer ", "x-api-key", "x-amz-security-token"
    })

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._hostname = os.uname().nodename if hasattr(os, "uname") else "unknown"

    def _sanitize(self, obj: Any) -> Any:
        """Recursively sanitize sensitive fields from log data."""
        if isinstance(obj, dict):
            return {
                k: "***REDACTED***" if self._is_sensitive(k) else self._sanitize(v)
                for k, v in obj.items()
            }
        if isinstance(obj, (list, tuple)):
            return [self._sanitize(item) for item in obj]
        if isinstance(obj, str):
            # Check for common secret patterns in string values
            for pattern in ("nvapi-", "sk-", "Bearer ", "eyJ"):  # JWT prefix
                if pattern in obj and len(obj) > 20:
                    return "***REDACTED***"
        return obj

    def _is_sensitive(self, key: str) -> bool:
        key_lower = key.lower()
        return any(s in key_lower for s in self.SENSITIVE_FIELDS)

    def format(self, record: logging.LogRecord) -> str:
        request_id = _REQUEST_ID.get()
        span_id = _SPAN_ID.get()

        log_data = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "host": self._hostname,
            "pid": os.getpid(),
            "thread": threading.current_thread().ident,
        }

        if request_id:
            log_data["request_id"] = request_id
        if span_id:
            log_data["span_id"] = span_id

        # Add extra fields from record
        for key, value in record.__dict__.items():
            if key not in {
                "name", "msg", "args", "created", "filename", "funcName",
                "levelname", "levelno", "lineno", "module", "msecs",
                "message", "name", "pathname", "process", "processName",
                "relativeCreated", "thread", "threadName", "exc_info",
                "exc_text", "stack_info", "getMessage"
            }:
                # If the key itself is sensitive, always redact the value
                if self._is_sensitive(key):
                    log_data[key] = "***REDACTED***"
                else:
                    log_data[key] = self._sanitize(value)

        # Add exception info if present
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_data, default=str, separators=(",", ":"))


# ──────────────────────────────────────────────────────────────────────────────
# Stage Timer
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class StageTimer:
    """Timer for tracking pipeline stage latencies with structured logging.

    Usage:
        timer = StageTimer()
        with timer.stage("routing"):
            routing = route_query(question, kg)
        with timer.stage("traversal"):
            nodes, edges = retrieve_financial_context(...)

        # Get all stage latencies as dict
        latencies = timer.as_dict()  # {"routing_ms": 1.2, "traversal_ms": 45.6, "total_ms": 46.8}
    """

    _stages: dict[str, float] = field(default_factory=dict)
    _current_stage: str | None = None
    _stage_start: float = 0.0
    _start_time: float = field(default_factory=time.perf_counter)
    _on_enter: Callable[[str], None] | None = None
    _on_exit: Callable[[str, float], None] | None = None
    _request_id: str | None = field(default_factory=lambda: _REQUEST_ID.get())

    def __init__(
        self,
        on_enter: Callable[[str], None] | None = None,
        on_exit: Callable[[str, float], None] | None = None,
        request_id: str | None = None,
    ) -> None:
        self._stages = {}
        self._current_stage = None
        self._stage_start = 0.0
        self._start_time = time.perf_counter()
        self._on_enter = on_enter
        self._on_exit = on_exit
        self._request_id = request_id or _REQUEST_ID.get()

    @contextmanager
    def stage(self, name: str) -> "StageTimer":
        """Context manager for timing a stage."""
        if self._current_stage is not None:
            raise RuntimeError(f"Stage {self._current_stage!r} already active; cannot nest stages")

        self._current_stage = name
        self._stage_start = time.perf_counter()
        if self._on_enter:
            self._on_enter(name)

        # Emit stage start event
        self._log_stage_event(name, "start", 0.0)

        try:
            yield self
        finally:
            elapsed_ms = (time.perf_counter() - self._stage_start) * 1000
            self._stages[name] = elapsed_ms
            self._log_stage_event(name, "end", elapsed_ms)
            if self._on_exit:
                self._on_exit(name, elapsed_ms)
            self._current_stage = None

    def _log_stage_event(self, stage: str, event: str, elapsed_ms: float) -> None:
        """Emit structured log for stage boundary."""
        logger = logging.getLogger("graphrag.obs")
        if logger.isEnabledFor(logging.INFO):
            logger.info(
                "stage_event",
                extra={
                    "stage": stage,
                    "stage_event": event,
                    "stage_elapsed_ms": round(elapsed_ms, 2),
                    "request_id": self._request_id,
                },
            )

    def as_dict(self) -> dict[str, float]:
        """Return stage latencies as dict with _ms suffix."""
        total_ms = (time.perf_counter() - self._start_time) * 1000
        result = {f"{k}_ms": round(v, 2) for k, v in self._stages.items()}
        result["total_ms"] = round(total_ms, 2)
        return result

    def as_wire(self) -> dict[str, float]:
        """Wire-compatible format matching existing timer.as_wire()."""
        return self.as_dict()

    @property
    def elapsed_ms(self) -> float:
        """Total elapsed time in milliseconds."""
        return (time.perf_counter() - self._start_time) * 1000


# ──────────────────────────────────────────────────────────────────────────────
# Counters
# ──────────────────────────────────────────────────────────────────────────────

class CounterSet:
    """Thread-safe counters for observability metrics."""

    def __init__(self) -> None:
        self._counters: dict[str, int] = defaultdict(int)
        self._lock = threading.Lock()

    def increment(self, name: str, value: int = 1, labels: dict[str, str] | None = None) -> None:
        """Increment a counter."""
        key = self._make_key(name, labels)
        with self._lock:
            self._counters[key] += value

    def get(self, name: str, labels: dict[str, str] | None = None) -> int:
        """Get counter value."""
        key = self._make_key(name, labels)
        with self._lock:
            return self._counters.get(key, 0)

    def snapshot(self) -> dict[str, int]:
        """Get all counters as a snapshot."""
        with self._lock:
            return dict(self._counters)

    def _make_key(self, name: str, labels: dict[str, str] | None) -> str:
        if not labels:
            return name
        label_str = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
        return f"{name}{{{label_str}}}"


# Global counter registry
_COUNTERS = CounterSet()


def get_counters() -> CounterSet:
    """Get the global counter registry."""
    return _COUNTERS


# Convenience counter names
class CounterNames:
    REQUESTS_TOTAL = "requests_total"
    REQUESTS_FAILED = "requests_failed"
    RATE_LIMIT_REJECTIONS = "rate_limit_rejections"
    INGESTION_JOBS = "ingestion_jobs"
    RETRIES_TOTAL = "retries_total"
    LLM_FAILURES = "llm_failures"
    EXTERNAL_API_FAILURES = "external_api_failures"
    SEC_FETCH_TOTAL = "sec_fetch_total"
    SEC_FETCH_FAILED = "sec_fetch_failed"
    SEC_RATE_LIMITED = "sec_rate_limited"
    YAHOO_FETCH_TOTAL = "yahoo_fetch_total"
    YAHOO_FETCH_FAILED = "yahoo_fetch_failed"
    OLLAMA_PROBE_TOTAL = "ollama_probe_total"
    OLLAMA_PROBE_FAILED = "ollama_probe_failed"


def record_request(route: str, success: bool = True) -> None:
    """Record a request counter."""
    _COUNTERS.increment(CounterNames.REQUESTS_TOTAL, labels={"route": route})
    if not success:
        _COUNTERS.increment(CounterNames.REQUESTS_FAILED, labels={"route": route})


def record_rate_limit_rejection(service: str) -> None:
    """Record a rate limit rejection."""
    _COUNTERS.increment(CounterNames.RATE_LIMIT_REJECTIONS, labels={"service": service})


def record_retry(service: str, attempt: int) -> None:
    """Record a retry attempt."""
    _COUNTERS.increment(CounterNames.RETRIES_TOTAL, labels={"service": service, "attempt": str(attempt)})


def record_llm_failure(provider: str, error_type: str) -> None:
    """Record an LLM failure."""
    _COUNTERS.increment(CounterNames.LLM_FAILURES, labels={"provider": provider, "error_type": error_type})


def record_external_api_failure(service: str, error_type: str) -> None:
    """Record an external API failure."""
    _COUNTERS.increment(CounterNames.EXTERNAL_API_FAILURES, labels={"service": service, "error_type": error_type})


def record_sec_fetch(success: bool = True, rate_limited: bool = False) -> None:
    """Record SEC fetch metrics."""
    _COUNTERS.increment(CounterNames.SEC_FETCH_TOTAL)
    if not success:
        _COUNTERS.increment(CounterNames.SEC_FETCH_FAILED)
    if rate_limited:
        _COUNTERS.increment(CounterNames.SEC_RATE_LIMITED)


def record_yahoo_fetch(success: bool = True) -> None:
    """Record Yahoo Finance fetch metrics."""
    _COUNTERS.increment(CounterNames.YAHOO_FETCH_TOTAL)
    if not success:
        _COUNTERS.increment(CounterNames.YAHOO_FETCH_FAILED)


def record_ollama_probe(success: bool = True) -> None:
    """Record Ollama probe metrics."""
    _COUNTERS.increment(CounterNames.OLLAMA_PROBE_TOTAL)
    if not success:
        _COUNTERS.increment(CounterNames.OLLAMA_PROBE_FAILED)


def record_ingestion_job(ticker: str, status: str) -> None:
    """Record an ingestion job."""
    _COUNTERS.increment(CounterNames.INGESTION_JOBS, labels={"ticker": ticker, "status": status})


# ──────────────────────────────────────────────────────────────────────────────
# Log volume control
# ──────────────────────────────────────────────────────────────────────────────

class LogVolumeLimiter:
    """Rate limiter for log volume to prevent logging from becoming a bottleneck.

    Uses token bucket algorithm with per-logger limits.
    """

    def __init__(
        self,
        max_per_second: float = 100.0,
        burst: int = 50,
    ) -> None:
        self.max_per_second = max_per_second
        self.burst = burst
        self._tokens: dict[str, float] = defaultdict(lambda: float(burst))
        self._last_update: dict[str, float] = defaultdict(time.monotonic)
        self._lock = threading.Lock()

    def allow(self, logger_name: str) -> bool:
        """Check if a log event should be emitted."""
        with self._lock:
            now = time.monotonic()
            last = self._last_update[logger_name]
            tokens = self._tokens[logger_name]

            # Refill tokens
            elapsed = now - last
            tokens = min(self.burst, tokens + elapsed * self.max_per_second)

            if tokens >= 1.0:
                tokens -= 1.0
                self._tokens[logger_name] = tokens
                self._last_update[logger_name] = now
                return True

            self._tokens[logger_name] = tokens
            self._last_update[logger_name] = now
            return False


# Global log volume limiter (configurable via env)
_LOG_LIMITER = LogVolumeLimiter(
    max_per_second=float(os.environ.get("GRAPHRAG_LOG_MAX_PER_SECOND", "100")),
    burst=int(os.environ.get("GRAPHRAG_LOG_BURST", "50")),
)


class VolumeLimitedFilter(logging.Filter):
    """Logging filter that enforces volume limits."""

    def filter(self, record: logging.LogRecord) -> bool:
        # Always allow ERROR and above
        if record.levelno >= logging.ERROR:
            return True
        # Rate limit INFO and below
        return _LOG_LIMITER.allow(record.name)


# ──────────────────────────────────────────────────────────────────────────────
# Logging setup
# ──────────────────────────────────────────────────────────────────────────────

def configure_structured_logging(
    level: int = logging.INFO,
    json_format: bool = True,
    volume_limit: bool = True,
) -> None:
    """Configure structured logging for the application.

    Args:
        level: Log level (default INFO)
        json_format: Use JSON structured format (default True)
        volume_limit: Enable log volume limiting (default True)
    """
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # Clear existing handlers
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    handler = logging.StreamHandler()
    handler.setLevel(level)

    if json_format:
        handler.setFormatter(StructuredFormatter())
    else:
        handler.setFormatter(logging.Formatter(
            "%(asctime)s  %(levelname)-7s  %(message)s",
            datefmt="%H:%M:%S"
        ))

    if volume_limit:
        handler.addFilter(VolumeLimitedFilter())

    root_logger.addHandler(handler)

    # Reduce noise from noisy libraries
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)
    logging.getLogger("anthropic").setLevel(logging.WARNING)


# ──────────────────────────────────────────────────────────────────────────────
# Request context decorator for functions
# ──────────────────────────────────────────────────────────────────────────────

def with_request_context(request_id: str | None = None):
    """Decorator to ensure request ID context is set for a function."""
    def decorator(func: Callable) -> Callable:
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            token = set_request_id(request_id)
            try:
                return func(*args, **kwargs)
            finally:
                clear_request_id(token)
        return wrapper
    return decorator


# ──────────────────────────────────────────────────────────────────────────────
# Context manager for request lifecycle
# ──────────────────────────────────────────────────────────────────────────────

@contextmanager
def request_context(request_id: str | None = None):
    """Context manager for request lifecycle with automatic request ID management.

    Usage:
        with request_context() as req_id:
            # request_id is available via get_request_id()
            do_work()
    """
    token = set_request_id(request_id)
    req_id = _REQUEST_ID.get()
    logger = logging.getLogger("graphrag.obs")
    logger.info("request_start", extra={"request_id": req_id})
    start = time.perf_counter()
    success = True
    try:
        yield req_id
    except Exception:
        success = False
        raise
    finally:
        elapsed_ms = (time.perf_counter() - start) * 1000
        logger.info(
            "request_end",
            extra={
                "request_id": req_id,
                "elapsed_ms": round(elapsed_ms, 2),
                "success": success,
            },
        )
        record_request("http", success=success)
        clear_request_id(token)