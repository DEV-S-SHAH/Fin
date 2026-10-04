"""Bounded, signal-driven graceful shutdown for the FinGraph HTTP servers.

Why this module exists
----------------------
The servers previously handled ``KeyboardInterrupt`` and nothing else. SIGTERM --
which is what a supervisor, ``docker stop``, ``systemctl stop`` and a CI timeout
all send -- had no handler at all, so it killed the process on the default
disposition: no log line, no record of what was in flight, and the request
threads cut off mid-query.

The shutdown they did have was also unsafe, which is the part worth stating
precisely because it is invisible from reading ``serve()``::

    for server in servers:
        server.shutdown()
        server.server_close()
    kg.close()

``_listeners`` sets ``daemon_threads = True``, and ``socketserver._Threads``
deliberately refuses to track daemon threads::

    def append(self, thread):
        self.reap()
        if thread.daemon:
            return
        super().append(thread)

So ``ThreadingMixIn.server_close()`` joins an *empty* list and returns at once,
however many requests are mid-flight. ``kg.close()`` therefore ran while live
request threads were still querying through that same handle. See
``tests/test_graceful_shutdown.py`` for the regression that pins the ordering.

The obvious fix -- drop ``daemon_threads`` and let ``block_on_close`` do the
joining -- is worse. These handlers speak HTTP/1.1 with keep-alive, so a browser
holds its connection thread open between requests for the life of the page.
``server_close()`` joining those waits on an idle socket indefinitely, which
turns "graceful" into "hangs until something kills it". Bounded waiting on a
counter this module owns is the only option that both returns promptly and never
cuts a live query short before the deadline. That is what it does.

No new dependency: ``threading``, ``signal``, ``time``, ``logging`` and
``http.server`` are all standard library, and the timeout arrives through the
server's existing ``_setting``/``.env`` mechanism.

One limit worth knowing, so it is not rediscovered painfully: CPython restores
the default disposition for signals during interpreter finalisation. A SIGTERM
that arrives *after* the drain has finished and the process is already tearing
down kills it, handler or no handler -- reproducible in six lines with no
FinGraph code involved. The handler is deliberately left installed anyway,
because it does protect the real window: the stretch between the drain being
requested and the last line of teardown, which is where the graph handle is
still closing. Signals that land after that are simply answered with the
disposition Python insists on, and they are harmless -- everything they could
have damaged has already been closed.
"""

from __future__ import annotations

import logging
import re
import signal
import sys
import threading
import time
from typing import Any, Callable, Iterable, Sequence

#: Same logger as the servers, so a drain's lines interleave with the request
#: lines they refer to instead of arriving under a second logger's heading.
log = logging.getLogger("graphrag_ui")

#: Fallback only. The value actually used is passed in by the caller, which
#: reads it through the server's existing ``_setting`` so ``.env`` is honoured.
DEFAULT_SHUTDOWN_TIMEOUT = 30.0

#: Paths that keep answering while the server drains.
#:
#: A load balancer has to be able to *see* a draining instance to stop sending
#: it traffic, so ``/readyz`` must answer -- with ``ready: false``. And a
#: supervisor watching a process that is draining needs liveness to stay 200 so
#: it can tell "shutting down on purpose" from "wedged". Neither is work, so
#: neither is refused; everything else is.
DRAIN_EXEMPT_PATHS = frozenset({"/healthz", "/readyz"})

