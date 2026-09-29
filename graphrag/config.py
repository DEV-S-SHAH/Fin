"""Runtime configuration for the graph and the extraction pipeline."""

from __future__ import annotations

import os
from dataclasses import dataclass

BYTES_PER_MB = 1024 * 1024

#: Environment variable that overrides the UI port, and the port used when it
#: is unset. Held here rather than in the argparse default so ``make_server``,
#: ``serve`` and the CLI all resolve the same value from one place.
UI_PORT_ENV = "PORT_GRAPHRAG_UI"
DEFAULT_UI_PORT = 8765


def default_ui_port() -> int:
    """The port the UI binds, from ``PORT_GRAPHRAG_UI`` or the default.

    An explicit ``--port`` always wins; this is only consulted when the caller
    expressed no preference. A value that is not a usable port is an error
    rather than a silent fall back: a typo'd ``PORT_GRAPHRAG_UI=876O`` would
    otherwise look applied while the server quietly listened somewhere else, and
    an out-of-range integer such as ``70000`` would instead fail much later,
    inside ``bind``, as a bare ``OverflowError``.
    """
    raw = os.environ.get(UI_PORT_ENV, "").strip()
    if not raw:
        return DEFAULT_UI_PORT
    try:
        port = int(raw)
    except ValueError:
        port = 0
    if not 1 <= port <= 65535:
        raise ValueError(
            f"{UI_PORT_ENV}={raw!r} is not a usable port; expected an integer "
            f"between 1 and 65535"
        ) from None
    return port


@dataclass(frozen=True)
class GraphRAGConfig:
    """Knobs for graph creation, chunking, extraction, and retrieval.

    The defaults match the operating envelope requested for this deployment:
    an in-process graph with a 256 MB buffer pool, and 800-token chunks with a
    100-token sliding overlap.
    """

    buffer_pool_size_mb: int = 256
    max_num_threads: int = 0  # 0 -> let the engine choose

    chunk_tokens: int = 800
    chunk_overlap_tokens: int = 100

    # Retrieval breadth. Hop 1 is the immediate neighbourhood; hop 2 adds the
    # second ring, which is where most multi-hop relations become visible.
    hops: int = 2
    max_context_nodes: int = 120
    max_context_edges: int = 240

    # LLM wiring. ``provider`` is resolved at construction time unless pinned.
    provider: str | None = None
    model: str | None = None
    temperature: float = 0.0
    max_retries: int = 3

    # Extraction guards. Guards exist to keep a hallucinating model from
    # growing the graph without bound; they are not domain knowledge.
    max_entities_per_chunk: int = 40
    max_relationships_per_chunk: int = 60
    min_entity_name_length: int = 2
    min_description_length: int = 8

    @property
    def buffer_pool_size(self) -> int:
        """Buffer pool size in bytes, as the engine expects."""
        return self.buffer_pool_size_mb * BYTES_PER_MB

    @property
    def stride_tokens(self) -> int:
        """Forward distance of the sliding window."""
        if self.chunk_overlap_tokens >= self.chunk_tokens:
            raise ValueError(
                "chunk_overlap_tokens must be smaller than chunk_tokens "
                f"(got {self.chunk_overlap_tokens} >= {self.chunk_tokens})"
            )
        return self.chunk_tokens - self.chunk_overlap_tokens

    @classmethod
    def from_env(cls, **overrides: object) -> GraphRAGConfig:
        """Build a config from ``GRAPHRAG_*`` environment variables."""
        env: dict[str, object] = {}
        for name in (
            "buffer_pool_size_mb",
            "max_num_threads",
            "chunk_tokens",
            "chunk_overlap_tokens",
            "hops",
            "max_context_nodes",
            "max_context_edges",
            "temperature",
            "max_retries",
        ):
            raw = os.environ.get(f"GRAPHRAG_{name.upper()}")
            if raw is not None:
                env[name] = int(raw) if _is_int(raw) else float(raw)
        for name in ("provider", "model"):
            raw = os.environ.get(f"GRAPHRAG_{name.upper()}")
            if raw:
                env[name] = raw
        env.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**env)  # type: ignore[arg-type]


def _is_int(raw: str) -> bool:
    try:
        int(raw)
    except ValueError:
        return False
    return True
