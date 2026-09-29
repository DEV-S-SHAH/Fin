"""Asynchronous background ingestion queue for Cold-Start JIT Graph RAG.

Manages background ingestion tasks using a bounded ThreadPoolExecutor,
prevents duplicate in-flight or already-staged runs, and performs atomic
file writes without locking the live LadybugDB instance.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


class BackgroundIngestQueue:
    """Non-blocking background queue manager for historical entity ingestion."""

    def __init__(
        self,
        max_workers: int = 2,
        staging_dir: Optional[Path | str] = None,
    ) -> None:
        self.max_workers = max_workers
        self.staging_dir = Path(staging_dir) if staging_dir else Path("data/staging")
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        self.executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="ColdStartIngest",
        )
        self.in_flight_tickers: set[str] = set()
        self.lock = threading.Lock()
        self.tasks: list[concurrent.futures.Future] = []

    def is_in_flight(self, ticker: str) -> bool:
        """Check if a ticker is currently being processed."""
        clean = ticker.strip().upper()
        with self.lock:
            return clean in self.in_flight_tickers

    def is_staged(self, ticker: str) -> bool:
        """Check if an ingestion staging artifact exists for this ticker."""
        clean = ticker.strip().upper()
        target_file = self.staging_dir / f"{clean}.jsonl"
        return target_file.is_file() and target_file.stat().st_size > 0

    def enqueue_coldstart_sync(
        self,
        ticker: str,
        metadata: Optional[dict[str, Any]] = None,
        force: bool = False,
    ) -> bool:
        """Enqueue background historical ingestion for a cold-start ticker.

        Returns:
        - True if the task was successfully enqueued.
        - False if the ticker is already in flight or already staged (deduplication).
        """
        clean = ticker.strip().upper()
        if not clean:
            return False

        with self.lock:
            if not force:
                if clean in self.in_flight_tickers or self.is_staged(clean):
                    return False
            self.in_flight_tickers.add(clean)

        try:
            future = self.executor.submit(
                self._async_stage_full_ingestion, clean, metadata or {}
            )
            self.tasks.append(future)
            return True
        except Exception:
            with self.lock:
                self.in_flight_tickers.discard(clean)
            return False

    def _write_atomic(self, ticker: str, record: dict[str, Any]) -> Path:
        """Atomically write record to target JSONL file using temp file and replace."""
        target_file = self.staging_dir / f"{ticker}.jsonl"
        tmp_file = (
            self.staging_dir
            / f".{ticker}.tmp.{os.getpid()}.{threading.get_ident()}.{time.monotonic_ns()}"
        )
        line = json.dumps(record) + "\n"
        try:
            with open(tmp_file, "w", encoding="utf-8") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_file, target_file)
            return target_file
        except Exception:
            if tmp_file.exists():
                try:
                    tmp_file.unlink()
                except OSError:
                    pass
            raise

    def _async_stage_full_ingestion(
        self,
        ticker: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> Path:
        """Worker task: executes full extraction pipeline and writes staging records."""
        from sandbox_engine.tier1_fetch import SECRuntimeFetcher
        from sandbox_engine.tier1_clean import clean_and_truncate_section
        from sandbox_engine.coldstart_extract import ColdStartExtractor

        clean_ticker = ticker.strip().upper()
        target_file = self.staging_dir / f"{clean_ticker}.jsonl"

        try:
            # Step A: Write initial intent { "ticker": ticker, "status": "staged", "timestamp": ... }
            timestamp_staged = datetime.now(timezone.utc).isoformat()
            initial_record: dict[str, Any] = {
                "ticker": clean_ticker,
                "status": "staged",
                "timestamp": timestamp_staged,
            }
            if metadata is not None:
                initial_record["metadata"] = metadata
            self._write_atomic(clean_ticker, initial_record)

            # Step B: Fetch the latest 10-K via SECRuntimeFetcher.fetch_latest_filing_html(ticker)
            try:
                fetch_res = SECRuntimeFetcher.fetch_latest_filing_html(clean_ticker)
            except TypeError:
                fetcher = SECRuntimeFetcher()
                fetch_res = fetcher.fetch_latest_filing_html(clean_ticker)

            if isinstance(fetch_res, tuple):
                raw_html = fetch_res[0]
            else:
                raw_html = fetch_res

            # Step C: Clean and slice Item 1 narrative via clean_and_truncate_section(html)
            narrative = clean_and_truncate_section(raw_html)

            # Step D: Run triple extraction via ColdStartExtractor.extract(text, ticker)
            try:
                payload = ColdStartExtractor.extract(narrative, clean_ticker)
            except TypeError:
                extractor = ColdStartExtractor()
                payload = extractor.extract(narrative, clean_ticker)
            except AttributeError:
                extractor = ColdStartExtractor()
                payload = extractor.extract_triples(narrative, target_ticker=clean_ticker)

            # Step E: Atomically overwrite data/staging/{ticker}.jsonl with status: "extracted"
            timestamp_extracted = datetime.now(timezone.utc).isoformat()
            payload_data = (
                payload.model_dump() if hasattr(payload, "model_dump") else payload
            )
            extracted_record = {
                "ticker": clean_ticker,
                "status": "extracted",
                "timestamp": timestamp_extracted,
                "payload": payload_data,
            }
            self._write_atomic(clean_ticker, extracted_record)

        except Exception as e:
            # Error Handling: atomically write { "ticker": ticker, "status": "failed", "error": str(e) }
            failed_record = {
                "ticker": clean_ticker,
                "status": "failed",
                "error": str(e),
            }
            try:
                self._write_atomic(clean_ticker, failed_record)
            except Exception:
                pass
        finally:
            with self.lock:
                self.in_flight_tickers.discard(clean_ticker)

        return target_file

    def shutdown(self, wait: bool = True) -> None:
        """Cleanly shutdown the worker executor."""
        self.executor.shutdown(wait=wait)
