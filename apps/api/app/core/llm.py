"""LLM router — central gateway for every model call in Alloy.

Per roadmap §5 / §8, we funnel all model calls through LiteLLM so we can:

* Route by region (Azure East US 2 + Sweden Central are load-balanced
  under the same primary `model_name`; LiteLLM picks the less-busy one
  and auto-retries on 429).
* Cascade on 429 / 5xx / timeout (primary Azure pair → OpenAI direct →
  Anthropic Claude Sonnet) via LiteLLM's `fallbacks=` config.
* Track cost + latency + error class per underlying model. `RouterStats`
  is the in-memory precursor to the Langfuse trace shipper we wire in
  Phase 4; it's enough to verify behaviour in tests and to expose on a
  `/admin/llm-stats` probe today.
* Stabilize cache keys across calls (`prompt_cache_key`) so the Azure
  prompt-cache hit rate actually materialises.

Surface:

    ModelGroup          Named pools (`planner`, `coder`) with their own
                        fallback cascade. Add groups as needs emerge
                        (`apply` reserved for Morph wiring in Phase 2).
    ModelCallOptions    Typed bundle of reasoning_effort / verbosity /
                        max_tokens / cache-key / user / temperature.
    RouterStats         Per-underlying-model success + failure counters.
    LLMRouterError      Raised when no providers are configured.
    get_llm_router()    Lazy `litellm.Router` singleton. `None` when all
                        provider keys are absent so callers can branch.
    acompletion(...)    Single async entry-point; handles stats + stream
                        wrapping. Returns the raw LiteLLM response (or an
                        async iterator when streaming).
    get_openai_client() Raw Azure SDK client for Responses API features
                        (`reasoning_effort` + `prompt_cache_key` on the
                        Responses endpoint) that LiteLLM still doesn't
                        round-trip cleanly on Azure.

Notes:

* Agent work goes through `pydantic_ai` and picks up its own Azure provider
  in `app/agents/models.py`. The router here serves the *non-agent* call
  sites: Coder Agent `run_command` retries, future apply-model proxying,
  background jobs, and ad-hoc admin tooling.
* LiteLLM is imported lazily inside `get_llm_router()` — top-level
  `import litellm` pulls in ~600 ms of network + SDK init we don't need
  on API replicas that never route a call (pure metadata endpoints).
* The @lru_cache on `get_llm_router()` makes this process-global. Tests
  call `.cache_clear()` before patching `settings.*` to rebuild.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from threading import Lock
from typing import Any, Literal

import structlog
from openai import AsyncAzureOpenAI

from app.core.config import settings

_log = structlog.get_logger(__name__)


# ── Types ────────────────────────────────────────────────────────────────


class ModelGroup(StrEnum):
    """Named pools exposed to callers.

    Each group has its own ordered fallback cascade. Callers reference
    groups by enum, not raw strings, so a typo can't silently route to
    a non-existent pool.
    """

    PLANNER = "planner"  # spec + planner agents; reasoning_effort=low, short outputs
    CODER = "coder"  # coder agent; reasoning_effort=medium, longer outputs
    # APPLY = "apply"      # reserved for Phase 2 Morph integration


ReasoningEffort = Literal["minimal", "low", "medium", "high"]
Verbosity = Literal["low", "medium", "high"]


@dataclass(slots=True)
class ModelCallOptions:
    """Per-call knobs with sensible defaults drawn from roadmap §5.

    `reasoning_effort=low` + `verbosity=low` are the right defaults for
    gpt-5-family — upgrade only on quality regressions. Non-reasoning
    fallback models (Claude) silently ignore these fields.

    `prompt_cache_key` stabilises the Azure prompt-cache bucket across
    turns; set it to something like `f"tenant:{tenant}:group:{group}:v1"`
    so two logically-equivalent calls land in the same bucket.

    `user` becomes the `user=` field LiteLLM forwards to Azure / OpenAI
    for abuse tracking — pass the Clerk sub (or tenant id) here.
    """

    reasoning_effort: ReasoningEffort = "low"
    verbosity: Verbosity = "low"
    max_tokens: int = 4000
    prompt_cache_key: str | None = None
    user: str | None = None
    # gpt-5 ignores `temperature`; kept for the Claude fallback leg where
    # deterministic output matters (defaults None → provider default).
    temperature: float | None = None

    def as_call_kwargs(self) -> dict[str, Any]:
        """Flatten into kwargs for `router.acompletion()`.

        Returns only fields that are set so we don't override provider
        defaults with `None`s (LiteLLM forwards every kwarg verbatim).

        `verbosity` is deliberately *not* emitted here. The roadmap §5
        lists it as a Responses-API field Chat Completions silently
        ignores; Azure actually returns `400 Unknown parameter: 'text'`
        when we send `extra_body={"text": {"verbosity": ...}}`. Until we
        wire a Responses-API path through the router (LiteLLM's Azure
        support still routes through Chat Completions), `verbosity`
        rides along on the `ModelCallOptions` struct as metadata only.
        """
        kwargs: dict[str, Any] = {"max_tokens": self.max_tokens}
        if self.reasoning_effort:
            kwargs["reasoning_effort"] = self.reasoning_effort
        if self.temperature is not None:
            kwargs["temperature"] = self.temperature
        if self.user:
            kwargs["user"] = self.user
        extra_body: dict[str, Any] = {}
        if self.prompt_cache_key:
            extra_body["prompt_cache_key"] = self.prompt_cache_key
        if extra_body:
            kwargs["extra_body"] = extra_body
        return kwargs


# ── Router construction ──────────────────────────────────────────────────


def _azure_entry(model_name: str, endpoint: str, api_key: str) -> dict[str, Any]:
    """Single Azure deployment entry in the router's model_list."""
    return {
        "model_name": model_name,
        "litellm_params": {
            # LiteLLM parses `azure/<deployment>` to pick the Azure client.
            "model": f"azure/{settings.AZURE_OPENAI_DEPLOYMENT}",
            "api_base": endpoint,
            "api_key": api_key,
            "api_version": settings.AZURE_OPENAI_API_VERSION,
        },
    }


