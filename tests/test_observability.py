"""Tests for the observability module."""

from __future__ import annotations

import io
import json
import logging
import time
from contextlib import redirect_stdout
from unittest import TestCase, mock

from sandbox_engine.observability import (
    StageTimer,
    CounterSet,
    CounterNames,
    generate_request_id,
    get_request_id,
    set_request_id,
    clear_request_id,
    new_span_id,
    configure_structured_logging,
    record_request,
    record_rate_limit_rejection,
    record_retry,
    record_llm_failure,
    record_external_api_failure,
    record_sec_fetch,
    record_yahoo_fetch,
    record_ollama_probe,
    record_ingestion_job,
    _COUNTERS,
    request_context,
    StructuredFormatter,
    _LOG_LIMITER,
)


class TestRequestId(TestCase):
    """Test request ID generation and context propagation."""

    def test_generate_request_id_format(self) -> None:
        """Request ID should be 16 hex characters."""
        rid = generate_request_id()
        self.assertEqual(len(rid), 16)
        self.assertTrue(all(c in "0123456789abcdef" for c in rid))

    def test_generate_request_id_unique(self) -> None:
        """Request IDs should be unique."""
        ids = {generate_request_id() for _ in range(100)}
        self.assertEqual(len(ids), 100)

    def test_set_and_get_request_id(self) -> None:
        """Setting and getting request ID works."""
        token = set_request_id("test-123")
        try:
            self.assertEqual(get_request_id(), "test-123")
        finally:
            clear_request_id(token)

    def test_clear_request_id(self) -> None:
        """Clearing request ID restores previous value."""
        set_request_id("first")
        token = set_request_id("second")
        try:
            self.assertEqual(get_request_id(), "second")
        finally:
            clear_request_id(token)
        self.assertEqual(get_request_id(), "first")

    def test_clear_with_none_token(self) -> None:
        """Clearing with None token sets to None."""
        set_request_id("test")
        clear_request_id(None)
        self.assertIsNone(get_request_id())

    def test_context_manager(self) -> None:
        """request_context manager sets and clears request ID."""
        with request_context("ctx-123") as rid:
            self.assertEqual(rid, "ctx-123")
            self.assertEqual(get_request_id(), "ctx-123")
        self.assertIsNone(get_request_id())

    def test_context_manager_auto_generates(self) -> None:
        """request_context generates ID if not provided."""
        with request_context() as rid:
            self.assertIsNotNone(rid)
            self.assertEqual(len(rid), 16)


class TestSpanId(TestCase):
    """Test span ID generation."""

    def test_new_span_id_increments(self) -> None:
        """Each call to new_span_id increments."""
        # Reset
        from sandbox_engine.observability import _SPAN_ID
        _SPAN_ID.set(0)
        
        self.assertEqual(new_span_id(), 1)
        self.assertEqual(new_span_id(), 2)
        self.assertEqual(new_span_id(), 3)

    def test_get_span_id(self) -> None:
        """get_span_id returns current value."""
        from sandbox_engine.observability import _SPAN_ID
        _SPAN_ID.set(5)
        self.assertEqual(new_span_id(), 6)  # new_span_id increments
        self.assertEqual(new_span_id(), 7)


class TestStageTimer(TestCase):
    """Test StageTimer functionality."""

    def test_stage_timing(self) -> None:
        """Stage timer records elapsed time."""
        timer = StageTimer()
        with timer.stage("test_stage"):
            time.sleep(0.01)
        latencies = timer.as_dict()
        self.assertIn("test_stage_ms", latencies)
        self.assertGreaterEqual(latencies["test_stage_ms"], 10)
        self.assertIn("total_ms", latencies)

    def test_multiple_stages(self) -> None:
        """Multiple stages are tracked separately."""
        timer = StageTimer()
        with timer.stage("stage1"):
            time.sleep(0.01)
        with timer.stage("stage2"):
            time.sleep(0.01)
        latencies = timer.as_dict()
        self.assertIn("stage1_ms", latencies)
        self.assertIn("stage2_ms", latencies)
        self.assertIn("total_ms", latencies)

    def test_nested_stages_raises(self) -> None:
        """Nesting stages raises RuntimeError."""
        timer = StageTimer()
        with timer.stage("outer"):
            with self.assertRaises(RuntimeError):
                with timer.stage("inner"):
                    pass

    def test_on_enter_callback(self) -> None:
        """on_enter callback is called on stage entry."""
        stages = []
        timer = StageTimer(on_enter=stages.append)
        with timer.stage("test"):
            pass
        self.assertEqual(stages, ["test"])

    def test_as_wire_format(self) -> None:
        """as_wire returns wire-compatible format."""
        timer = StageTimer()
        with timer.stage("routing"):
            time.sleep(0.01)
        wire = timer.as_wire()
        self.assertIn("routing_ms", wire)
        self.assertIn("total_ms", wire)


