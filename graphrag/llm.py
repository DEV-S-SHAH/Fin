"""LLM access with a strict JSON contract.

The extraction and answering steps both require structured output, so every
provider here is reduced to one method: :meth:`LLMClient.complete_json`, which
returns a parsed ``dict`` or raises :class:`LLMError`.

Providers
---------
``GeminiClient``        Google Gemini via its OpenAI-compatible endpoint, using
                        strict ``json_schema`` so the output is constrained to
                        the requested shape at decode time.
``OpenAIChatClient``    native JSON mode (``response_format={"type":"json_object"}``).
``AnthropicClient``     forced tool-use, which is Anthropic's equivalent of JSON mode.
``HeuristicClient``     no network, no model. Deterministic pattern extraction.

The heuristic provider exists so the pipeline is runnable and testable without
credentials. It is a lexical approximation, not a substitute for a model, and
:class:`HeuristicClient` says so in its ``is_model`` attribute so callers can
label their output honestly.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from typing import Any

log = logging.getLogger(__name__)

_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class LLMError(RuntimeError):
    """Raised when a provider cannot return usable JSON."""


# Gemini and NVIDIA both expose an OpenAI-compatible surface, so the OpenAI
# client is reused against these base URLs rather than adding a second HTTP
# implementation.
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"

# Default timeouts (seconds) - can be overridden via environment
DEFAULT_CONNECT_TIMEOUT = 10.0
DEFAULT_READ_TIMEOUT = 120.0
MAX_TOTAL_TIMEOUT = 300.0  # Hard ceiling: no external call blocks longer than this


def _is_retryable_error(exc: Exception) -> bool:
    """Whether *exc* is a transient error worth retrying.

    Retries only:
    - Rate limits (429)
    - Server errors (5xx)
    - Connection/timeout errors
    - Temporary network issues

    Does NOT retry:
    - Authentication errors (401, 403)
    - Invalid requests (400)
    - Not found (404)
    - Other client errors (4xx except 429)
    """
    text = str(exc).lower()

    # Check for rate limit indicators
    if any(marker in text for marker in ("429", "rate limit", "rate_limit", "quota", "resource_exhausted")):
        return True

    # Check for server errors
    if any(marker in text for marker in ("500", "502", "503", "504", "internal server error", "bad gateway", "service unavailable", "gateway timeout")):
        return True

    # Check for connection/timeout errors
    if any(marker in text for marker in ("connection", "timeout", "timed out", "connect", "dns", "network unreachable", "connection refused", "connection reset")):
        return True

    # Explicitly non-retryable: auth errors, bad requests, not found
    if any(marker in text for marker in ("401", "403", "400", "404", "unauthorized", "forbidden", "invalid request", "bad request", "not found")):
        return False

    # For OpenAI SDK exceptions, check status code if available
    if hasattr(exc, "status_code"):
        code = exc.status_code
        if code in (429, 500, 502, 503, 504):
            return True
        if code in (400, 401, 403, 404):
            return False

    # For httpx exceptions
    if hasattr(exc, "response") and hasattr(exc.response, "status_code"):
        code = exc.response.status_code
        if code in (429, 500, 502, 503, 504):
            return True
        if code in (400, 401, 403, 404):
            return False

    # Default: retry on unknown errors (conservative)
    return True


def _extract_status_code(exc: Exception) -> int | None:
    """Extract HTTP status code from an exception if available."""
    if hasattr(exc, "status_code"):
        return exc.status_code
    if hasattr(exc, "response") and hasattr(exc.response, "status_code"):
        return exc.response.status_code
    # Try to parse from error message
    text = str(exc)
    import re as _re
    match = _re.search(r"\b(4\d\d|5\d\d)\b", text)
    if match:
        try:
            return int(match.group(1))
        except ValueError:
            pass
    return None


class LLMClient(ABC):
    """Minimal structured-output interface used by the pipeline."""

    name: str = "base"
    is_model: bool = True
    # Providers that can constrain decoding to *schema* set this. When false
    # the schema is advisory and the response is still validated by the caller.
    supports_schema: bool = False

    @abstractmethod
    def complete_json(
        self, system: str, user: str, schema: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Return a JSON object for the given system/user prompt pair.

        *schema* is an optional JSON Schema. Providers that support constrained
        decoding should honour it; the rest may ignore it.
        """

    @abstractmethod
    def complete_text(self, system: str, user: str) -> str:
        """Return free text, used for the final synthesised answer."""


