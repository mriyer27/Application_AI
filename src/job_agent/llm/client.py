"""Ollama HTTP client (localhost only; no cloud APIs).

* ``POST {base_url}/api/chat`` with ``"stream": false`` and, for structured
  output, ``"format": <JSON schema>`` (Ollama structured outputs, >= 0.5).
* Bounded retries (``llm.max_retries``) for transient failures (connection
  errors, timeouts, HTTP 408/429/5xx) and exactly ONE corrective re-ask when
  the reply fails Pydantic validation.
* ``LLMUnavailable`` (recoverable) when the server or model cannot be used;
  ``LLMInvalidOutput`` (permanent) when output stays invalid.
* Logs contain metadata only (model, milliseconds, character counts, attempt
  numbers) -- never prompt or response text, which contain personal data.
* The request never includes ``tools``/``functions``: the model cannot act.
* ``trust_env=False``: the client ignores proxy environment variables, so
  requests to the local server can never be routed through a proxy.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from job_agent.config import LLMSection
from job_agent.errors import PermanentError
from job_agent.llm import prompts
from job_agent.llm.errors import LLMInvalidOutput, LLMUnavailable
from job_agent.logging_config import get_logger
from job_agent.security.redaction import redact_text

if TYPE_CHECKING:  # pragma: no cover
    from job_agent.context import AppContext

log = get_logger(__name__)

T = TypeVar("T", bound=BaseModel)

OK, WARN, FAIL, INFO = "ok", "warn", "fail", "info"

#: Ollama added JSON-schema structured outputs in 0.5.0.
MIN_STRUCTURED_OUTPUT_VERSION: tuple[int, int, int] = (0, 5, 0)
#: How long ``get_client`` trusts one availability probe (per process).
PROBE_TTL_SECONDS = 300.0
_TRANSIENT_STATUS = frozenset({408, 429, 500, 502, 503, 504})
_FENCE_RE = re.compile(r"^```[a-zA-Z0-9_-]*\s*\n?(.*?)\n?```$", re.S)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_NS_PER_MS = 1_000_000


# --------------------------------------------------------------------------- #
# Result types
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class LLMHealth:
    """Health of the local LLM. ``health.py`` reads ``status``, ``detail`` and ``action``."""

    status: str  # ok | warn | fail | info
    detail: str
    action: str | None = None
    reachable: bool = False
    version: str | None = None
    models: tuple[str, ...] = ()
    selected_model: str | None = None


@dataclass(frozen=True)
class ChatResult:
    """One successful ``/api/chat`` call. ``content`` is excluded from ``repr`` (personal data)."""

    content: str = field(repr=False)
    model: str
    wall_ms: int
    attempts: int
    load_ms: float | None = None
    prompt_tokens: int | None = None
    prompt_eval_ms: float | None = None
    eval_tokens: int | None = None
    eval_ms: float | None = None
    total_ms: float | None = None  # server-side total_duration

    @property
    def tokens_per_second(self) -> float | None:
        if self.eval_tokens and self.eval_ms:
            return self.eval_tokens / (self.eval_ms / 1000)
        return None

    @property
    def prompt_tokens_per_second(self) -> float | None:
        if self.prompt_tokens and self.prompt_eval_ms:
            return self.prompt_tokens / (self.prompt_eval_ms / 1000)
        return None


class OutputValidationError(Exception):
    """A reply that does not match the schema. ``problems`` never contain reply text."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #
class OllamaClient:
    def __init__(self, llm_config: LLMSection, http_client: httpx.Client | None = None, *,
                 backoff_seconds: float = 1.0, probe_timeout_seconds: float = 5.0,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.config = llm_config
        self.base_url = llm_config.base_url.rstrip("/")
        self._owns_http = http_client is None
        self._http = http_client or httpx.Client(
            timeout=httpx.Timeout(llm_config.timeout_seconds, connect=min(10.0, llm_config.timeout_seconds)),
            trust_env=False,
        )
        self._backoff = backoff_seconds
        self._probe_timeout = min(probe_timeout_seconds, llm_config.timeout_seconds)
        self._sleep = sleep
        self._selected: str | None = None
        #: Metadata of the most recent successful chat call (content hidden from repr).
        self.last_call: ChatResult | None = None

    # -- lifecycle ----------------------------------------------------------- #
    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> "OllamaClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- low-level HTTP ------------------------------------------------------ #
    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _get_json(self, path: str) -> Any:
        try:
            resp = self._http.get(self._url(path), timeout=self._probe_timeout)
        except httpx.TimeoutException:
            raise LLMUnavailable(f"Ollama at {self.base_url} did not answer within {self._probe_timeout:g}s") from None
        except httpx.HTTPError as exc:
            raise LLMUnavailable(f"Ollama not reachable at {self.base_url} ({type(exc).__name__})") from None
        if resp.status_code != 200:
            raise LLMUnavailable(f"Ollama returned HTTP {resp.status_code} for {path}: {_error_text(resp)}")
        try:
            return resp.json()
        except ValueError:
            raise LLMUnavailable(f"Ollama returned a malformed response for {path}") from None

    def version(self) -> str | None:
        data = self._get_json("/api/version")
        value = data.get("version") if isinstance(data, dict) else None
        return str(value) if value else None

    def model_details(self) -> list[dict[str, Any]]:
        """Pulled models as reported by ``/api/tags`` (name, size, details)."""
        data = self._get_json("/api/tags")
        models = data.get("models") if isinstance(data, dict) else None
        if not isinstance(models, list):
            raise LLMUnavailable("Ollama returned a malformed model list")
        return [m for m in models if isinstance(m, dict) and isinstance(m.get("name"), str)]

    def available_models(self) -> list[str]:
        return [m["name"] for m in self.model_details()]

    # -- model selection ----------------------------------------------------- #
    def select_model(self) -> str | None:
        """Configured model if pulled, else the first pulled fallback, else ``None``.

        Raises ``LLMUnavailable`` if the server cannot be reached.
        """
        available = self.available_models()
        for wanted in [self.config.model, *self.config.fallback_models]:
            match = match_model(wanted, available)
            if match:
                return match
        return None

    def resolve_model(self, model: str | None = None) -> str:
        """The model a call will use. Raises ``LLMUnavailable`` if none is pulled."""
        if model:
            return model
        if self._selected is None:
            selected = self.select_model()
            if selected is None:
                raise LLMUnavailable(
                    f"model {self.config.model} is not pulled and no fallback model "
                    f"({', '.join(self.config.fallback_models) or 'none configured'}) is available",
                    human_action=f"Run: ollama pull {self.config.model}",
                )
            self._selected = selected
        return self._selected

    # -- health -------------------------------------------------------------- #
    def health(self) -> LLMHealth:
        cfg = self.config
        if not cfg.enabled:
            return LLMHealth(INFO, "local LLM disabled in config (llm.enabled: false); rules-based fallback only")
        try:
            version = self.version()
            models = tuple(self.available_models())
        except LLMUnavailable as exc:
            return LLMHealth(WARN, f"{exc}; AI features fall back to rules",
                             "Install/start Ollama (`ollama serve`); see docs/local-llm.md")
        vtext = f"Ollama {version}" if version else "Ollama"
        if version and _parse_version(version) < MIN_STRUCTURED_OUTPUT_VERSION:
            return LLMHealth(WARN, f"{vtext} is too old for structured JSON outputs (needs >= 0.5.0)",
                             "Upgrade Ollama: https://ollama.com/download", True, version, models)
        configured = match_model(cfg.model, models)
        if configured:
            return LLMHealth(OK, f"{vtext} running; model {configured} available ({len(models)} pulled)", None,
                             True, version, models, configured)
        fallback = next((m for m in (match_model(f, models) for f in cfg.fallback_models) if m), None)
        if fallback:
            return LLMHealth(WARN, f"{vtext} running; model {cfg.model} not pulled, using fallback {fallback}",
                             f"ollama pull {cfg.model}", True, version, models, fallback)
        return LLMHealth(WARN, f"{vtext} running but model {cfg.model} (and fallbacks "
                         f"{', '.join(cfg.fallback_models) or 'none'}) not pulled; AI features fall back to rules",
                         f"ollama pull {cfg.model}", True, version, models)

    # -- chat ---------------------------------------------------------------- #
    def chat(self, messages: Sequence[dict[str, str]], *, model: str, format: dict[str, Any] | str | None = None,
             temperature: float | None = None, max_tokens: int | None = None) -> ChatResult:
        """One non-streaming chat call with bounded retries for transient errors."""
        options: dict[str, Any] = {
            "temperature": self.config.temperature if temperature is None else temperature,
            "num_ctx": self.config.num_ctx,
        }
        if max_tokens:
            options["num_predict"] = int(max_tokens)
        payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": m["role"], "content": m["content"]} for m in messages],
            "stream": False,
            "options": options,
        }
        if format is not None:
            payload["format"] = format
        prompt_chars = sum(len(m["content"]) for m in messages)
        attempts = self.config.max_retries + 1
        last: LLMUnavailable | None = None
        for attempt in range(1, attempts + 1):
            started = time.monotonic()
            try:
                resp = self._http.post(self._url("/api/chat"), json=payload)
            except httpx.TimeoutException as exc:
                last = LLMUnavailable(f"Ollama request timed out after {self.config.timeout_seconds:g}s "
                                      f"(model {model}); CPU inference may be too slow for this input")
                log.warning("ollama chat timeout model=%s attempt=%d/%d error=%s", model, attempt, attempts,
                            type(exc).__name__)
            except httpx.HTTPError as exc:
                last = LLMUnavailable(f"Ollama not reachable at {self.base_url} ({type(exc).__name__})")
                log.warning("ollama chat connection error model=%s attempt=%d/%d error=%s", model, attempt,
                            attempts, type(exc).__name__)
            else:
                wall_ms = int((time.monotonic() - started) * 1000)
                if resp.status_code == 200:
                    result = self._parse_chat(resp, model, wall_ms, attempt)
                    log.info("ollama chat ok model=%s ms=%d prompt_chars=%d response_chars=%d attempt=%d",
                             result.model, wall_ms, prompt_chars, len(result.content), attempt)
                    self.last_call = result
                    return result
                detail = _error_text(resp)
                if resp.status_code == 404:
                    raise LLMUnavailable(f"model {model!r} is not available in Ollama ({detail})",
                                         human_action=f"Run: ollama pull {model}")
                if resp.status_code not in _TRANSIENT_STATUS:
                    raise PermanentError(f"Ollama rejected the request (HTTP {resp.status_code}): {detail}")
                last = LLMUnavailable(f"Ollama returned HTTP {resp.status_code}: {detail}")
                log.warning("ollama chat http error model=%s status=%d attempt=%d/%d", model, resp.status_code,
                            attempt, attempts)
            if attempt < attempts and self._backoff > 0:
                self._sleep(self._backoff * 2 ** (attempt - 1))
        assert last is not None
        raise last

    def _parse_chat(self, resp: httpx.Response, model: str, wall_ms: int, attempt: int) -> ChatResult:
        try:
            data = resp.json()
        except ValueError:
            raise LLMUnavailable("Ollama returned a malformed (non-JSON) chat response") from None
        message = data.get("message") if isinstance(data, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str):
            raise LLMUnavailable("Ollama chat response has no message content")

        def ms(key: str) -> float | None:
            value = data.get(key)
            return value / _NS_PER_MS if isinstance(value, (int, float)) else None

        def count(key: str) -> int | None:
            value = data.get(key)
            return int(value) if isinstance(value, (int, float)) else None

        return ChatResult(content=content, model=str(data.get("model") or model), wall_ms=wall_ms,
                          attempts=attempt, load_ms=ms("load_duration"), prompt_tokens=count("prompt_eval_count"),
                          prompt_eval_ms=ms("prompt_eval_duration"), eval_tokens=count("eval_count"),
                          eval_ms=ms("eval_duration"), total_ms=ms("total_duration"))

    # -- high-level ---------------------------------------------------------- #
    def generate_json(self, *, system: str, prompt: str, schema: type[T], model: str | None = None,
                      temperature: float | None = None, max_tokens: int | None = None) -> T:
        """Structured output validated against ``schema``; one corrective re-ask on failure."""
        model = self.resolve_model(model)
        fmt = schema.model_json_schema()
        messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
        result = self.chat(messages, model=model, format=fmt, temperature=temperature, max_tokens=max_tokens)
        try:
            return parse_model(result.content, schema)
        except OutputValidationError as first:
            log.info("ollama output failed validation; corrective re-ask model=%s schema=%s problems=%d",
                     model, schema.__name__, len(first.problems))
            retry_messages = [
                *messages,
                {"role": "assistant", "content": result.content[:6000]},
                {"role": "user", "content": prompts.corrective_prompt(first.problems)},
            ]
        retry = self.chat(retry_messages, model=model, format=fmt, temperature=temperature, max_tokens=max_tokens)
        try:
            return parse_model(retry.content, schema)
        except OutputValidationError as second:
            log.warning("ollama output invalid after corrective re-ask model=%s schema=%s problems=%d",
                        model, schema.__name__, len(second.problems))
            raise LLMInvalidOutput(
                f"model {model} returned invalid {schema.__name__} output twice: {'; '.join(second.problems[:5])}"
            ) from None

    def generate_text(self, *, system: str, prompt: str, model: str | None = None,
                      temperature: float | None = None, max_tokens: int | None = None) -> str:
        """Free-text output (control characters stripped); one re-ask if the reply is empty."""
        model = self.resolve_model(model)
        messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
        text = clean_text(self.chat(messages, model=model, temperature=temperature, max_tokens=max_tokens).content)
        if text:
            return text
        retry_messages = [*messages, {"role": "assistant", "content": ""},
                          {"role": "user", "content": "Your previous reply was empty. Reply again as requested."}]
        text = clean_text(self.chat(retry_messages, model=model, temperature=temperature,
                                    max_tokens=max_tokens).content)
        if not text:
            raise LLMInvalidOutput(f"model {model} returned an empty reply twice")
        return text

    def unload(self, model: str) -> None:
        """Ask Ollama to unload ``model`` from memory (used by cold-start benchmarks)."""
        try:
            resp = self._http.post(self._url("/api/generate"), json={"model": model, "keep_alive": 0},
                                   timeout=max(self._probe_timeout, 30.0))
        except httpx.HTTPError as exc:
            raise LLMUnavailable(f"Ollama not reachable at {self.base_url} ({type(exc).__name__})") from None
        if resp.status_code == 404:
            raise LLMUnavailable(f"model {model!r} is not available in Ollama", human_action=f"Run: ollama pull {model}")
        if resp.status_code != 200:
            raise LLMUnavailable(f"Ollama returned HTTP {resp.status_code} while unloading: {_error_text(resp)}")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def match_model(wanted: str, available: Sequence[str]) -> str | None:
    """Return the pulled model name matching ``wanted`` (``name`` == ``name:latest``)."""
    if not wanted:
        return None
    if wanted in available:
        return wanted
    if ":" not in wanted and f"{wanted}:latest" in available:
        return f"{wanted}:latest"
    if wanted.endswith(":latest") and wanted.removesuffix(":latest") in available:
        return wanted.removesuffix(":latest")
    return None