#: Mutating Cypher, refused once the drain starts.
#: Matched on standalone tokens outside string literals -- see :func:`is_mutation`.
_WRITE_CLAUSES = (
    "CREATE", "INSERT", "DELETE", "DETACH", "MERGE", "SET", "REMOVE",
    "DROP", "COPY", "ALTER", "RENAME",
)
_WRITE_RE = re.compile(
    r"\b(?:%s)\b" % "|".join(_WRITE_CLAUSES),
    re.IGNORECASE,
)
#: Single- and double-quoted Cypher string literals, including their escapes.
_STRING_RE = re.compile(r"'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"")


class ShutdownStarted(Exception):
    """Raised when a request arrives after the drain has begun.

    Carries nothing but a reason string: it is turned into a 503 body, and a
    traceback in an HTTP response is a disclosure the drain must not create.
    """


class DatabaseSealed(Exception):
    """Raised when something tries to write after the graph is sealed."""


def is_mutation(cypher: str) -> bool:
    """Whether *cypher* can change the database.

    String literals are removed first, so a question containing the word ``set``
    -- ``MATCH (n) WHERE n.tag = 'SET' RETURN n`` -- is not mistaken for an
    update. False positives here are worse than they look: this runs inside
    ``execute``, so a read misread as a write would be refused from a request
    the drain is supposed to be letting finish.

    Every keyword in :data:`_WRITE_CLAUSES` is reserved in Cypher, so it cannot
    legitimately appear in a read as an identifier or an alias. That is what
    makes the token test sound rather than merely convenient.
    """
    return bool(_WRITE_RE.search(_STRING_RE.sub(" ", cypher)))


class ShutdownCoordinator:
    """Owns the drain: who may still work, and when the process may exit.

    One instance per server process, shared by every listener and reachable from
    request threads through ``server.lifecycle``. All the mutable state sits
    behind one condition variable, so "drain started" and "last request
    finished" cannot interleave into a state where both are believed true.
    """

    def __init__(
        self,
        service: str = "graphrag-ui",
        timeout: float = DEFAULT_SHUTDOWN_TIMEOUT,
    ) -> None:
        self.service = service
        self.timeout = float(timeout)
        self._cond = threading.Condition()
        self._draining = False
        self._escalated = False
        self._inflight = 0
        self._reason: str | None = None
        self._signals: list[int] = []
        self._finished = threading.Event()
        #: Set by :meth:`begin`, i.e. by the first signal or Ctrl-C.
        #: ``serve_until_signalled`` blocks on this so the server keeps serving
        #: until something actually asks it to stop -- starting the drain
        #: eagerly would close the database out from under the first request.
        self._requested = threading.Event()
        #: (step, detail) in the order the drain ran them. Tests assert on this
        #: rather than on log text, which no reformatting can then break.
        self.steps: list[tuple[str, str]] = []

    # ── state ──────────────────────────────────────────────────────────────

    @property
    def draining(self) -> bool:
        with self._cond:
            return self._draining

    @property
    def escalated(self) -> bool:
        with self._cond:
            return self._escalated

    @property
    def inflight(self) -> int:
        with self._cond:
            return self._inflight

    def admits(self, path: str) -> bool:
        """Whether a request to *path* may still start.

        Health and readiness are exempt by design -- see
        :data:`DRAIN_EXEMPT_PATHS`.
        """
        if path.split("?", 1)[0] in DRAIN_EXEMPT_PATHS:
            return True
        return not self.draining

    def report(self) -> dict[str, Any]:
        """A snapshot for health payloads and for tests."""
        with self._cond:
            return {
                "draining": self._draining,
                "escalated": self._escalated,
                "inflight_requests": self._inflight,
                "shutdown_timeout_seconds": self.timeout,
                "shutdown_reason": self._reason,
            }

    # ── request accounting ─────────────────────────────────────────────────

    def enter_request(self, path: str) -> bool:
        """Count a request in, or refuse it.

        Returns ``True`` when the caller must pair the call with
        :meth:`exit_request`. Raises :class:`ShutdownStarted` when the drain has
        begun, so the refusal happens at admission -- before any handler body
        runs -- rather than partway through a query.
        """
        route = path.split("?", 1)[0]
        with self._cond:
            if route not in DRAIN_EXEMPT_PATHS and self._draining:
                raise ShutdownStarted(
                    f"{self.service} is shutting down and is not accepting "
                    f"new requests"
                )
            self._inflight += 1
            return True

    def exit_request(self) -> None:
        with self._cond:
            if self._inflight > 0:
                self._inflight -= 1
            if self._inflight == 0:
                self._cond.notify_all()

    # ── drain control ──────────────────────────────────────────────────────

    def begin(self, reason: str) -> bool:
        """Start draining. Returns ``False`` if a drain was already running.

        Idempotent, and it has to be: two signals arriving together is ordinary
        (a supervisor escalating, then a user pressing Ctrl-C), and a second
        ``server.shutdown()`` on a closed socket or a second ``kg.close()`` on a
        closed LadybugDB handle is exactly the corruption the double signal
        path must not cause.
        """
        with self._cond:
            if self._draining:
                return False
            self._draining = True
            self._reason = reason
            self._cond.notify_all()
        self._requested.set()
        self._step("drain_started", reason)
        return True

    def escalate(self, reason: str) -> bool:
        """Cut the remaining wait short. Returns ``False`` if already escalated.

        The second signal is treated as "stop waiting". Waiting out the full
        deadline after someone has already asked twice is how a supervisor ends
        up escalating to SIGKILL, which is the abrupt death this module exists to
        avoid -- just later and with less information.
        """
        with self._cond:
            if self._escalated:
                return False
            self._escalated = True
            self._cond.notify_all()
        self._step("escalated", reason)
        return True

    def record_signal(self, signum: int) -> str:
        """Note a signal and decide what it means. Returns what was done.

        Called from the signal handler, so it does almost nothing: it must not
        block, and it must not call ``server.shutdown()``. That method waits on
        an event the *main* thread sets from inside ``serve_forever`` -- and the
        main thread is the thread the handler is running on, so calling it here
        would deadlock the process instead of stopping it.
        """
        with self._cond:
            self._signals.append(signum)
            count = len(self._signals)
            draining = self._draining
        name = signal.Signals(signum).name if signum in _SIGNAL_VALUES else str(signum)
        if self._finished.is_set():
            # The drain already completed: graph sealed, handles closed, ports
            # released. There is nothing left to protect, and reverting to the
            # default disposition now would let a supervisor's retry kill the
            # process mid-exit and report a signal death for a clean shutdown.
            # Swallowing it is the whole point of having a handler at all.
            self._step("signal_after_drain", f"{name} (signal {signum})")
            return "ignored"
        if not draining:
            self.begin(f"{name} (signal {signum})")
            return "began"
        if count >= 2:
            return ("escalated" if self.escalate(f"repeat {name} (signal {signum})")
                    else "ignored")
        self._step("signal_ignored", f"repeat {name} (signal {signum})")
        return "ignored"

    def _step(self, step: str, detail: str = "") -> None:
        self.steps.append((step, detail))
        log.info("[%s] shutdown: %s%s", self.service, step,
                 f" ({detail})" if detail else "")

    # ── the sequence ───────────────────────────────────────────────────────

    def wait_for_idle(self, deadline: float) -> bool:
        """Wait for in-flight requests to finish. Returns ``True`` if idle.

        Bounded by *deadline* on purpose (see the module docstring): the
        alternative -- joining unconditionally -- is what hangs on keep-alive.
        """
        with self._cond:
            while self._inflight:
                if self._escalated:
                    return False
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(min(remaining, 0.05))
            return True

    def drain(
        self,
        servers: Sequence[Any] = (),
        graph: Any = None,
        background: Any = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Run the whole sequence and return what happened.

        Ordered so that the cheapest reversals happen first and the one
        irreversible step happens last:

        1. stop accepting connections
        2. refuse new requests (already done by :meth:`begin`)
        3. stop background ingestion from starting anything
        4. let in-flight requests finish, within the deadline
        5. close listeners
        6. stop background workers, within what is left of the deadline
        7. seal the graph and close its handle
        8. flush logging

        The graph handle is closed in step 7, after step 4, because closing it
        while a request is mid-query is the bug described in the module
        docstring. Nothing after step 7 can fail in a way that leaves the
        database inconsistent: the handle is read-only in every shipped
        configuration, so there is nothing to flush and nothing to commit.
        """
        budget = self.timeout if timeout is None else float(timeout)
        deadline = time.monotonic() + budget
        summary: dict[str, Any] = {
            "timeout_seconds": budget,
            "signals": list(self._signals),
            "reason": self._reason,
            "forced": False,
            "dropped_requests": 0,
            "background_stopped": None,
            "graph_closed": False,
        }

        def remaining(cap: float | None = None) -> float:
            left = deadline - time.monotonic()
            return left if cap is None else max(0.0, min(left, cap))

        def record(name: str, ok: bool = True, detail: str = "") -> None:
            self._step(name, detail if ok else f"{detail} (timed out)")

        # 1. Stop accepting. Runs on this thread, which is *not* the thread in
        #    serve_forever -- that is what keeps shutdown() from deadlocking.
        for server in servers:
            if not _bounded(lambda s=server: s.shutdown(), remaining(2.0),
                            f"stop-accepting:{server.server_address[1]}"):
                record("stop_accepting", False, str(server.server_address[1]))

        # 2/3. New work is already refused by `begin`; stop the queue taking any
        #      more before waiting on it, so the wait cannot grow.
        if background is not None:
            _safe_call(getattr(background, "begin_shutdown", None), "background.begin_shutdown")
            self._step("ingestion_closed")

        # 4. Let live requests finish.
        if not self.wait_for_idle(deadline):
            summary["forced"] = True
            summary["dropped_requests"] = self.inflight
            record("drain_inflight", False, f"{self.inflight} request(s) still running")
        else:
            record("drain_inflight", True, "all requests finished")

        # 5. Close listeners. Safe now: nothing is accepting and (usually)
        #    nothing is in flight.
        for server in servers:
            _safe_call(server.server_close, "server_close")

        # 6. Background workers. Bounded because ThreadPoolExecutor.shutdown
        #    cannot be interrupted, and an interrupted ingestion task can
        #    otherwise hold the process open past the deadline.
        if background is not None:
            summary["background_stopped"] = _bounded(
                lambda: _safe_call(background.shutdown, "background.shutdown"),
                remaining(),
                "background.stop",
            )
            record("background_stopped", bool(summary["background_stopped"]))

        # 7. The irreversible step, last.
        if graph is not None:
            _safe_call(getattr(graph, "begin_shutdown", None), "graph.begin_shutdown")
            _safe_call(graph.close, "graph.close")
            summary["graph_closed"] = True
            record("graph_closed")

        # 8. Logs last: every step above wanted to be written down.
        _safe_call(logging.shutdown, "logging.shutdown")
        _flush_streams()
        self._step("logs_flushed")
        self._finished.set()
        summary["elapsed_seconds"] = round(
            max(0.0, budget - remaining()), 3
        )
        summary["steps"] = list(self.steps)
        return summary

    def wait_finished(self, timeout: float | None = None) -> bool:
        return self._finished.wait(timeout if timeout is not None else self.timeout)

    def wait_requested(self, timeout: float | None = None) -> bool:
        """Block until a drain has been asked for. ``False`` if *timeout* passed.

        Returns ``False`` only on timeout; ``wait_finished`` covers the case
        where a caller should stop caring.
        """
        return self._requested.wait(timeout)


_SIGNAL_VALUES = {int(s) for s in signal.Signals}


def _safe_call(fn: Callable[..., Any] | None, what: str) -> Any:
    """Call *fn*, turning a failure into a log line.

    A shutdown that dies halfway is worse than one that finishes imperfectly:
    the whole point is that the process reaches the end in a state somebody can
    reason about. So every step is attempted even if the one before it failed.
    """
    if fn is None:
        return None
    try:
        return fn()
    except Exception as exc:
        log.warning("[shutdown] %s failed: %s: %s", what, type(exc).__name__, exc)
        return None


def _bounded(fn: Callable[[], Any], budget: float, what: str) -> bool:
    """Run *fn* on a helper thread and give up after *budget* seconds.

    ``socketserver.shutdown()`` and ``ThreadPoolExecutor.shutdown(wait=True)``
    both wait on something this thread cannot influence, so the only way to keep
    a hard deadline is to stop waiting for them. The helper is a daemon so it
    cannot itself hold the process open.
    """
    if budget <= 0:
        return False
    done = threading.Event()

    def runner() -> None:
        _safe_call(fn, what)
        done.set()

    thread = threading.Thread(target=runner, daemon=True, name=f"shutdown-{what}")
    thread.start()
    finished = done.wait(budget)
    if not finished:
        log.warning("[shutdown] %s did not finish within %.2fs", what, budget)
    return finished


def _flush_streams() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:
            pass


def install_signal_handlers(coordinator: ShutdownCoordinator) -> dict[int, Any]:
    """Route SIGTERM and SIGINT into *coordinator*. Returns previous handlers.

    Both are trapped, not just SIGINT: SIGTERM is what a supervisor, a container
    runtime and a CI timeout actually send, so handling only Ctrl-C leaves the
    commonest stop path the abrupt one.

    Degrades to a warning when it cannot install -- ``signal.signal`` only works
    from the main thread, so a server embedded in a thread (several tests do
    this) keeps its existing ``KeyboardInterrupt`` behaviour instead of raising.
    """
    previous: dict[int, Any] = {}

    def handler(signum: int, _frame: Any) -> None:
        # Deliberately tiny. The drain runs on its own thread (see
        # `run_until_signalled`); this only records and decides.
        try:
            coordinator.record_signal(signum)
        except Exception as exc:  # pragma: no cover - handler must never raise
            log.warning("[shutdown] signal handler failed: %s", type(exc).__name__)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            previous[sig] = signal.signal(sig, handler)
        except (ValueError, OSError) as exc:
            log.warning(
                "[%s] cannot trap %s (%s); Ctrl-C still stops the server, "
                "a supervisor's SIGTERM will not be graceful",
                coordinator.service, sig.name, type(exc).__name__,
            )
    return previous


def restore_signal_handlers(previous: dict[int, Any]) -> None:
    for sig, handler in previous.items():
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):
            pass


def serve_until_signalled(
    servers: Sequence[Any],
    coordinator: ShutdownCoordinator,
    graph: Any = None,
    background: Any = None,
    **drain_kwargs: Any,
) -> dict[str, Any]:
    """Serve on *servers* until a signal, then drain. Returns the summary.

    Called from ``serve()`` in place of a bare ``serve_forever()``. The primary
    listener stays on the calling thread so Ctrl-C lands where the user is
    looking; extra listeners keep their own threads; the drain runs on a third,
    which is what lets it call ``server.shutdown()`` at all.
    """
    for server in servers:
        server.lifecycle = coordinator
    if graph is not None:
        graph.lifecycle = coordinator

    for server in servers[1:]:
        threading.Thread(
            target=server.serve_forever, daemon=True,
            name=f"http-{server.server_address[1]}",
        ).start()

    summary: dict[str, Any] = {}

    def drain() -> None:
        nonlocal summary
        # Block until a signal actually arrives. Eagerly running the sequence
        # here would tear the database down the moment the process started.
        if not coordinator.wait_requested():
            return
        summary = coordinator.drain(
            servers=servers, graph=graph, background=background, **drain_kwargs
        )

    worker = threading.Thread(target=drain, daemon=True, name="shutdown-drain")
    worker.start()
    try:
        servers[0].serve_forever()
    except KeyboardInterrupt:
        # Reached only if signal handling could not be installed, or between the
        # handler being torn down and the drain finishing.
        coordinator.begin("KeyboardInterrupt")
        worker.join(coordinator.timeout + 5.0)
    worker.join(coordinator.timeout + 5.0)
    return summary


__all__ = [
    "DatabaseSealed",
    "DEFAULT_SHUTDOWN_TIMEOUT",
    "DRAIN_EXEMPT_PATHS",
    "ShutdownCoordinator",
    "ShutdownStarted",
    "install_signal_handlers",
    "is_mutation",
    "restore_signal_handlers",
    "serve_until_signalled",
]