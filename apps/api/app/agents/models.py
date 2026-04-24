"""Factory for Pydantic AI `Model` instances bound to our Azure OpenAI config.

We route everything through `pydantic_ai` instead of the raw Azure SDK for the
agent layer so tool dispatch, structured output validation, retry, and
streaming land on a single code path.

Key design notes:

* `get_planner_model()` returns an `OpenAIChatModel` wired to our primary
  Azure deployment via `AzureProvider`. We use the Chat Completions API
  (not the Responses API) because pydantic-ai's AzureProvider uses Chat
  Completions under the hood and the Responses API path has rougher edges
  on Azure in early 2026 (see roadmap §5 quirks).
* `reasoning_effort` + `verbosity` are passed through `OpenAIChatModelSettings`
  — pydantic-ai surfaces them as `openai_reasoning_effort` / `openai_text_verbosity`.
* In tests, callers swap the model via `agent.override(model=TestModel())`;
  we never check keys at module import time so tests don't need creds.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings
from pydantic_ai.providers.azure import AzureProvider
from pydantic_ai.settings import ModelSettings

from app.core.config import settings


class AgentModelConfigError(RuntimeError):
    """Raised when we try to instantiate an agent model without Azure creds."""


@lru_cache(maxsize=1)
def get_planner_model() -> OpenAIChatModel:
    """Primary planner/spec/coder model: Azure gpt-5-mini.

    Raises `AgentModelConfigError` if Azure isn't configured — callers in
    production should surface this as a 503; tests should call `agent.override`
    before any `agent.run*()` is reached.
    """
    if not settings.AZURE_OPENAI_ENDPOINT or not settings.AZURE_OPENAI_API_KEY:
        raise AgentModelConfigError(
            "Azure OpenAI is not configured. Set AZURE_OPENAI_ENDPOINT and "
            "AZURE_OPENAI_API_KEY, or override the agent's model in tests."
        )
    provider = AzureProvider(
        azure_endpoint=settings.AZURE_OPENAI_ENDPOINT,
        api_key=settings.AZURE_OPENAI_API_KEY,
        api_version=settings.AZURE_OPENAI_API_VERSION,
    )
    # When using Azure, the `model` arg is the **deployment name**, not the
    # underlying OpenAI model id. pydantic-ai's AzureProvider knows to pass
    # it through verbatim to the Azure endpoint.
    return OpenAIChatModel(
        model_name=settings.AZURE_OPENAI_DEPLOYMENT,
        provider=provider,
    )


def default_settings(
    *,
    reasoning_effort: str = "low",
    verbosity: str = "low",  # noqa: ARG001 — kept for call-site compatibility; see note below
    max_output_tokens: int = 4000,
) -> ModelSettings:
    """Shared defaults for every agent call.

    Start at `reasoning_effort=low` (roadmap §5 — avoid burning reasoning
    tokens on simple spec extraction); individual agents can bump this
    per-call via `agent.run(..., model_settings=...)`.

    We *do not* forward `verbosity` here. The roadmap claims Azure Chat
    Completions silently ignores unknown params, but in practice it
    rejects the request with `400 Unknown parameter: 'text'` when we
    send `extra_body={"text": {"verbosity": ...}}`. `text.verbosity` is a
    Responses-API-only field — use `get_openai_client()` + the Responses
    endpoint when verbosity actually needs to land. Pydantic AI's
    AzureProvider is Chat-Completions-only, so the parameter is kept in
    the signature for call-site compatibility but otherwise ignored.
    """
    return OpenAIChatModelSettings(
        openai_reasoning_effort=reasoning_effort,  # type: ignore[typeddict-item]
        max_tokens=max_output_tokens,
    )
