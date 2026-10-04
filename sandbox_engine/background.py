"""Asynchronous background ingestion queue for Cold-Start JIT Graph RAG.

Manages background ingestion tasks using a bounded ThreadPoolExecutor,
prevents duplicate in-flight or already-staged runs, and performs atomic
file writes without locking the live LadybugDB instance.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .config import FINGRAPH_DATA_DIR
from .observability import (
    StageTimer,
    get_request_id,
    set_request_id,
    clear_request_id,
    generate_request_id,
    record_ingestion_job,
    record_external_api_failure,
)

log = logging.getLogger("graphrag.ingest")


class BackgroundIngestQueue:
    """Non-blocking background queue manager for historical entity ingestion."""

    def __init__(
        self,
        max_workers: int = 2,
        staging_dir: Optional[Path | str] = None,
    ) -> None:
        self.max_workers = max_workers
        # Use persistent staging directory under FINGRAPH_DATA_DIR so that
        # staged extractions survive restarts. The staging files are the
        # intermediate state that allows recovery without re-fetching from SEC.
        persistent_staging = FINGRAPH_DATA_DIR / "staging"
        self.staging_dir = Path(staging_dir) if staging_dir else persistent_staging
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        self.executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="ColdStartIngest",
        )
        self.in_flight_tickers: set[str] = set()
        self.lock = threading.Lock()
        self.tasks: list[concurrent.futures.Future] = []
        #: Set by ``begin_shutdown`` so a ticker enqueued during a drain is
        #: refused rather than started and then abandoned mid-extraction.
        self.draining: bool = False
        # Recover any in-flight state from persistent staging on startup
        self._recover_in_flight()

    def _recover_in_flight(self) -> None:
        """Recover in-flight tickers from persistent staging files.

        On restart, any ticker with a staging file in "staged" state was
        interrupted mid-extraction. We mark it as in-flight so it won't be
        re-enqueued until the current extraction completes or fails.
        Tickers with "extracted" status are considered complete and ready for
        the next pipeline stage. "failed" status tickers are available for retry.
        """
        with self.lock:
            for staging_file in self.staging_dir.glob("*.jsonl"):
                ticker = staging_file.stem
                try:
                    # Read the last line to get current status
                    with open(staging_file, "r", encoding="utf-8") as f:
                        lines = f.readlines()
                    if not lines:
                        continue
                    last_record = json.loads(lines[-1])
                    status = last_record.get("status")
                    if status == "staged":
                        # Was being extracted when process died; treat as in-flight
                        self.in_flight_tickers.add(ticker)
                except (json.JSONDecodeError, OSError):
                    # Corrupt or unreadable; ignore and let normal logic handle it
                    pass

    def begin_shutdown(self) -> None:
        """Stop accepting new work. Idempotent, so a second signal is safe.

        Separate from :meth:`shutdown` because the two happen at different
        times and for different reasons: this is called the instant a signal
        arrives, while ``shutdown`` runs later and may be given a deadline it
        cannot meet. Closing the door first means the wait is against a fixed
        amount of work rather than a moving target.
        """
        with self.lock:
            self.draining = True

    def is_in_flight(self, ticker: str) -> bool:
        """Check if a ticker is currently being processed."""
        clean = ticker.strip().upper()
        with self.lock:
            return clean in self.in_flight_tickers

    def is_staged(self, ticker: str) -> bool:
        """Check if an ingestion staging artifact exists and is complete.

        Returns True for "extracted" status (ready for next stage) and False for
        "failed" or "staged" (in progress or failed, available for retry).
        """
        clean = ticker.strip().upper()
        target_file = self.staging_dir / f"{clean}.jsonl"
        if not target_file.is_file() or target_file.stat().st_size == 0:
            return False
        try:
            with open(target_file, "r", encoding="utf-8") as f:
                lines = f.readlines()
            if not lines:
                return False
            last_record = json.loads(lines[-1])
            status = last_record.get("status")
            return status == "extracted"
        except (json.JSONDecodeError, OSError):
            return False

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
            if self.draining:
                # Shutting down. Starting this now would either be abandoned
                # partway through an extraction or force the drain past its
                # deadline; a refused enqueue costs the caller one retry after
                # the restart.
                return False
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

        # Generate a request ID for this ingestion job
        request_id = generate_request_id()
        token = set_request_id(request_id)
        timer = StageTimer(request_id=request_id)

        clean_ticker = ticker.strip().upper()
        target_file = self.staging_dir / f"{clean_ticker}.jsonl"

        try:
            # Step A: Write initial intent { "ticker": ticker, "status": "staged", "timestamp": ... }
            with timer.stage("staging_write"):
                timestamp_staged = datetime.now(timezone.utc).isoformat()
                initial_record: dict[str, Any] = {
                    "ticker": clean_ticker,
                    "status": "staged",
                    "timestamp": timestamp_staged,
                    "request_id": request_id,
                }
                if metadata is not None:
                    initial_record["metadata"] = metadata
                self._write_atomic(clean_ticker, initial_record)

            # Step B: Fetch the latest 10-K via SECRuntimeFetcher.fetch_latest_filing_html(ticker)
            with timer.stage("sec_fetch"):
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
            with timer.stage("cleaning"):
                narrative = clean_and_truncate_section(raw_html)

            # Step D: Run triple extraction via ColdStartExtractor.extract(text, ticker)
            with timer.stage("extraction"):
                try:
                    payload = ColdStartExtractor.extract(narrative, clean_ticker)
                except TypeError:
                    extractor = ColdStartExtractor()
                    payload = extractor.extract(narrative, clean_ticker)
                except AttributeError:
                    extractor = ColdStartExtractor()
                    payload = extractor.extract_triples(narrative, target_ticker=clean_ticker)

            # Step E: Atomically overwrite data/staging/{ticker}.jsonl with status: "extracted"
            with timer.stage("staging_finalize"):
                timestamp_extracted = datetime.now(timezone.utc).isoformat()
                payload_data = (
                    payload.model_dump() if hasattr(payload, "model_dump") else payload
                )
                extracted_record = {
                    "ticker": clean_ticker,
                    "status": "extracted",
                    "timestamp": timestamp_extracted,
                    "payload": payload_data,
                    "request_id": request_id,
                    "stage_latencies_ms": timer.as_dict(),
                }
                self._write_atomic(clean_ticker, extracted_record)

            record_ingestion_job(clean_ticker, "success")
            log.info(
                "ingestion_complete",
                extra={
                    "request_id": request_id,
                    "ticker": clean_ticker,
                    "stage_latencies_ms": timer.as_dict(),
                },
            )

        except Exception as e:
            # Error Handling: atomically write { "ticker": ticker, "status": "failed", "error": str(e) }
            record_ingestion_job(clean_ticker, "failed")
            record_external_api_failure("ingestion", type(e).__name__)
            failed_record = {
                "ticker": clean_ticker,
                "status": "failed",
                "error": str(e),
                "request_id": request_id,
            }
            try:
                self._write_atomic(clean_ticker, failed_record)
            except Exception:
                pass
            log.error(
                "ingestion_failed",
                extra={
                    "request_id": request_id,
                    "ticker": clean_ticker,
                    "error": str(e),
                    "stage_latencies_ms": timer.as_dict(),
                },
            )
        finally:
            with self.lock:
                self.in_flight_tickers.discard(clean_ticker)
            clear_request_id(token)

        return target_file

    def shutdown(self, wait: bool = True) -> None:
        """Cleanly shutdown the worker executor.

        Always calls :meth:`begin_shutdown` first, so a caller that only wants to
        stop new work has one method to reach for.

        ``cancel_futures`` drops work that has not started. A staged ticker is
        re-enqueueable after a restart, so abandoning a queued extraction loses
        nothing; *not* cancelling it means ``wait=True`` blocks behind an
        extraction nobody will see the result of.
        """
        self.begin_shutdown()
        kwargs: dict[str, Any] = {"wait": wait}
        if sys.version_info >= (3, 9):
            kwargs["cancel_futures"] = True
        self.executor.shutdown(**kwargs)
