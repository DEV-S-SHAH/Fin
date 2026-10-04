"""Bounded resources for the stdlib HTTP servers, without replacing them.

Why this exists
---------------
``ThreadingHTTPServer`` spawns one thread per accepted connection and imposes no
limit on how many that is. Measured against the current handler, on this machine:

* 200 idle keep-alive connections -> 219 threads, held **indefinitely**.
  ``BaseHTTPRequestHandler.timeout`` is ``None``, so after answering, the thread
  blocks forever in ``readline()`` waiting for a request that never comes.
* 100 sockets that send a partial request line and stall -> 100 more threads,
  also held indefinitely, for the same reason.
* Disconnecting reaps the thread (measured: 54 -> 4 within one second), so there
  is no leak -- the exposure is that *keeping a socket open costs a thread*, with
  no ceiling on how many sockets may do so.

``ThreadingHTTPServer`` is kept. What changes is that each of those costs is now
bounded, using only the standard library:

* a socket timeout on the handler, which ends idle and stalled connections;
* a connection ceiling enforced at ``process_request``, before a thread exists;
* a separate, lower ceiling on streaming connections, so a burst of long-lived
  SSE requests cannot consume the whole pool;
* a listen backlog wide enough to absorb a connection burst rather than refuse
  it at the kernel;
* ``handle_error`` no longer prints a traceback for a client that simply hung up.

``verify_request``/``process_request`` are the documented ``socketserver``
extension points, and a handler-class ``timeout`` is what ``StreamRequestHandler``
already honours, so none of this forks the server.
"""

from __future__ import annotations

import os
import socket
import threading
from dataclasses import dataclass
from http.server import ThreadingHTTPServer
from typing import Any, Callable

__all__ = [
    "ENV_NAMES",
    "BoundedThreadingHTTPServer",
    "ConnectionCounter",
    "Limits",
]

#: Sent to a connection refused at the ceiling. A real status line rather than a
#: bare reset: an operator watching a client see "503" learns the pool is full,
#: where an empty RST looks like a network fault.
#:
#: The length is computed from the body rather than written by hand. A literal
#: here is a trap: editing the message without editing the count produces a
#: response whose ``Content-Length`` disagrees with its body, and a conforming
#: client then reads a truncated, unparsable JSON object -- the operator sees
#: "503 with no explanation" instead of the reason.
_REFUSAL_BODY = (
    b'{"error":"server connection limit reached; retry shortly",'
    b'"limit":"max_connections"}'
)
_REFUSAL = (
    b"HTTP/1.1 503 Service Unavailable\r\n"
    b"Content-Type: application/json; charset=utf-8\r\n"
    b"Content-Length: "
    + str(len(_REFUSAL_BODY)).encode("ascii")
    + b"\r\n"
    b"Connection: close\r\n"
    b"\r\n"
    + _REFUSAL_BODY
)


#: The environment variables that tune the ceilings, with the value used when
#: one is unset or unparsable. Defined here rather than in any one server so
#: that every HTTP entry point in the repository answers to the same names and
#: there is a single place to change a default.
ENV_NAMES = {
    "request_timeout": "REQUEST_TIMEOUT",
    "max_connections": "MAX_CONNECTIONS",
    "max_sse_connections": "MAX_SSE_CONNECTIONS",
    "sse_max_seconds": "SSE_MAX_SECONDS",
    "listen_backlog": "LISTEN_BACKLOG",
}


def _as_float(raw: str, fallback: float) -> float:
    """Parse a float, refusing to let bad configuration take the server down.

    A typo in ``.env`` should not be the reason the UI refuses to start; it
    should fall back to the known-good value and say so via the banner.
    """
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return fallback
    return value if value > 0 else fallback


def _as_int(raw: str, fallback: int) -> int:
    value = _as_float(raw, float(fallback))
    return int(value) if value >= 1 else fallback


@dataclass(frozen=True)
class Limits:
    """Ceilings for one listener.

    Defaults here are what every server applies unless it overrides them via
    :meth:`from_env`.
    """

    #: Seconds a connection may sit idle (or stall mid-request) before the
    #: handler closes it. This is what stops an idle keep-alive socket from
    #: pinning a thread forever.
    request_timeout: float = 30.0
    #: Maximum simultaneously open connections, counted at accept time.
    max_connections: int = 256
    #: Maximum simultaneous streaming (SSE) responses. Deliberately far below
    #: ``max_connections``: a stream is held for as long as the model takes, so
    #: letting streams compete for the whole pool is how normal requests get
    #: starved.
    max_sse_connections: int = 32
    #: Hard ceiling on one streaming response. Enforced at stage and token
    #: boundaries; see the note in ``query_ui._handle_sse_ask`` for why the
    #: absolute worst case is still the model timeout.
    sse_max_seconds: float = 900.0
    #: Listen backlog. The stdlib default is 5, which refuses a burst at the
    #: kernel before the server ever sees it.
    listen_backlog: int = 128

    @classmethod
    def from_env(cls, read: Callable[[str], str] | None = None) -> Limits:
        """Build the ceilings from the environment.

        *read* maps a variable name to its raw string value. Each server passes
        its own reader so the resolution order stays its own: ``query_ui``
        passes a reader that also consults ``.env`` (via ``_setting``), while
        the ``graphrag``-side servers read ``os.environ`` directly, which is
        what ``graphrag.config`` already does everywhere else.

        Unparsable or non-positive values fall back to the field default, so a
        mistake in configuration degrades to the safe number instead of
        crashing the process or, worse, silently disabling a ceiling.
        """
        if read is None:
            read = lambda name: os.environ.get(name, "")  # noqa: E731
        base = cls()
        return cls(
            request_timeout=_as_float(
                read(ENV_NAMES["request_timeout"]), base.request_timeout
            ),
            max_connections=_as_int(
                read(ENV_NAMES["max_connections"]), base.max_connections
            ),
            max_sse_connections=_as_int(
                read(ENV_NAMES["max_sse_connections"]), base.max_sse_connections
            ),
            sse_max_seconds=_as_float(
                read(ENV_NAMES["sse_max_seconds"]), base.sse_max_seconds
            ),
            listen_backlog=_as_int(
                read(ENV_NAMES["listen_backlog"]), base.listen_backlog
            ),
        )