def _between(text: str, start_marker: str, end_marker: str) -> str:
    """Slice the payload a prompt wraps between two delimiter phrases."""
    body = text
    if start_marker in body:
        body = body.split(start_marker, 1)[1]
    if end_marker in body:
        body = body.split(end_marker, 1)[0]
    return body.strip()


def parse_json_object(raw: str) -> dict[str, Any]:
    if raw is None:
        raise LLMError("empty response")
    text = raw.strip()
    if not text:
        raise LLMError("empty response")

    candidates: list[str] = [text]
    fenced = _JSON_FENCE.search(text)
    if fenced:
        candidates.insert(0, fenced.group(1).strip())
    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        candidates.append(text[first_brace : last_brace + 1])

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(parsed, dict):
            return parsed
        if isinstance(parsed, list):
            return {"items": parsed}
    raise LLMError(f"could not parse JSON from response: {text[:200]!r}")


def _summarise_error(exc: Exception, limit: int = 240) -> str:
    """Condense a provider error into something readable in a report.

    Rate-limit payloads from Gemini and OpenAI run to several hundred characters
    of nested JSON, which is useless in a one-line warning.
    """
    text = " ".join(str(exc).split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _is_rate_limit(exc: Exception) -> bool:
    """Whether *exc* is a quota/rate-limit rejection rather than a real fault.

    These are worth waiting out; a malformed request is not, so retrying it
    three times in a row only wastes quota.
    """
    text = str(exc).lower()
    return any(
        marker in text
        for marker in ("429", "rate limit", "rate_limit", "quota", "resource_exhausted")
    )


class _RetryingClient(LLMClient):
    """Shared throttling and retry-with-backoff for network-backed providers.

    Free tiers are the common case: Gemini's allows five requests per minute
    per model, which a multi-chunk document exceeds almost immediately. Two
    mechanisms cooperate here — a minimum interval between requests, and an
    adaptive increase of that interval whenever a quota error comes back.
    """

    #: Seconds between requests for this provider; overridden per instance.
    default_min_interval: float = 0.0

    def __init__(
        self,
        max_retries: int = 3,
        min_interval: float | None = None,
        backoff: float = 2.0,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        read_timeout: float = DEFAULT_READ_TIMEOUT,
    ) -> None:
        self.max_retries = max_retries
        self.backoff = backoff
        self.connect_timeout = min(max(0.1, connect_timeout), MAX_TOTAL_TIMEOUT)
        self.read_timeout = min(max(0.1, read_timeout), MAX_TOTAL_TIMEOUT)
        if min_interval is None:
            env = os.environ.get("GRAPHRAG_MIN_INTERVAL")
            if env is not None:
                try:
                    min_interval = float(env)
                except ValueError:
                    min_interval = self.default_min_interval
            else:
                min_interval = self.default_min_interval
        self.min_interval = max(0.0, float(min_interval))
        self._last_request = 0.0

    def _throttle(self) -> None:
        if self.min_interval <= 0:
            return
        wait = self._last_request + self.min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def _retry(self, call, what: str) -> str:
        last: Exception | None = None
        for attempt in range(self.max_retries):
            self._throttle()
            try:
                return call()
            except Exception as exc:  # noqa: BLE001 - provider errors vary widely
                last = exc
                if attempt == self.max_retries - 1:
                    break
                if not _is_retryable_error(exc):
                    log.warning(
                        "%s: non-retryable error (attempt %d/%d): %s",
                        self.name,
                        attempt + 1,
                        self.max_retries,
                        _summarise_error(exc),
                    )
                    break
                delay = self.backoff**attempt
                if _is_rate_limit(exc):
                    # Back off harder and slow down for subsequent calls too,
                    # so a burst of chunks does not repeatedly trip the quota.
                    delay = max(delay, self.min_interval or 8.0)
                    self.min_interval = max(self.min_interval, delay)
                    log.warning(
                        "rate limited by %s; slowing to %.1fs between requests",
                        self.name,
                        self.min_interval,
                    )
                time.sleep(delay * (0.5 + random.random()))
        raise LLMError(
            f"{what} failed after {self.max_retries} attempts: "
            f"{_summarise_error(last or RuntimeError('unknown'))}"
        )


class OpenAIChatClient(_RetryingClient):
    """OpenAI chat completions, using the strongest JSON mode available.

    When a schema is supplied the request uses ``json_schema`` with
    ``strict``, so the model is constrained to that shape during decoding.
    Without one it falls back to plain JSON mode.
    """

    name = "openai"
    supports_schema = True

    def __init__(
        self,
        model: str = "gpt-4o-mini",
        temperature: float = 0.0,
        api_key: str | None = None,
        base_url: str | None = None,
        max_retries: int = 3,
        min_interval: float | None = None,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        read_timeout: float = DEFAULT_READ_TIMEOUT,
    ) -> None:
        super().__init__(max_retries, min_interval=min_interval, connect_timeout=connect_timeout, read_timeout=read_timeout)
        # Validate configuration before importing the SDK, so the error names
        # the problem the caller can actually fix.
        api_key = api_key or os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise LLMError(
                "OPENAI_API_KEY is not set; export it or use "
                "--provider anthropic / --provider heuristic"
            )
        try:
            from openai import OpenAI  # type: ignore
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise LLMError(
                "OpenAI provider requires the 'openai' package: pip install openai"
            ) from exc

        kwargs: dict[str, Any] = {"api_key": api_key}
        if base_url or os.environ.get("OPENAI_BASE_URL"):
            kwargs["base_url"] = base_url or os.environ["OPENAI_BASE_URL"]
        # OpenAI SDK accepts timeout as tuple (connect_timeout, read_timeout)
        kwargs["timeout"] = (self.connect_timeout, self.read_timeout)
        self.model = model
        self.temperature = temperature
        self._client = OpenAI(**kwargs)

    def extra_request_args(self) -> dict[str, Any]:
        """Provider-specific request fields, empty for OpenAI-compatible APIs.

        The hook exists for endpoints that accept parameters outside the
        OpenAI schema; see :class:`NvidiaClient`, which needs one. Subclasses
        override it rather than :meth:`_chat`, so the retry, JSON-mode and
        schema logic stays in one place.
        """
        return {}

    def _messages(self, system: str, user: str) -> list[dict[str, str]]:
        """Build the message list. Overridden by providers without a system role."""
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

    def _chat(
        self,
        system: str,
        user: str,
        schema: dict[str, Any] | None = None,
        *,
        json_mode: bool = True,
    ) -> str:
        """Call chat completions, optionally constraining the output shape.

        *json_mode* is separate from *schema* on purpose: a JSON call with no
        schema still needs ``json_object``, while a prose call must send no
        ``response_format`` at all.

        The schema is honoured only when :attr:`supports_schema` is set. A
        provider that advertises an OpenAI-compatible surface can still get
        ``json_schema`` constrained decoding wrong, and the symptom is subtle:
        the model nests the response under the schema's own top-level key and
        truncates it (``{"entities":{"entities": ["Apple"]}``), which then fails
        in :func:`parse_json_object` as unparseable rather than as a provider
        defect. Degrading to plain ``json_object`` keeps the call working, and
        the caller still validates the shape.
        """

        def call() -> str:
            kwargs: dict[str, Any] = {
                "model": self.model,
                "messages": self._messages(system, user),
                "temperature": self.temperature,
            }
            if json_mode:
                if schema and self.supports_schema:
                    kwargs["response_format"] = {
                        "type": "json_schema",
                        "json_schema": {
                            "name": "structured_output",
                            "strict": True,
                            "schema": schema,
                        },
                    }
                else:
                    kwargs["response_format"] = {"type": "json_object"}
            kwargs.update(self.extra_request_args())
            response = self._client.chat.completions.create(**kwargs)
            return response.choices[0].message.content or ""

        return self._retry(call, f"{self.name} request")

    def complete_json(
        self, system: str, user: str, schema: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return parse_json_object(self._chat(system, user, schema))

    def complete_text(self, system: str, user: str) -> str:
        return self._chat(system, user, json_mode=False)


class GeminiClient(OpenAIChatClient):
    """Google Gemini through its OpenAI-compatible endpoint.

    Reusing the OpenAI transport keeps one tested code path for both providers,
    so Gemini only overrides credential resolution and message layout. The key is
    read from ``GEMINI_API_KEY`` or ``GOOGLE_API_KEY`` so it does not collide with
    an OpenAI key in the same environment.
    """

    name = "gemini"
    # The free tier allows five requests per minute per model, which a
    # multi-chunk document exceeds immediately. Set GRAPHRAG_MIN_INTERVAL=0 to
    # disable this on a paid key.
    default_min_interval = 13.0

    def __init__(
        self,
        model: str = "gemini-3.8-flash",
        temperature: float = 0.0,
        api_key: str | None = None,
        max_retries: int = 3,
        min_interval: float | None = None,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        read_timeout: float = DEFAULT_READ_TIMEOUT,
    ) -> None:
        api_key = (
            api_key
            or os.environ.get("GEMINI_API_KEY")
            or os.environ.get("GOOGLE_API_KEY")
        )
        if not api_key:
            raise LLMError(
                "GEMINI_API_KEY (or GOOGLE_API_KEY) is not set; export it or use "
                "--provider openai / --provider heuristic"
            )
        super().__init__(
            model=model,
            temperature=temperature,
            api_key=api_key,
            base_url=GEMINI_BASE_URL,
            max_retries=max_retries,
            min_interval=min_interval,
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
        )

    def _messages(self, system: str, user: str) -> list[dict[str, str]]:
        # The OpenAI-compatible surface has no system role, so the instruction
        # is prepended to the single user turn.
        return [{"role": "user", "content": f"{system}\n\n{user}"}]


class NvidiaClient(OpenAIChatClient):
    """NVIDIA NIM through its OpenAI-compatible chat completions endpoint.

    Added because this repository's only configured credential is
    ``NVIDIA_API_KEY`` -- the one ``sandbox_engine/query_ui.py`` already uses at
    port 9000. Without a provider here, ``resolve_client`` found no recognised
    key, fell through to an unreachable local Ollama, and silently answered
    every question with :class:`HeuristicClient`: a restatement of the retrieved
    graph, with no reasoning and no prose. Reusing the OpenAI transport keeps one
    tested code path, so this only overrides credential resolution.

    The key is read from ``NVIDIA_API_KEY`` and not from ``OPENAI_API_KEY``: NIM
    rejects an OpenAI key with a confusing 401, so sharing the variable would
    make the wrong credential look like a bad one.
    """

    name = "nvidia"
    # NIM advertises an OpenAI-compatible surface but does not implement
    # `json_schema` constrained decoding correctly. Measured against
    # nvidia/nemotron-3-ultra-550b-a55b with the pipeline's own identify prompt:
    # `json_schema` returned the truncated `{"entities":{"entities": ["Apple"]}`
    # on 6 of 6 attempts, while `json_object` returned valid JSON every time.
    # Declining the schema costs shape enforcement and nothing else, since
    # parse_json_object validates the result either way.
    supports_schema = False
    # A hosted endpoint, not a free tier, so there is no per-minute ceiling to
    # respect. NIM returns 503 while a model is loading or overloaded, which
    # _RetryingClient already treats as retryable.
    default_min_interval = 0.0

    def __init__(
        self,
        model: str = "nvidia/nemotron-3-ultra-550b-a55b",
        temperature: float = 0.0,
        api_key: str | None = None,
        max_retries: int = 3,
        min_interval: float | None = None,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        read_timeout: float = DEFAULT_READ_TIMEOUT,
    ) -> None:
        api_key = api_key or os.environ.get("NVIDIA_API_KEY")
        if not api_key:
            raise LLMError(
                "NVIDIA_API_KEY is not set; export it or use "
                "--provider openai / --provider heuristic"
            )
        super().__init__(
            model=model,
            temperature=temperature,
            api_key=api_key,
            base_url=os.environ.get("NVIDIA_BASE_URL") or NVIDIA_BASE_URL,
            max_retries=max_retries,
            min_interval=min_interval,
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
        )

    def _messages(self, system: str, user: str) -> list[dict[str, str]]:
        # NIM's OpenAI-compatible surface accepts a system role, so the base
        # implementation's message layout is correct here and is not overridden.
        return super()._messages(system, user)

    def extra_request_args(self) -> dict[str, Any]:
        """Turn off the reasoning trace for this request.

        Nemotron Ultra is a reasoning model: it emits a ``reasoning_content``
        field alongside ``content``. In JSON mode that budget is mis-spent --
        measured here, the model emitted 83 completion tokens of which the
        answer was a truncated 35 characters, so every response failed to
        parse. With thinking disabled the same prompt returned valid JSON 5
        times out of 5, and prose answers still carry their ``[E1]`` citations.

        ``extra_body`` is the SDK's escape hatch for parameters outside the
        OpenAI schema: it merges the keys into the top level of the request,
        which is what this endpoint expects. Passing them as named arguments is
        rejected, since ``chat_template_kwargs`` is not part of the typed
        surface.
        """
        return {"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}}


class AnthropicClient(_RetryingClient):
    """Anthropic messages with a forced tool to obtain JSON."""

    name = "anthropic"

    def __init__(
        self,
        model: str = "claude-sonnet-4-5",
        temperature: float = 0.0,
        api_key: str | None = None,
        max_retries: int = 3,
        min_interval: float | None = None,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        read_timeout: float = DEFAULT_READ_TIMEOUT,
    ) -> None:
        super().__init__(max_retries, min_interval=min_interval, connect_timeout=connect_timeout, read_timeout=read_timeout)
        api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise LLMError(
                "ANTHROPIC_API_KEY is not set; export it or use "
                "--provider openai / --provider heuristic"
            )
        try:
            from anthropic import Anthropic  # type: ignore
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise LLMError(
                "Anthropic provider requires the 'anthropic' package: "
                "pip install anthropic"
            ) from exc

        # Anthropic SDK accepts timeout as tuple (connect, read)
        self._client = Anthropic(api_key=api_key, timeout=(self.connect_timeout, self.read_timeout))
        self.model = model
        self.temperature = temperature

    def _tool_for(self, schema: dict[str, Any] | None) -> dict[str, Any]:
        """Build a forced tool whose input schema is the requested shape."""
        return {
            "name": "emit_json",
            "description": "Return the structured result as a JSON object.",
            "input_schema": schema
            or {
                "type": "object",
                "properties": {"result": {"type": "object"}},
                "required": ["result"],
            },
        }

    def _chat(
        self, system: str, user: str, schema: dict[str, Any] | None = None
    ) -> str:
        def call() -> str:
            kwargs: dict[str, Any] = {
                "model": self.model,
                "max_tokens": 4096,
                "temperature": self.temperature,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            }
            if schema is not None:
                kwargs["tools"] = [self._tool_for(schema)]
                kwargs["tool_choice"] = {"type": "tool", "name": "emit_json"}
            response = self._client.messages.create(**kwargs)
            if schema is not None:
                for block in response.content:
                    if getattr(block, "type", None) == "tool_use":
                        return json.dumps(block.input)
                raise LLMError("Anthropic returned no tool_use block")
            return "".join(getattr(b, "text", "") for b in response.content)

        return self._retry(call, "Anthropic request")

    def complete_json(
        self, system: str, user: str, schema: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return parse_json_object(self._chat(system, user, schema))

    def complete_text(self, system: str, user: str) -> str:
        return self._chat(system, user, None)


class OllamaClient(_RetryingClient):
    """Ollama local models via HTTP API."""

    name = "ollama"
    default_min_interval = 0.0

    def __init__(
        self,
        model: str = "llama3.2:latest",
        temperature: float = 0.0,
        base_url: str | None = None,
        max_retries: int = 3,
        min_interval: float | None = None,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        read_timeout: float = DEFAULT_READ_TIMEOUT,
    ) -> None:
        super().__init__(max_retries, min_interval=min_interval, connect_timeout=connect_timeout, read_timeout=read_timeout)
        self.model = model
        self.temperature = temperature
        self.base_url = base_url or os.environ.get("OLLAMA_BASE_URL") or "http://localhost:11434"
        try:
            import httpx  # type: ignore
        except ImportError as exc:
            raise LLMError("Ollama provider requires 'httpx': pip install httpx") from exc
        # httpx accepts timeout as tuple (connect, read) or httpx.Timeout
        self._client = httpx.Client(timeout=(self.connect_timeout, self.read_timeout), base_url=self.base_url)

    def _messages(self, system: str, user: str) -> list[dict[str, str]]:
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

    def _chat(
        self,
        system: str,
        user: str,
        schema: dict[str, Any] | None = None,
        *,
        json_mode: bool = True,
    ) -> str:
        def call() -> str:
            payload = {
                "model": self.model,
                "messages": self._messages(system, user),
                "temperature": self.temperature,
                "stream": False,
            }
            if json_mode:
                payload["format"] = "json"
            resp = self._client.post("/api/chat", json=payload)
            resp.raise_for_status()
            data = resp.json()
            return data["message"]["content"] or ""

        return self._retry(call, f"{self.name} request")

    def complete_json(
        self, system: str, user: str, schema: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return parse_json_object(self._chat(system, user, schema, json_mode=True))

    def complete_text(self, system: str, user: str) -> str:
        return self._chat(system, user, json_mode=False)


class HeuristicClient(LLMClient):
    """Offline, deterministic stand-in used when no API key is available.

    It recovers capitalised multi-word names and links them pairwise when a
    relation cue sits between them. This keeps the end-to-end path exercisable
    without credentials; it misses paraphrases and implicit relations, and it
    must not be mistaken for model-quality extraction.
    """

    name = "heuristic"
    is_model = True

    # Word tokens keep internal hyphens/apostrophes so "Mid-Atlantic" and
    # "Bell's" survive as single units; punctuation is separate.
    _TOKEN = re.compile(r"\w+(?:[-'’]\w+)*|[^\w\s]")
    _CAP = re.compile(r"^[A-Z][\w'’-]*$")
    _SENTENCE_END = frozenset({".", "!", "?"})
    # Function words that must not extend a name ("Bay of Fundy" yes,
    # "Degrees Celsius" no).
    _FUNCTION_WORDS = frozenset(
        {
            "and", "or", "but", "the", "a", "an", "of", "in", "on", "at", "to",
            "for", "from", "with", "by", "as", "is", "are", "was", "were", "be",
            "been", "has", "have", "had", "that", "which", "who", "whose", "this",
            "these", "those", "it", "its", "they", "their", "he", "she", "his",
            "her", "not", "no", "than", "then", "so", "if", "when", "while",
            "into", "over", "under", "above", "below", "between", "among",
            "called", "known", "knowns", "such", "also", "both", "each", "any",
        }
    )
    _STOP_HEAD = {
        "the", "a", "an", "this", "that", "these", "those", "it", "they",
        "researchers", "scientists", "bakers", "cooks", "in", "on", "at",
        "because", "however", "although", "while", "when", "if", "there",
        "we", "he", "she", "his", "her", "their", "its", "both", "and",
        "but", "which", "who", "what", "where", "how", "why",
    }
    # Relation cues are generic English verbs, not domain terms. The probe for
    # the subject is a capitalised name, the object is the next capitalised
    # name after the cue.
    _CUES: tuple[tuple[re.Pattern[str], str], ...] = (
        (re.compile(r"\b(?:is|are|was|were)\b", re.I), "is_a"),
        (re.compile(r"\b(?:hosts?|harbou?rs?|shelters?|symbiotises|colonises)\b", re.I), "hosts"),
        (re.compile(r"\b(?:relies?\s+on|relied\s+on|depends?\s+on|depended\s+on|feeds?\s+on|uses?|used|grows?\s+on)\b", re.I), "relies_on"),
        (re.compile(r"\b(?:produces?|producing|generate[sd]?|release[sd]?|emit[sd]?|secrete[sd]?)\b", re.I), "produces"),
        (re.compile(r"\b(?:contains?|containing|includes?|consists?\s+of|made\s+up\s+of)\b", re.I), "contains"),
        (re.compile(r"\b(?:belongs?\s+to|part\s+of|within|inside)\b", re.I), "part_of"),
        (re.compile(r"\b(?:oxidi[sz]es?|reduce[sd]?|synthesi[sz]es?|ferments?|bakes?|baking)\b", re.I), "processes"),
        (re.compile(r"\b(?:combine[sd]?|mix(?:es|ed)?|add(?:s|ed)?|stirs?|ladles?|toasts?|cools?|heats?)\b", re.I), "combined_with"),
        (re.compile(r"\b(?:documents?|documented|study|studies|studied|surveys?|surveyed)\b", re.I), "documented_in"),
    )
    # How far past a cue we will look for the object, and how far past a name
    # we will look for a cue. Both bounded to keep unrelated names apart.
    _CUE_GAP = 45
    _OBJECT_GAP = 45
    # Longest run of lowercase words allowed to extend a capitalised name. One
    # is enough for the binomial form ("Riftia pachyptila"); two starts
    # absorbing verbs ("Riftia pachyptila hosts").
    _MAX_LOWERCASE_RUN = 1

    def complete_json(
        self, system: str, user: str, schema: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        # The schema is ignored: this provider has no decoder to constrain.
        if "relationshi" in system.lower():
            return self._extract(user)
        return self._identify(user)

    def complete_text(self, system: str, user: str) -> str:
        return self._synthesise(user)

    @classmethod
    def _tokens(cls, text: str) -> list[tuple[str, int, int]]:
        return [(m.group(0), m.start(), m.end()) for m in cls._TOKEN.finditer(text)]

    @classmethod
    def _spans(
        cls, text: str, max_lowercase_run: int | None = None
    ) -> list[tuple[str, int, int]]:
        """Return (surface, start, end) for plausible multi-word entity names.

        Scanning is token-wise so a name never absorbs a sentence boundary, and
        a lowercase word may extend a capitalised one. The latter is what lets a
        binomial-style name survive intact; it is also the main source of false
        positives, which is an accepted cost of a lexical fallback.

        *max_lowercase_run* overrides how far a lowercase run may extend a name.
        Question parsing passes 0, because in a question a trailing lowercase
        word is nearly always a verb ("Vesicles relate") rather than part of a
        name.
        """
        cap = cls._MAX_LOWERCASE_RUN if max_lowercase_run is None else max_lowercase_run
        tokens = cls._tokens(text)
        spans: list[tuple[str, int, int]] = []
        index = 0
        total = len(tokens)

        while index < total:
            word, start, _end = tokens[index]
            if not cls._CAP.match(word) or word.lower() in cls._STOP_HEAD:
                index += 1
                continue

            # Extend across capitalised words, connectors, and lowercase
            # continuations, stopping at any sentence boundary. Lowercase
            # continuations are capped: names run to a few words ("Candidatus
            # Endoriftia", "Bathymodiolus thermophilus") and an uncapped run
            # swallows the rest of the sentence.
            last = index
            probe = index + 1
            lower_run = 0
            while probe < total:
                nxt = tokens[probe][0]
                if nxt in cls._SENTENCE_END:
                    break
                lowered = nxt.lower()
                if cls._CAP.match(nxt):
                    last = probe
                    lower_run = 0
                    probe += 1
                    continue
                if lowered in ("of", "de", "van", "der", "von", "the"):
                    # Only a connector if a capitalised word follows.
                    if probe + 1 < total and cls._CAP.match(tokens[probe + 1][0]):
                        last = probe
                        probe += 1
                        continue
                    break
                if (
                    nxt.isalpha()
                    and len(nxt) >= 4
                    and lowered not in cls._FUNCTION_WORDS
                    and lower_run < cap
                ):
                    last = probe
                    lower_run += 1
                    probe += 1
                    continue
                break

            surface = text[tokens[index][1] : tokens[last][2]]
            surface = surface.strip(" .,;:'’-")
            if len(surface) >= 4:
                end = tokens[index][1] + len(surface)
                spans.append((surface, tokens[index][1], end))
                index = last + 1
            else:
                index += 1
        return spans

    def _extract(self, passage: str) -> dict[str, Any]:
        # The prompt is wrapped in delimiters; the passage is the run of prose
        # between "PASSAGE:" and the trailing instruction.
        body = _between(passage, "PASSAGE:", "Respond with the JSON object")

        spans = self._spans(body)
        entities: list[dict[str, str]] = []
        index: dict[str, int] = {}
        for surface, start, end in spans:
            key = surface.lower()
            if key in index:
                continue
            context = body[max(0, start - 90) : min(len(body), end + 90)]
            index[key] = len(entities)
            entities.append(
                {
                    "name": surface,
                    "type": "term",
                    "description": f"Appears in context: ...{context.strip()}...",
                }
            )

        relationships: list[dict[str, str]] = []
        seen_edges: set[tuple[str, str, str]] = set()
        for surface, start, end in spans:
            for pattern, relation in self._CUES:
                cue = pattern.search(body, end, min(len(body), end + self._CUE_GAP))
                if not cue:
                    continue
                target = self._next_span(spans, cue.end(), self._OBJECT_GAP)
                if target is None or target[1] < end:
                    continue
                key = (surface.lower(), target[0].lower(), relation)
                if key in seen_edges:
                    break
                seen_edges.add(key)
                relationships.append(
                    {
                        "source": surface,
                        "target": target[0],
                        "relation": relation,
                        "description": body[
                            start : min(len(body), target[2] + 60)
                        ].strip()[:220],
                    }
                )
                break

        return {"entities": entities, "relationships": relationships}

    @staticmethod
    def _next_span(
        spans: list[tuple[str, int, int]], after: int, gap: int
    ) -> tuple[str, int, int] | None:
        limit = after + gap
        for surface, start, end in spans:
            if start >= after and end <= limit + 60:
                return surface, start, end
            if start > limit + 60:
                break
        return None

    def _identify(self, question: str) -> dict[str, Any]:
        body = _between(question, "QUESTION:", "Respond with the JSON object")
        names: list[str] = []
        seen: set[str] = set()
        for surface, _start, _end in self._spans(body, max_lowercase_run=0):
            key = surface.lower()
            if key not in seen:
                seen.add(key)
                names.append(surface)
        return {"entities": names}

    def _synthesise(self, prompt: str) -> str:
        """Assemble a cited answer straight from the supplied subgraph block.

        This restates retrieved context; it does not reason over it, and says so
        so the output is never mistaken for a model's answer.
        """
        body = _between(prompt, "CONTEXT (retrieved knowledge graph):", "QUESTION:")
        if not body:
            return "The graph contains no entities matching this question."

        entity_part, _, relation_part = body.partition("RELATIONSHIPS:")
        entity_lines = [
            line.strip()
            for line in entity_part.splitlines()
            if line.strip().startswith("[E")
        ]
        relation_lines = [
            line.strip()
            for line in relation_part.splitlines()
            if line.strip().startswith("[E")
        ]

        if not entity_lines and not relation_lines:
            return "The graph contains no entities matching this question."

        out: list[str] = ["From the retrieved graph:", ""]
        if entity_lines:
            out.append("Entities")
            out.extend(f"- {line}" for line in entity_lines)
            out.append("")
        if relation_lines:
            out.append("Relationships")
            out.extend(f"- {line}" for line in relation_lines)
            out.append("")
        out.append(
            "Note: produced by the offline heuristic provider, which restates "
            "graph context rather than reasoning over it. Set one of "
            "NVIDIA_API_KEY, GEMINI_API_KEY, OPENAI_API_KEY, or ANTHROPIC_API_KEY "
            "in .env for model-written answers, or start a local Ollama."
        )
        return "\n".join(out)


def _ollama_reachable(timeout: float = 2.0) -> bool:
    """Whether a local Ollama is answering, using only the stdlib.

    The probe deliberately avoids ``httpx``: that package is an optional
    dependency of the Ollama *provider*, so importing it here made an absent
    package look identical to an absent server, and both cases silently
    degraded to the heuristic provider. ``urllib`` is always present, so this
    answers the question actually being asked.
    """
    base = os.environ.get("OLLAMA_BASE_URL") or "http://localhost:11434"
    try:
        with urllib.request.urlopen(f"{base.rstrip('/')}/api/tags", timeout=timeout):
            return True
    except (urllib.error.URLError, OSError, ValueError):
        return False


def resolve_client(
    provider: str | None = None,
    model: str | None = None,
    temperature: float = 0.0,
    connect_timeout: float | None = None,
    read_timeout: float | None = None,
) -> LLMClient:
    """Pick a provider from an explicit request or the environment.

    Falls back to :class:`HeuristicClient` when no credentials are present, so
    the pipeline is never blocked on configuration.
    """
    provider = (provider or os.environ.get("GRAPHRAG_PROVIDER") or "").lower()
    temperature = float(os.environ.get("GRAPHRAG_TEMPERATURE", temperature))

    # Resolve timeouts from environment if not explicitly provided
    if connect_timeout is None:
        connect_timeout = float(os.environ.get("GRAPHRAG_CONNECT_TIMEOUT", DEFAULT_CONNECT_TIMEOUT))
    if read_timeout is None:
        read_timeout = float(os.environ.get("GRAPHRAG_READ_TIMEOUT", DEFAULT_READ_TIMEOUT))

    if provider == "heuristic":
        return HeuristicClient()

    if provider in ("", "auto"):
        if os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
            provider = "gemini"
        elif os.environ.get("OPENAI_API_KEY"):
            provider = "openai"
        elif os.environ.get("ANTHROPIC_API_KEY"):
            provider = "anthropic"
        elif os.environ.get("NVIDIA_API_KEY"):
            provider = "nvidia"
        else:
            if not _ollama_reachable():
                return HeuristicClient()
            provider = "ollama"

    if provider == "gemini":
        return GeminiClient(
            model=model or os.environ.get("GRAPHRAG_MODEL") or "gemini-3.8-flash",
            temperature=temperature,
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
        )
    if provider == "nvidia":
        return NvidiaClient(
            model=model
            or os.environ.get("GRAPHRAG_MODEL")
            or os.environ.get("NVIDIA_MODEL")
            or "nvidia/nemotron-3-ultra-550b-a55b",
            temperature=temperature,
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
        )
    if provider == "openai":
        return OpenAIChatClient(
            model=model or os.environ.get("GRAPHRAG_MODEL") or "gpt-4o-mini",
            temperature=temperature,
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
        )
    if provider == "anthropic":
        return AnthropicClient(
            model=model or os.environ.get("GRAPHRAG_MODEL") or "claude-sonnet-4-5",
            temperature=temperature,
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
        )
    if provider == "ollama":
        return OllamaClient(
            model=model or os.environ.get("GRAPHRAG_MODEL") or "llama3.2:latest",
            temperature=temperature,
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
        )
    raise LLMError(
        f"Unknown provider {provider!r}; "
        "use gemini, nvidia, openai, anthropic, ollama, or heuristic"
    )


def provider_available() -> str:
    """Report which provider :func:`resolve_client` would choose."""
    client = resolve_client()
    return client.name
