"""Unit tests for `app.core.llm` — router topology, options flattening, stats.

The LiteLLM `Router` itself is not constructed in these tests — we don't
want a network-touching dependency in the test path. Instead we:

* assert `build_router_model_list()` + `build_fallbacks()` produce the
  right shape for every combination of configured providers
* assert `ModelCallOptions.as_call_kwargs()` emits the fields we'd
  actually send over the wire
* assert `acompletion()` records stats correctly by patching
  `get_llm_router()` to return a lightweight fake router

Fixtures monkeypatch `app.core.config.settings` and clear the
`@lru_cache` on `get_llm_router` between scenarios so state doesn't bleed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from app.core import llm as llm_mod
from app.core.llm import (
    LLMRouterError,
    ModelCallOptions,
    ModelGroup,
    RouterStats,
    acompletion,
    build_fallbacks,
    build_router_model_list,
    get_llm_router,
    get_router_stats,
)


@pytest.fixture(autouse=True)
def _reset_router_cache() -> None:
    """Clear the LRU cache so each test rebuilds with its own env."""
    get_llm_router.cache_clear()
    yield
    get_llm_router.cache_clear()


@pytest.fixture(autouse=True)
def _reset_stats() -> None:
    """Wipe per-model stats between tests."""
    get_router_stats().reset()
    yield
    get_router_stats().reset()


def _patch_settings(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> None:
    """Set attributes on the cached settings object so module-level reads
    pick them up without rebuilding the whole Settings instance."""
    for key, value in overrides.items():
        monkeypatch.setattr(llm_mod.settings, key, value)


# ── Model list construction ──────────────────────────────────────────────


def test_build_router_model_list_empty_when_no_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_settings(
        monkeypatch,
        AZURE_OPENAI_ENDPOINT=None,
        AZURE_OPENAI_API_KEY=None,
        AZURE_OPENAI_ENDPOINT_FALLBACK=None,
        AZURE_OPENAI_API_KEY_FALLBACK=None,
        OPENAI_API_KEY=None,
        ANTHROPIC_API_KEY=None,
    )
    assert build_router_model_list() == []
    assert build_fallbacks() == []
    assert get_llm_router() is None


def test_build_router_model_list_azure_primary_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_settings(
        monkeypatch,
        AZURE_OPENAI_ENDPOINT="https://primary.openai.azure.com",
        AZURE_OPENAI_API_KEY="azure-primary-key",
        AZURE_OPENAI_ENDPOINT_FALLBACK=None,
        AZURE_OPENAI_API_KEY_FALLBACK=None,
        OPENAI_API_KEY=None,
        ANTHROPIC_API_KEY=None,
    )
    models = build_router_model_list()
    # One entry per group (planner, coder) = 2.
    assert len(models) == len(list(ModelGroup))
    assert {m["model_name"] for m in models} == {g.value for g in ModelGroup}
    for m in models:
        p = m["litellm_params"]
        assert p["api_base"] == "https://primary.openai.azure.com"
        assert p["api_key"] == "azure-primary-key"
        assert p["model"].startswith("azure/")


def test_build_router_model_list_azure_pair_shares_model_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both Azure regions must use the *same* model_name so LiteLLM
    load-balances between them instead of treating the fallback region
    as a cross-provider fallback."""
    _patch_settings(
        monkeypatch,
        AZURE_OPENAI_ENDPOINT="https://a.openai.azure.com",
        AZURE_OPENAI_API_KEY="key-a",
        AZURE_OPENAI_ENDPOINT_FALLBACK="https://b.openai.azure.com",
        AZURE_OPENAI_API_KEY_FALLBACK="key-b",
        OPENAI_API_KEY=None,
        ANTHROPIC_API_KEY=None,
    )
    models = build_router_model_list()
    # 2 Azure entries per group × 2 groups = 4 total.
    assert len(models) == 2 * len(list(ModelGroup))
    planner_entries = [m for m in models if m["model_name"] == ModelGroup.PLANNER.value]
    assert len(planner_entries) == 2
    endpoints = {m["litellm_params"]["api_base"] for m in planner_entries}
    assert endpoints == {"https://a.openai.azure.com", "https://b.openai.azure.com"}


