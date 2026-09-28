"""Load a local ``.env`` file into the process environment.

Provider credentials have to reach the pipeline somehow, and the shell is a poor
place to keep them: an ``export KEY=...`` typed into a launch command is written
to shell history and quoted verbatim in any transcript of the session that started
the server. A gitignored ``.env`` in the working directory keeps the same secret
out of both, and ``.env`` is already ignored by this repository.

The key still reaches the running process's own environment, because that is
where the SDK reads it from; this module only moves it out of the parts of a
launch that get logged and shared.

Precedence is the point of this module: a variable already present in the real
environment always wins, so any file value can be overridden for a single run
without editing the file. The file fills gaps; it never overrides intent.
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_FILENAME = ".env"
ENV_FILE_VAR = "GRAPHRAG_ENV_FILE"


def load_env_file(path: str | Path | None = None) -> dict[str, str]:
    """Apply ``path`` (default: ``./.env``) to ``os.environ`` for missing keys.

    Returns the mapping that was actually applied. A missing file yields ``{}``:
    a clean checkout without credentials is a normal state, not an error.
    """
    default = os.environ.get(ENV_FILE_VAR) or ENV_FILENAME
    target = Path(path) if path is not None else Path(default)
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError:
        return {}

    applied: dict[str, str] = {}
    for name, value in _parse(raw):
        if name in os.environ:
            continue
        os.environ[name] = value
        applied[name] = value
    return applied


def _parse(raw: str) -> list[tuple[str, str]]:
    """Turn ``.env`` text into ``(name, value)`` pairs in file order.

    Handles the subset a credentials file needs: comments, blank lines, an
    optional ``export`` prefix, and quoted values. A quoted value is taken
    verbatim, so a token containing ``#`` survives; a bare value has a trailing
    ``# comment`` removed.
    """
    pairs: list[tuple[str, str]] = []
    for line in raw.splitlines():
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        if entry.startswith("export "):
            entry = entry[len("export ") :].strip()
        name, separator, value = entry.partition("=")
        name = name.strip()
        if not separator or not name:
            continue
        pairs.append((name, _value(value.strip())))
    return pairs


def _value(value: str) -> str:
    """Unwrap a quoted value, or strip an inline comment from a bare one."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    head, marker, _ = value.partition(" #")
    return (head if marker else value).strip()
