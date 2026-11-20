"""Per-session graph management with TTL destruction.

Sessions live in an in-memory dict keyed by session ID. Each session holds one
SessionGraph scoped to one company. A background sweeper destroys sessions
after SESSION_TTL_SECONDS of inactivity (default 30 min). Nothing persists
between sessions — the graph is built just-in-time and destroyed when the
session ends.
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from .config import SESSION_TTL_SECONDS, IndiaConfig
from .graph import SessionGraph


class SessionManager:
    """Manages in-memory session graphs with TTL-based destruction."""

    def __init__(self, config: IndiaConfig | None = None, ttl_seconds: int | None = None) -> None:
        self.config = config or IndiaConfig()
        self.ttl_seconds = ttl_seconds or SESSION_TTL_SECONDS
        self._lock = threading.Lock()
        self._sessions: dict[str, dict[str, Any]] = {}
        self._sweeper: threading.Thread | None = None
        self._stop = threading.Event()
        self._start_sweeper()

    # -- session lifecycle --------------------------------------------------

    def create_session(self) -> str:
        """Create a new session and return its ID."""
        session_id = uuid.uuid4().hex
        now = datetime.now(timezone.utc)
        with self._lock:
            self._sessions[session_id] = {
                "session_id": session_id,
                "graph": None,
                "cin": None,
                "created_at": now,
                "last_accessed": now,
                "question_count": 0,
            }
        return session_id

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        """Return the session dict, updating last_accessed. None if missing/expired."""
        with self._lock:
            sess = self._sessions.get(session_id)
            if sess is None:
                return None
            if self._expired(sess):
                del self._sessions[session_id]
                return None
            sess["last_accessed"] = datetime.now(timezone.utc)
            return sess

    def get_or_create_graph(self, session_id: str) -> SessionGraph | None:
        """Return the session's graph, creating an empty one if needed."""
        sess = self.get_session(session_id)
        if sess is None:
            return None
        if sess.get("graph") is None:
            sess["graph"] = SessionGraph(cin=sess.get("cin") or "", company_name=sess.get("company_name") or "")
        return sess["graph"]

    def bind_company(self, session_id: str, cin: str, company_name: str = "") -> bool:
        """Bind a session to a company (CIN). Returns False if session missing/expired."""
        with self._lock:
            sess = self._sessions.get(session_id)
            if sess is None or self._expired(sess):
                return False
            sess["cin"] = cin
            sess["company_name"] = company_name
            sess["graph"] = SessionGraph(cin=cin, company_name=company_name)
            return True

    def record_question(self, session_id: str) -> None:
        """Increment the question count for a session (call on each Q&A)."""
        with self._lock:
            sess = self._sessions.get(session_id)
            if sess is not None:
                sess["question_count"] = sess.get("question_count", 0) + 1

    def destroy_session(self, session_id: str) -> bool:
        """Explicitly destroy a session (e.g. on logout)."""
        with self._lock:
            if session_id in self._sessions:
                del self._sessions[session_id]
                return True
            return False

    def list_sessions(self) -> list[dict[str, Any]]:
        """Debug: list active sessions (no graphs, just metadata)."""
        with self._lock:
            now = datetime.now(timezone.utc)
            out = []
            for sess in self._sessions.values():
                out.append({
                    "session_id": sess["session_id"],
                    "cin": sess.get("cin"),
                    "company_name": sess.get("company_name"),
                    "created_at": sess["created_at"].isoformat(),
                    "last_accessed": sess["last_accessed"].isoformat(),
                    "question_count": sess.get("question_count", 0),
                    "age_seconds": (now - sess["last_accessed"]).total_seconds(),
                })
            return out

    # -- TTL sweeper --------------------------------------------------------

    def _expired(self, sess: dict[str, Any]) -> bool:
        idle = (datetime.now(timezone.utc) - sess["last_accessed"]).total_seconds()
        return idle > self.ttl_seconds

    def _start_sweeper(self) -> None:
        """Background thread that destroys expired sessions every 60s."""
        def sweep() -> None:
            while not self._stop.is_set():
                self._stop.wait(60)
                if self._stop.is_set():
                    break
                with self._lock:
                    expired = [
                        sid for sid, sess in self._sessions.items()
                        if self._expired(sess)
                    ]
                    for sid in expired:
                        del self._sessions[sid]
        self._sweeper = threading.Thread(target=sweep, daemon=True, name="india-session-sweeper")
        self._sweeper.start()

    def stop(self) -> None:
        """Stop the sweeper (call on shutdown)."""
        self._stop.set()
        if self._sweeper is not None:
            self._sweeper.join(timeout=2)