def _openai_entry(model_name: str) -> dict[str, Any]:
    return {
        "model_name": model_name,
        "litellm_params": {
            "model": "gpt-5-mini",
            "api_key": settings.OPENAI_API_KEY,
        },
    }


def _anthropic_entry(model_name: str) -> dict[str, Any]:
    return {
        "model_name": model_name,
        "litellm_params": {
            # LiteLLM's Anthropic integration uses its canonical model ids.
            "model": "anthropic/claude-sonnet-4-5",
            "api_key": settings.ANTHROPIC_API_KEY,
        },
    }


def _openai_fallback_name(group: ModelGroup) -> str:
    return f"{group.value}-openai-direct"


def _claude_fallback_name(group: ModelGroup) -> str:
    return f"{group.value}-claude"


def build_router_model_list() -> list[dict[str, Any]]:
    """Assemble LiteLLM's `model_list` across every group.

    Topology per group:

    * `<group>`                   — Azure primary region (always first
                                    entry when configured). If a fallback
                                    region is also configured, its entry
                                    shares the same `model_name` so
                                    LiteLLM load-balances between them.
    * `<group>-openai-direct`     — OpenAI direct; used only via fallback.
    * `<group>-claude`            — Anthropic Claude Sonnet; last resort.

    Entries with missing creds are skipped so tests (which don't set any
    keys) see an empty list rather than a KeyError at Router init.
    """
    models: list[dict[str, Any]] = []
    for group in ModelGroup:
        primary = group.value
        if settings.AZURE_OPENAI_ENDPOINT and settings.AZURE_OPENAI_API_KEY:
            models.append(
                _azure_entry(
                    primary,
                    settings.AZURE_OPENAI_ENDPOINT,
                    settings.AZURE_OPENAI_API_KEY,
                )
            )
        if settings.AZURE_OPENAI_ENDPOINT_FALLBACK and settings.AZURE_OPENAI_API_KEY_FALLBACK:
            # Same model_name → LiteLLM treats as peer of Azure primary
            # for routing-strategy (usage-based / least-busy), so the two
            # regions share load and a 429 on one shifts to the other
            # without tripping cross-provider fallback.
            models.append(
                _azure_entry(
                    primary,
                    settings.AZURE_OPENAI_ENDPOINT_FALLBACK,
                    settings.AZURE_OPENAI_API_KEY_FALLBACK,
                )
            )
        if settings.OPENAI_API_KEY:
            models.append(_openai_entry(_openai_fallback_name(group)))
        if settings.ANTHROPIC_API_KEY:
            models.append(_anthropic_entry(_claude_fallback_name(group)))
    return models


