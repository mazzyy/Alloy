"""Generation endpoints.

Phase 0: a single smoke test — `POST /generate/echo` — that verifies Azure
OpenAI connectivity end-to-end. It sends a one-shot chat completion with
`reasoning_effort="low"` and streams the text back via Server-Sent Events.

Phase 1 replaces this with the real Spec Agent → Planner → Coder Agent
pipeline, where long-running work is enqueued into Arq and the HTTP handler
only streams incremental events (tokens, tool calls, diagnostics).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.api.deps import CurrentPrincipal
from app.core.config import settings
from app.core.llm import get_openai_client

router = APIRouter(prefix="/generate", tags=["generate"])


class EchoRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=4000)
    reasoning_effort: str = Field(default="low", pattern="^(minimal|low|medium|high)$")


@router.post("/echo", name="echo")
async def echo(body: EchoRequest, principal: CurrentPrincipal) -> StreamingResponse:
    """Stream a gpt-5-mini completion back as SSE events.

    Payload format:
        data: <chunk>\\n\\n
        ...
        data: [DONE]\\n\\n
    """
    if not settings.AZURE_OPENAI_API_KEY:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Azure OpenAI not configured.",
        )

    client = get_openai_client()

    async def event_stream() -> AsyncIterator[bytes]:
        try:
            stream = await client.chat.completions.create(
                model=settings.AZURE_OPENAI_DEPLOYMENT,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are Alloy, an AI full-stack generator in Phase 0 bootstrap. "
                            "Respond concisely in plain text."
                        ),
                    },
                    {"role": "user", "content": body.prompt},
                ],
                stream=True,
                stream_options={"include_usage": True},
                extra_body={
                    "prompt_cache_key": f"user:{principal.user_id}:echo:v1",
                    "reasoning_effort": body.reasoning_effort,
                },
                max_completion_tokens=1024,
            )
            async for chunk in stream:
                if chunk.choices and chunk.choices[0].delta.content:
                    token = chunk.choices[0].delta.content.replace("\n", "\\n")
                    yield f"data: {token}\n\n".encode()
            yield b"data: [DONE]\n\n"
        except Exception as exc:  # noqa: BLE001 — surface provider errors to UI
            err = str(exc).replace("\n", " ")
            yield f"event: error\ndata: {err}\n\n".encode()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx buffering
        },
    )