def _parse_version(text: str) -> tuple[int, int, int]:
    nums = [int(n) for n in re.findall(r"\d+", text)[:3]]
    while len(nums) < 3:
        nums.append(0)
    return nums[0], nums[1], nums[2]


def _error_text(resp: httpx.Response) -> str:
    try:
        data = resp.json()
        text = str(data.get("error") or data) if isinstance(data, dict) else str(data)
    except ValueError:
        text = resp.text
    return redact_text(" ".join(text.split())[:200], pii=True) or "no details"


def clean_text(text: str) -> str:
    return _CONTROL_RE.sub("", text or "").strip()


def parse_model(content: str, schema: type[T]) -> T:
    """Parse a model reply into ``schema``; raises ``OutputValidationError`` (content-free problems)."""
    text = (content or "").strip()
    fenced = _FENCE_RE.match(text)
    if fenced:
        text = fenced.group(1).strip()
    if not text:
        raise OutputValidationError(["the reply was empty"])
    try:
        return schema.model_validate_json(text)
    except ValidationError as exc:
        problems = []
        for err in exc.errors(include_input=False, include_url=False)[:10]:
            loc = ".".join(str(x) for x in err.get("loc", ())) or "reply"
            problems.append(f"{loc}: {err.get('msg', 'invalid')}")
        raise OutputValidationError(problems or ["the reply did not match the schema"]) from None