def build_fallbacks() -> list[dict[str, list[str]]]:
    """Cross-provider fallback cascade per group.

    Triggered on 429 / 5xx / timeout (LiteLLM's `RetryPolicy` decides).
    Primary Azure pair shares `model_name=<group>` so retries within
    that pool happen transparently (region failover); only when *both*
    regions are exhausted does LiteLLM pop up to the named fallbacks
    below.
    """
    fallbacks: list[dict[str, list[str]]] = []
    for group in ModelGroup:
        chain: list[str] = []
        if settings.OPENAI_API_KEY:
            chain.append(_openai_fallback_name(group))
        if settings.ANTHROPIC_API_KEY:
            chain.append(_claude_fallback_name(group))
        if chain:
            fallbacks.append({group.value: chain})
    return fallbacks


@lru_cache(maxsize=1)
def get_llm_router() -> Any | None:
    """Lazy-constructed `litellm.Router` singleton.

    Returns `None` when no providers are configured so callers branch
    on identity instead of catching exceptions.

    Routing strategy `usage-based-routing-v2` picks the least-loaded
    entry within a `model_name` — exactly what we want for the two
    Azure regions. `allowed_fails=3` + `cooldown_time=30` take a flaky
    deployment out of rotation for 30s after three strikes.
    """
    models = build_router_model_list()
    if not models:
        _log.warning(
            "llm_router.no_providers",
            detail="no API keys configured; router disabled",
        )
        return None

    # Import here to avoid LiteLLM's module-level init tax on API replicas
    # that never actually route a call.
    from litellm import Router

    router = Router(
        model_list=models,
        fallbacks=build_fallbacks(),
        routing_strategy="usage-based-routing-v2",
        num_retries=2,
        retry_after=1,
        allowed_fails=3,
        cooldown_time=30,
        set_verbose=False,
    )
    _log.info(
        "llm_router.ready",
        groups=[g.value for g in ModelGroup],
        providers=sorted({m["model_name"] for m in models}),
    )
    return router


# ── Stats ────────────────────────────────────────────────────────────────


@dataclass
class _ModelStats:
    """Per-underlying-model counters. Private — consumers see snapshots."""

    calls: int = 0
    errors: int = 0
    latency_sum_s: float = 0.0
    cost_sum_usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    last_error: str | None = None


class RouterStats:
    """Thread-safe in-memory counters per underlying model.

    Phase 4 replaces this with a Langfuse span; today it's the smallest
    thing that lets tests assert fallbacks actually fired. Counters reset
    on process restart — treat as observability-grade, not billing-grade.
    """

    def __init__(self) -> None:
        self._by_model: dict[str, _ModelStats] = {}
        self._lock = Lock()

    def record_success(
        self,
        *,
        model: str,
        latency_s: float,
        cost_usd: float,
        tokens_in: int,
        tokens_out: int,
    ) -> None:
        with self._lock:
            s = self._by_model.setdefault(model, _ModelStats())
            s.calls += 1
            s.latency_sum_s += latency_s
            s.cost_sum_usd += cost_usd
            s.tokens_in += tokens_in
            s.tokens_out += tokens_out

    def record_failure(self, *, model: str, error: str) -> None:
        with self._lock:
            s = self._by_model.setdefault(model, _ModelStats())
            s.errors += 1
            # Cap error string length so a full traceback doesn't blow up
            # the snapshot dict.
            s.last_error = error[:200]

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """Return a deep copy of the counters as plain dicts."""
        with self._lock:
            return {
                m: {
                    "calls": s.calls,
                    "errors": s.errors,
                    "avg_latency_s": (round(s.latency_sum_s / s.calls, 3) if s.calls else None),
                    # 6 decimals = micro-dollar precision; per-call costs on
                    # cheap models (gpt-5-mini spec extraction ≈ $0.0003) lose
                    # real signal at 4 decimals when aggregating over time.
                    "cost_usd": round(s.cost_sum_usd, 6),
                    "tokens_in": s.tokens_in,
                    "tokens_out": s.tokens_out,
                    "last_error": s.last_error,
                }
                for m, s in self._by_model.items()
            }

    def reset(self) -> None:
        """Drop all counters. Tests should call this in fixtures."""
        with self._lock:
            self._by_model.clear()


_stats = RouterStats()


def get_router_stats() -> RouterStats:
    """Return the process-global stats singleton."""
    return _stats


# ── Call helpers ─────────────────────────────────────────────────────────


class LLMRouterError(RuntimeError):
    """Raised when the router is unavailable or every fallback has failed."""


