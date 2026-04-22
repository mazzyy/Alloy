"""LLM router.

Per roadmap §5, we centralize every LLM call behind LiteLLM so that we can:

    - Route by region (Azure East US 2 → Azure Sweden Central)
    - Cascade on 429 / 5xx / timeout (Azure → OpenAI direct → Anthropic)
    - Track cost + latency per provider in Langfuse traces
    - Stabilize cache keys across providers

This module exposes two primitives the rest of the app should use:

    `llm_router` — a `litellm.Router` with the Azure-primary cascade wired up
    `get_openai_client()` — a raw Azure OpenAI SDK client for features LiteLLM
        doesn't surface yet (Responses API reasoning_effort, prompt_cache_key)

We keep the raw client because the roadmap calls for the Responses API path
with `reasoning_effort="low"` and `prompt_cache_key`, and LiteLLM's coverage
of `extra_body` on Azure is still patchy.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from openai import AsyncAzureOpenAI

from app.core.config import settings


@lru_cache(maxsize=1)
def get_openai_client() -> AsyncAzureOpenAI:
    if not settings.AZURE_OPENAI_ENDPOINT or not settings.AZURE_OPENAI_API_KEY:
        raise RuntimeError(
            "Azure OpenAI is not configured. Set AZURE_OPENAI_ENDPOINT and "
            "AZURE_OPENAI_API_KEY in .env."
        )
    return AsyncAzureOpenAI(
        azure_endpoint=settings.AZURE_OPENAI_ENDPOINT,
        api_key=settings.AZURE_OPENAI_API_KEY,
        api_version=settings.AZURE_OPENAI_API_VERSION,
    )


@lru_cache(maxsize=1)
def build_router_model_list() -> list[dict[str, Any]]:
    """LiteLLM model list. Safe to call even when keys are missing — entries
    without keys are filtered out so LiteLLM doesn't KeyError at init.
    """
    models: list[dict[str, Any]] = []

    if settings.AZURE_OPENAI_ENDPOINT and settings.AZURE_OPENAI_API_KEY:
        models.append(
            {
                "model_name": "planner",
                "litellm_params": {
                    "model": f"azure/{settings.AZURE_OPENAI_DEPLOYMENT}",
                    "api_base": settings.AZURE_OPENAI_ENDPOINT,
                    "api_key": settings.AZURE_OPENAI_API_KEY,
                    "api_version": settings.AZURE_OPENAI_API_VERSION,
                },
            }
        )

    if settings.AZURE_OPENAI_ENDPOINT_FALLBACK and settings.AZURE_OPENAI_API_KEY_FALLBACK:
        models.append(
            {
                "model_name": "planner",
                "litellm_params": {
                    "model": f"azure/{settings.AZURE_OPENAI_DEPLOYMENT}",
                    "api_base": settings.AZURE_OPENAI_ENDPOINT_FALLBACK,
                    "api_key": settings.AZURE_OPENAI_API_KEY_FALLBACK,
                    "api_version": settings.AZURE_OPENAI_API_VERSION,
                },
            }
        )

    if settings.OPENAI_API_KEY:
        models.append(
            {
                "model_name": "planner",
                "litellm_params": {
                    "model": "gpt-5-mini",
                    "api_key": settings.OPENAI_API_KEY,
                },
            }
        )

    if settings.ANTHROPIC_API_KEY:
        models.append(
            {
                "model_name": "planner",
                "litellm_params": {
                    "model": "anthropic/claude-sonnet-4-5",
                    "api_key": settings.ANTHROPIC_API_KEY,
                },
            }
        )

    return models


@lru_cache(maxsize=1)
def get_llm_router() -> Any:
    """Lazy-import LiteLLM so its top-level side effects don't block startup.

    Returns `None` if no models are configured — callers should guard.
    """
    models = build_router_model_list()
    if not models:
        return None
    # Import here to avoid LiteLLM's module-level network checks at import time.
    from litellm import Router

    return Router(
        model_list=models,
        routing_strategy="simple-shuffle",
        fallbacks=[{"planner": ["planner"]}],  # retry within the group
        num_retries=2,
        retry_after=1,
        allowed_fails=3,
        cooldown_time=30,
    )
