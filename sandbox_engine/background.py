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

    def _async_stage_full_ingestion(
        self,
        ticker: str,
        metadata: dict[str, Any],
    ) -> Path:
        """Worker task: writes staging record using atomic rename."""
        try:
            timestamp = datetime.now(timezone.utc).isoformat()
            record = {
                "ticker": ticker,
                "status": "staged",
                "timestamp": timestamp,
                "metadata": metadata,
            }
            line = json.dumps(record) + "\n"

            target_file = self.staging_dir / f"{ticker}.jsonl"
            tmp_file = (
                self.staging_dir
                / f".{ticker}.tmp.{os.getpid()}.{threading.get_ident()}"
            )

            # Atomic write to temporary file then replace
            with open(tmp_file, "w", encoding="utf-8") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())

            os.replace(tmp_file, target_file)
            return target_file
        finally:
            with self.lock:
                self.in_flight_tickers.discard(ticker)

    def shutdown(self, wait: bool = True) -> None:
        """Cleanly shutdown the worker executor."""
        self.executor.shutdown(wait=wait)