class TestCounterSet(TestCase):
    """Test CounterSet functionality."""

    def test_increment_and_get(self) -> None:
        """Counters can be incremented and retrieved."""
        counters = CounterSet()
        counters.increment("test_counter")
        counters.increment("test_counter", 5)
        self.assertEqual(counters.get("test_counter"), 6)

    def test_increment_with_labels(self) -> None:
        """Counters support labels."""
        counters = CounterSet()
        counters.increment("requests", labels={"route": "ask"})
        counters.increment("requests", labels={"route": "ask"})
        counters.increment("requests", labels={"route": "health"})
        self.assertEqual(counters.get("requests", labels={"route": "ask"}), 2)
        self.assertEqual(counters.get("requests", labels={"route": "health"}), 1)

    def test_snapshot(self) -> None:
        """Snapshot returns all counters."""
        counters = CounterSet()
        counters.increment("a")
        counters.increment("b", labels={"x": "y"})
        snap = counters.snapshot()
        self.assertIn("a", snap)
        self.assertIn("b{x=y}", snap)


class TestGlobalCounters(TestCase):
    """Test global counter functions."""

    def setUp(self) -> None:
        # Reset global counters
        _COUNTERS._counters.clear()

    def test_record_request(self) -> None:
        """record_request increments request counters."""
        record_request("ask", success=True)
        self.assertEqual(_COUNTERS.get(CounterNames.REQUESTS_TOTAL, labels={"route": "ask"}), 1)
        self.assertEqual(_COUNTERS.get(CounterNames.REQUESTS_FAILED, labels={"route": "ask"}), 0)

        record_request("ask", success=False)
        self.assertEqual(_COUNTERS.get(CounterNames.REQUESTS_FAILED, labels={"route": "ask"}), 1)

    def test_record_rate_limit_rejection(self) -> None:
        """record_rate_limit_rejection increments rate limit counter."""
        record_rate_limit_rejection("sec_edgar")
        self.assertEqual(_COUNTERS.get(CounterNames.RATE_LIMIT_REJECTIONS, labels={"service": "sec_edgar"}), 1)

    def test_record_retry(self) -> None:
        """record_retry increments retry counter."""
        record_retry("yahoo_finance", 1)
        record_retry("yahoo_finance", 2)
        self.assertEqual(_COUNTERS.get(CounterNames.RETRIES_TOTAL, labels={"service": "yahoo_finance", "attempt": "1"}), 1)
        self.assertEqual(_COUNTERS.get(CounterNames.RETRIES_TOTAL, labels={"service": "yahoo_finance", "attempt": "2"}), 1)

    def test_record_llm_failure(self) -> None:
        """record_llm_failure increments LLM failure counter."""
        record_llm_failure("nvidia", "timeout")
        self.assertEqual(_COUNTERS.get(CounterNames.LLM_FAILURES, labels={"provider": "nvidia", "error_type": "timeout"}), 1)

    def test_record_external_api_failure(self) -> None:
        """record_external_api_failure increments external API failure counter."""
        record_external_api_failure("sec_edgar", "network_error")
        self.assertEqual(_COUNTERS.get(CounterNames.EXTERNAL_API_FAILURES, labels={"service": "sec_edgar", "error_type": "network_error"}), 1)

    def test_record_sec_fetch(self) -> None:
        """record_sec_fetch increments SEC fetch counters."""
        record_sec_fetch(success=True)
        record_sec_fetch(success=False)
        record_sec_fetch(rate_limited=True)
        self.assertEqual(_COUNTERS.get(CounterNames.SEC_FETCH_TOTAL), 3)
        self.assertEqual(_COUNTERS.get(CounterNames.SEC_FETCH_FAILED), 1)
        self.assertEqual(_COUNTERS.get(CounterNames.SEC_RATE_LIMITED), 1)

    def test_record_yahoo_fetch(self) -> None:
        """record_yahoo_fetch increments Yahoo fetch counters."""
        record_yahoo_fetch(success=True)
        record_yahoo_fetch(success=False)
        self.assertEqual(_COUNTERS.get(CounterNames.YAHOO_FETCH_TOTAL), 2)
        self.assertEqual(_COUNTERS.get(CounterNames.YAHOO_FETCH_FAILED), 1)

    def test_record_ollama_probe(self) -> None:
        """record_ollama_probe increments Ollama probe counters."""
        record_ollama_probe(success=True)
        record_ollama_probe(success=False)
        self.assertEqual(_COUNTERS.get(CounterNames.OLLAMA_PROBE_TOTAL), 2)
        self.assertEqual(_COUNTERS.get(CounterNames.OLLAMA_PROBE_FAILED), 1)

    def test_record_ingestion_job(self) -> None:
        """record_ingestion_job increments ingestion job counters."""
        record_ingestion_job("AAPL", "success")
        record_ingestion_job("TSLA", "failed")
        self.assertEqual(_COUNTERS.get(CounterNames.INGESTION_JOBS, labels={"ticker": "AAPL", "status": "success"}), 1)
        self.assertEqual(_COUNTERS.get(CounterNames.INGESTION_JOBS, labels={"ticker": "TSLA", "status": "failed"}), 1)