class ConnectionCounter:
    """Thread-safe occupancy counters shared by one listener's connections.

    Separate from :class:`~sandbox_engine.shutdown.ShutdownCoordinator` on
    purpose. That one answers "is the drain finished?"; this one answers "may
    this connection exist?". Merging them would mean a saturated server also
    looked like a draining one, and ``/healthz`` would report the wrong thing.
    """

    def __init__(self, limits: Limits | None = None) -> None:
        self.limits = limits or Limits()
        self._cond = threading.Condition()
        self._active = 0
        self._active_sse = 0
        self._peak_active = 0
        self._refused = 0

    def try_acquire(self) -> bool:
        """Reserve a connection slot, or report the pool full."""
        with self._cond:
            if self._active >= self.limits.max_connections:
                self._refused += 1
                return False
            self._active += 1
            self._peak_active = max(self._peak_active, self._active)
            return True

    def release(self) -> None:
        with self._cond:
            if self._active > 0:
                self._active -= 1

    def try_acquire_sse(self) -> bool:
        """Reserve a streaming slot, or report the stream pool full."""
        with self._cond:
            if self._active_sse >= self.limits.max_sse_connections:
                return False
            self._active_sse += 1
            return True

    def release_sse(self) -> None:
        with self._cond:
            if self._active_sse > 0:
                self._active_sse -= 1

    @property
    def active(self) -> int:
        with self._cond:
            return self._active

    @property
    def active_sse(self) -> int:
        with self._cond:
            return self._active_sse

    def report(self) -> dict[str, Any]:
        with self._cond:
            return {
                "active_connections": self._active,
                "max_connections": self.limits.max_connections,
                "active_streams": self._active_sse,
                "max_streams": self.limits.max_sse_connections,
                "peak_connections": self._peak_active,
                "connections_refused": self._refused,
                "request_timeout_seconds": self.limits.request_timeout,
                "listen_backlog": self.limits.listen_backlog,
            }


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """``ThreadingHTTPServer`` with a ceiling on concurrent connections.

    The class stays what it is -- one thread per connection, daemon threads, the
    same accept loop. Only ``process_request`` changes: it refuses before a
    thread is created, so the ceiling is on threads, not on requests that have
    already been handed one.
    """

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        handler: type,
        *,
        limits: Limits | None = None,
        counter: ConnectionCounter | None = None,
    ) -> None:
        self._limits = limits or Limits()
        self._counter = counter if counter is not None else ConnectionCounter(self._limits)
        super().__init__(server_address, handler)
        # ``HTTPServer.server_activate`` already called ``listen`` with the
        # stdlib backlog of 5. Calling it again updates it; sockets do not need
        # to be rebound for that, and doing it here keeps the value per-instance
        # instead of mutating a class attribute every listener shares.
        self.socket.listen(self._limits.listen_backlog)

    @property
    def limits(self) -> Limits:
        return self._limits

    @property
    def connections(self) -> ConnectionCounter:
        return self._counter

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._counter.try_acquire():
            self._refuse(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            # The thread was never started, so nothing else will release.
            self._counter.release()
            raise

    def _refuse(self, request: Any) -> None:
        """Tell the client the pool is full, then close.

        Deliberately does not go through :meth:`shutdown_request`: that path is
        where the slot is released, and no slot was taken, so the count would go
        negative.
        """
        try:
            request.sendall(_REFUSAL)
        except OSError:
            # Already gone; the reset is the whole answer.
            pass
        try:
            request.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.close_request(request)

    def shutdown_request(self, request: Any) -> None:
        try:
            super().shutdown_request(request)
        finally:
            self._counter.release()

    def handle_error(self, request: Any, client_address: Any) -> None:
        """Log a routine disconnect briefly; traceback only for real faults.

        ``socketserver`` prints a full traceback for anything escaping a
        handler. A client that closes a tab mid-response is routine, and under a
        disconnect flood that traceback spam is itself resource consumption --
        one per dropped connection, on stderr, forever.
        """
        import sys
        import traceback

        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, TimeoutError)):
            # The peer went away mid-response. Expected; nothing to diagnose.
            return
        traceback.print_exc()

    def report(self) -> dict[str, Any]:
        return self._counter.report()