def test_build_router_model_list_full_cascade(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_settings(
        monkeypatch,
        AZURE_OPENAI_ENDPOINT="https://a.openai.azure.com",
        AZURE_OPENAI_API_KEY="key-a",
        AZURE_OPENAI_ENDPOINT_FALLBACK="https://b.openai.azure.com",
        AZURE_OPENAI_API_KEY_FALLBACK="key-b",
        OPENAI_API_KEY="sk-openai",
        ANTHROPIC_API_KEY="sk-ant",
    )
    models = build_router_model_list()
    names = [m["model_name"] for m in models]
    # Per group: 2x Azure (same name), 1x openai-direct, 1x claude.
    for group in ModelGroup:
        assert names.count(group.value) == 2
        assert f"{group.value}-openai-direct" in names
        assert f"{group.value}-claude" in names


def test_build_fallbacks_orders_openai_before_claude(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_settings(
        monkeypatch,
        AZURE_OPENAI_ENDPOINT="https://a.openai.azure.com",
        AZURE_OPENAI_API_KEY="key-a",
        OPENAI_API_KEY="sk-openai",
        ANTHROPIC_API_KEY="sk-ant",
    )
    fallbacks = build_fallbacks()
    planner_chain = next(iter(d["planner"] for d in fallbacks if "planner" in d))
    # Order matters: OpenAI direct is cheaper + faster than Claude.
    assert planner_chain == ["planner-openai-direct", "planner-claude"]


def test_build_fallbacks_empty_when_only_azure(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_settings(
        monkeypatch,
        AZURE_OPENAI_ENDPOINT="https://a.openai.azure.com",
        AZURE_OPENAI_API_KEY="key-a",
        AZURE_OPENAI_ENDPOINT_FALLBACK=None,
        AZURE_OPENAI_API_KEY_FALLBACK=None,
        OPENAI_API_KEY=None,
        ANTHROPIC_API_KEY=None,
    )
    # No named fallback models → `build_fallbacks` returns [] so LiteLLM
    # doesn't try to route to non-existent names.
    assert build_fallbacks() == []


# ── ModelCallOptions flattening ──────────────────────────────────────────


def test_model_call_options_defaults_flatten_cleanly() -> None:
    opts = ModelCallOptions()
    kwargs = opts.as_call_kwargs()
    assert kwargs["max_tokens"] == 4000
    assert kwargs["reasoning_effort"] == "low"
    # Verbosity is intentionally NOT forwarded — `text.verbosity` is a
    # Responses-API-only field and Azure Chat Completions rejects it with
    # `400 Unknown parameter: 'text'`. With no prompt_cache_key and no
    # verbosity, there's nothing to put in extra_body, so it must be absent.
    assert "extra_body" not in kwargs
    # Absent fields (temperature, user, cache key) should NOT appear —
    # leaving them out preserves provider defaults.
    assert "temperature" not in kwargs
    assert "user" not in kwargs


def test_model_call_options_full_payload() -> None:
    opts = ModelCallOptions(
        reasoning_effort="medium",
        verbosity="high",
        max_tokens=8000,
        prompt_cache_key="tenant:t1:coder:v1",
        user="user_abc",
        temperature=0.2,
    )
    kwargs = opts.as_call_kwargs()
    assert kwargs["reasoning_effort"] == "medium"
    assert kwargs["max_tokens"] == 8000
    assert kwargs["temperature"] == 0.2
    assert kwargs["user"] == "user_abc"
    # Only `prompt_cache_key` lands in extra_body; verbosity is dropped
    # on the Chat Completions path (see docstring on `as_call_kwargs`).
    assert kwargs["extra_body"] == {"prompt_cache_key": "tenant:t1:coder:v1"}


# ── acompletion + stats ──────────────────────────────────────────────────


@dataclass
class _FakeUsage:
    prompt_tokens: int
    completion_tokens: int


@dataclass
class _FakeResponse:
    model: str
    usage: _FakeUsage
    _hidden_params: dict[str, Any]


class _FakeRouter:
    """Minimal double of `litellm.Router` exposing `acompletion`."""

    def __init__(self, response: Any | None = None, raise_exc: Exception | None = None) -> None:
        self._response = response
        self._raise = raise_exc
        self.calls: list[dict[str, Any]] = []

    async def acompletion(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self._raise:
            raise self._raise
        return self._response


async def test_acompletion_raises_router_error_when_no_providers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_settings(
        monkeypatch,
        AZURE_OPENAI_ENDPOINT=None,
        AZURE_OPENAI_API_KEY=None,
        OPENAI_API_KEY=None,
        ANTHROPIC_API_KEY=None,
    )
    with pytest.raises(LLMRouterError):
        await acompletion(ModelGroup.PLANNER, [{"role": "user", "content": "hi"}])


async def test_acompletion_records_success_stats(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeRouter(
        response=_FakeResponse(
            model="azure/gpt-5-mini",
            usage=_FakeUsage(prompt_tokens=120, completion_tokens=40),
            _hidden_params={"response_cost": 0.00042},
        ),
    )
    monkeypatch.setattr(llm_mod, "get_llm_router", lambda: fake)

    await acompletion(
        ModelGroup.CODER,
        [{"role": "user", "content": "hello"}],
        options=ModelCallOptions(reasoning_effort="medium", max_tokens=1234),
    )

    snap = get_router_stats().snapshot()
    # Stats indexed by the underlying model the response came back with.
    assert "azure/gpt-5-mini" in snap
    bucket = snap["azure/gpt-5-mini"]
    assert bucket["calls"] == 1
    assert bucket["errors"] == 0
    assert bucket["tokens_in"] == 120
    assert bucket["tokens_out"] == 40
    assert bucket["cost_usd"] == pytest.approx(0.00042, rel=1e-3)
    assert bucket["avg_latency_s"] is not None and bucket["avg_latency_s"] >= 0.0

    # Kwargs actually forwarded — sanity check the mapping from options.
    call = fake.calls[0]
    assert call["model"] == ModelGroup.CODER.value
    assert call["max_tokens"] == 1234
    assert call["reasoning_effort"] == "medium"


async def test_acompletion_records_failure_stats_and_reraises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Boom(Exception):
        pass

    fake = _FakeRouter(raise_exc=_Boom("429 from Azure"))
    monkeypatch.setattr(llm_mod, "get_llm_router", lambda: fake)

    with pytest.raises(_Boom):
        await acompletion(ModelGroup.PLANNER, [{"role": "user", "content": "x"}])

    snap = get_router_stats().snapshot()
    # Failure gets keyed by the group (we don't know which underlying model
    # the router tried last — LiteLLM raises before we see the response).
    bucket = snap["planner"]
    assert bucket["errors"] == 1
    assert bucket["calls"] == 0
    assert "_Boom" in (bucket["last_error"] or "")


async def test_acompletion_stream_records_stats_after_drain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stats for streamed calls land when the caller drains the iterator,
    not when `acompletion()` returns — otherwise we'd record zero tokens
    for every stream."""

    async def _gen():
        # Mid-stream chunks carry no usage; only the final chunk does.
        yield _FakeResponse(
            model="azure/gpt-5-mini",
            usage=_FakeUsage(prompt_tokens=0, completion_tokens=0),
            _hidden_params={},
        )
        yield _FakeResponse(
            model="azure/gpt-5-mini",
            usage=_FakeUsage(prompt_tokens=50, completion_tokens=200),
            _hidden_params={"response_cost": 0.001},
        )

    fake = _FakeRouter(response=_gen())
    monkeypatch.setattr(llm_mod, "get_llm_router", lambda: fake)

    iterator = await acompletion(
        ModelGroup.CODER, [{"role": "user", "content": "stream me"}], stream=True
    )

    # Before drain: no stats yet.
    pre = get_router_stats().snapshot()
    assert "azure/gpt-5-mini" not in pre

    async for _ in iterator:
        pass

    snap = get_router_stats().snapshot()
    bucket = snap["azure/gpt-5-mini"]
    assert bucket["calls"] == 1
    assert bucket["tokens_in"] == 50
    assert bucket["tokens_out"] == 200


# ── RouterStats direct tests ─────────────────────────────────────────────


def test_router_stats_aggregates_multiple_calls() -> None:
    stats = RouterStats()
    stats.record_success(model="m", latency_s=0.5, cost_usd=0.001, tokens_in=10, tokens_out=5)
    stats.record_success(model="m", latency_s=1.5, cost_usd=0.002, tokens_in=20, tokens_out=10)
    snap = stats.snapshot()["m"]
    assert snap["calls"] == 2
    assert snap["tokens_in"] == 30
    assert snap["tokens_out"] == 15
    assert snap["avg_latency_s"] == pytest.approx(1.0, rel=1e-3)
    assert snap["cost_usd"] == pytest.approx(0.003, rel=1e-3)


def test_router_stats_truncates_long_error() -> None:
    stats = RouterStats()
    stats.record_failure(model="m", error="X" * 500)
    snap = stats.snapshot()["m"]
    assert snap["last_error"] is not None
    assert len(snap["last_error"]) == 200


def test_router_stats_reset_clears_all() -> None:
    stats = RouterStats()
    stats.record_success(model="m", latency_s=0.1, cost_usd=0.0, tokens_in=1, tokens_out=1)
    assert stats.snapshot() != {}
    stats.reset()
    assert stats.snapshot() == {}