async def acompletion(
    group: ModelGroup,
    messages: list[dict[str, Any]],
    *,
    options: ModelCallOptions | None = None,
    stream: bool = False,
    **extra: Any,
) -> Any:
    """Call the router for `group` and record stats on success + failure.

    Behavior:
    * Resolves `group` → LiteLLM `model_name` (string the Router indexes by).
    * Flattens `ModelCallOptions` into call kwargs, respecting provider
      defaults (fields left as `None` are dropped).
    * Records {latency, cost, tokens} on success; {error class + message}
      on failure.
    * On `stream=True`, returns an async iterator that records stats
      when the caller drains it (we can't know cost until the last chunk).

    Raises `LLMRouterError` when the router is disabled — this is a
    config issue the caller should surface as a 503, not a retriable
    transient. Underlying LiteLLM errors (after all fallbacks exhausted)
    bubble up unchanged.
    """
    router = get_llm_router()
    if router is None:
        raise LLMRouterError(
            "No LLM providers are configured. Set AZURE_OPENAI_* (and "
            "optional OPENAI_API_KEY / ANTHROPIC_API_KEY) in .env."
        )

    opts = options or ModelCallOptions()
    call_kwargs: dict[str, Any] = {
        "model": group.value,
        "messages": messages,
        "stream": stream,
        **opts.as_call_kwargs(),
        **extra,
    }

    start = time.perf_counter()
    try:
        response = await router.acompletion(**call_kwargs)
    except Exception as exc:
        _stats.record_failure(model=group.value, error=f"{type(exc).__name__}: {exc}")
        _log.warning(
            "llm_router.call_failed",
            group=group.value,
            error_class=type(exc).__name__,
        )
        raise

    if stream:
        # Defer stats to drain time — async generator below.
        return _wrap_stream_for_stats(response, group=group, started_at=start)

    _record_response_stats(response, group=group, started_at=start)
    return response


async def _wrap_stream_for_stats(
    stream: AsyncIterator[Any],
    *,
    group: ModelGroup,
    started_at: float,
) -> AsyncIterator[Any]:
    """Pass-through async iterator that records stats on the final chunk.

    LiteLLM stamps cost + model on the terminating chunk (after
    `stream_options={"include_usage": True}` — which callers must opt
    into via `extra`).
    """
    last_chunk: Any = None
    async for chunk in stream:
        last_chunk = chunk
        yield chunk
    if last_chunk is not None:
        _record_response_stats(last_chunk, group=group, started_at=started_at)


def _record_response_stats(response: Any, *, group: ModelGroup, started_at: float) -> None:
    """Pull {model, cost, tokens} off a LiteLLM response and record.

    Resilient to malformed responses (tests use plain dataclasses): every
    attribute access is defensive.
    """
    latency = time.perf_counter() - started_at
    used_model = getattr(response, "model", None) or group.value
    usage = getattr(response, "usage", None)
    tokens_in = getattr(usage, "prompt_tokens", 0) or 0
    tokens_out = getattr(usage, "completion_tokens", 0) or 0
    cost = _response_cost(response)
    _stats.record_success(
        model=used_model,
        latency_s=latency,
        cost_usd=cost,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
    )


def _response_cost(response: Any) -> float:
    """Best-effort cost extraction from LiteLLM's hidden-params.

    LiteLLM stamps `_hidden_params["response_cost"]` via its cost-calc
    hook. Falls back to 0.0 when absent (tests, streaming mid-chunk).
    """
    hidden = getattr(response, "_hidden_params", None)
    if isinstance(hidden, dict):
        cost = hidden.get("response_cost")
        if isinstance(cost, int | float):
            return float(cost)
    return 0.0


# ── Raw Azure client (for Responses API features LiteLLM omits) ──────────


@lru_cache(maxsize=1)
def get_openai_client() -> AsyncAzureOpenAI:
    """Direct Azure SDK client.

    Kept alongside the router because the Responses API path
    (`reasoning_effort`, `prompt_cache_key` with 24h retention, chain-
    of-thought carryover) is still rough on LiteLLM + Azure in early
    2026 — calls involving those features use this client instead.
    """
    if not settings.AZURE_OPENAI_ENDPOINT or not settings.AZURE_OPENAI_API_KEY:
        raise LLMRouterError(
            "Azure OpenAI is not configured. Set AZURE_OPENAI_ENDPOINT and "
            "AZURE_OPENAI_API_KEY in .env."
        )
    return AsyncAzureOpenAI(
        azure_endpoint=settings.AZURE_OPENAI_ENDPOINT,
        api_key=settings.AZURE_OPENAI_API_KEY,
        api_version=settings.AZURE_OPENAI_API_VERSION,
    )