class TestStructuredFormatter(TestCase):
    """Test StructuredFormatter."""

    def test_format_includes_request_id(self) -> None:
        """Formatted log includes request ID when set."""
        formatter = StructuredFormatter()
        record = logging.LogRecord(
            name="test.logger",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="Test message",
            args=(),
            exc_info=None,
        )
        token = set_request_id("test-req-123")
        try:
            output = formatter.format(record)
            parsed = json.loads(output)
            self.assertEqual(parsed["request_id"], "test-req-123")
            self.assertEqual(parsed["message"], "Test message")
            self.assertEqual(parsed["level"], "INFO")
        finally:
            clear_request_id(token)

    def test_format_sanitizes_sensitive_fields(self) -> None:
        """Formatter redacts sensitive fields."""
        formatter = StructuredFormatter()
        record = logging.LogRecord(
            name="test.logger",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="Test message",
            args=(),
            exc_info=None,
        )
        # Add sensitive extra fields
        record.api_key = "sk-secret123"
        record.password = "hunter2"
        record.normal_field = "value"
        
        output = formatter.format(record)
        parsed = json.loads(output)
        self.assertEqual(parsed["api_key"], "***REDACTED***")
        self.assertEqual(parsed["password"], "***REDACTED***")
        self.assertEqual(parsed["normal_field"], "value")

    def test_format_includes_span_id(self) -> None:
        """Formatted log includes span ID when set."""
        formatter = StructuredFormatter()
        record = logging.LogRecord(
            name="test.logger",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="Test message",
            args=(),
            exc_info=None,
        )
        from sandbox_engine.observability import _SPAN_ID
        _SPAN_ID.set(42)
        token = set_request_id("test-req")
        try:
            output = formatter.format(record)
            parsed = json.loads(output)
            self.assertEqual(parsed["span_id"], 42)
        finally:
            clear_request_id(token)
            _SPAN_ID.set(0)


class TestLogVolumeLimiter(TestCase):
    """Test log volume limiting."""

    def test_limiter_allows_burst(self) -> None:
        """Limiter allows burst up to limit."""
        from sandbox_engine.observability import LogVolumeLimiter
        limiter = LogVolumeLimiter(max_per_second=10, burst=5)
        for _ in range(5):
            self.assertTrue(limiter.allow("test.logger"))
        self.assertFalse(limiter.allow("test.logger"))

    def test_limiter_refills_over_time(self) -> None:
        """Limiter refills tokens over time."""
        from sandbox_engine.observability import LogVolumeLimiter
        limiter = LogVolumeLimiter(max_per_second=100, burst=2)
        self.assertTrue(limiter.allow("test.refill"))
        self.assertTrue(limiter.allow("test.refill"))
        self.assertFalse(limiter.allow("test.refill"))
        time.sleep(0.03)  # Wait for refill
        self.assertTrue(limiter.allow("test.refill"))


class TestConfigureStructuredLogging(TestCase):
    """Test configure_structured_logging."""

    def test_configures_root_logger(self) -> None:
        """configure_structured_logging configures root logger."""
        # Save original handlers
        root = logging.getLogger()
        original_handlers = root.handlers[:]
        original_level = root.level
        
        try:
            configure_structured_logging(level=logging.INFO, json_format=True)
            
            self.assertEqual(root.level, logging.INFO)
            self.assertEqual(len(root.handlers), 1)
            self.assertIsInstance(root.handlers[0].formatter, StructuredFormatter)
        finally:
            # Restore
            root.handlers = original_handlers
            root.level = original_level

    def test_non_json_format(self) -> None:
        """Non-JSON format uses standard formatter."""
        root = logging.getLogger()
        original_handlers = root.handlers[:]
        original_level = root.level
        
        try:
            configure_structured_logging(level=logging.INFO, json_format=False)
            
            self.assertEqual(len(root.handlers), 1)
            self.assertNotIsInstance(root.handlers[0].formatter, StructuredFormatter)
        finally:
            root.handlers = original_handlers
            root.level = original_level


if __name__ == "__main__":
    import unittest
    unittest.main()