# --------------------------------------------------------------------------- #
# Process-wide availability probe
# --------------------------------------------------------------------------- #
_probe_lock = threading.Lock()
_probe_cache: dict[tuple[str, str, tuple[str, ...]], tuple[float, str | None]] = {}


def _probe_key(cfg: LLMSection) -> tuple[str, str, tuple[str, ...]]:
    return cfg.base_url, cfg.model, tuple(cfg.fallback_models)


def reset_probe_cache() -> None:
    with _probe_lock:
        _probe_cache.clear()


def mark_unavailable(cfg: LLMSection) -> None:
    """Remember (for ``PROBE_TTL_SECONDS``) that the LLM failed, so callers skip it quickly."""
    with _probe_lock:
        _probe_cache[_probe_key(cfg)] = (time.monotonic(), None)


def get_client(ctx: "AppContext", *, http_client: httpx.Client | None = None) -> OllamaClient | None:
    """A ready client, or ``None`` if the LLM is disabled or unavailable.

    The availability probe (``/api/tags`` + model selection) is cached per
    process for ``PROBE_TTL_SECONDS`` so a scoring run probes at most once.
    """
    cfg = ctx.config.llm
    if not cfg.enabled:
        return None
    key = _probe_key(cfg)
    now = time.monotonic()
    with _probe_lock:
        cached = _probe_cache.get(key)
    if cached is not None and now - cached[0] < PROBE_TTL_SECONDS:
        if cached[1] is None:
            return None
        client = OllamaClient(cfg, http_client)
        client._selected = cached[1]
        return client
    client = OllamaClient(cfg, http_client)
    try:
        selected = client.select_model()
    except LLMUnavailable as exc:
        log.info("local LLM unavailable: %s", exc)
        selected = None
    with _probe_lock:
        _probe_cache[key] = (now, selected)
    if selected is None:
        log.info("local LLM: no usable model (configured %s)", cfg.model)
        client.close()
        return None
    client._selected = selected
    return client
