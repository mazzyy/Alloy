"""Application settings.

Source of truth for every knob in the roadmap. Values flow from environment
variables (see .env.example at the repo root). Pydantic Settings validates types
and fails fast on missing required values in non-local environments.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Any, Literal

from pydantic import AnyHttpUrl, BeforeValidator, HttpUrl, PostgresDsn, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _parse_list(v: Any) -> list[str] | str:
    if isinstance(v, str) and not v.startswith("["):
        return [s.strip() for s in v.split(",") if s.strip()]
    if isinstance(v, list | str):
        return v
    raise ValueError(v)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # Two levels up from apps/api/app/core/config.py -> repo root .env
        env_file=("../.env", "../../.env", ".env"),
        env_ignore_empty=True,
        extra="ignore",
        case_sensitive=True,
    )

    # ─── App metadata ───────────────────────────────────────────────
    PROJECT_NAME: str = "Alloy"
    API_V1_STR: str = "/api/v1"
    ENVIRONMENT: Literal["local", "staging", "production"] = "local"
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # ─── CORS ───────────────────────────────────────────────────────
    FRONTEND_HOST: AnyHttpUrl = AnyHttpUrl("http://localhost:5173")
    BACKEND_CORS_ORIGINS: Annotated[list[AnyHttpUrl] | str, BeforeValidator(_parse_list)] = []

    @computed_field
    @property
    def all_cors_origins(self) -> list[str]:
        origins = {str(self.FRONTEND_HOST).rstrip("/")}
        for o in self.BACKEND_CORS_ORIGINS or []:
            origins.add(str(o).rstrip("/"))
        return sorted(origins)

    # ─── Database ───────────────────────────────────────────────────
    POSTGRES_SERVER: str = "localhost"
    POSTGRES_PORT: int = 5432
    POSTGRES_USER: str = "alloy"
    POSTGRES_PASSWORD: str = "alloy"
    POSTGRES_DB: str = "alloy"

    @computed_field
    @property
    def DATABASE_URL_SYNC(self) -> PostgresDsn:
        return PostgresDsn.build(
            scheme="postgresql+psycopg",
            username=self.POSTGRES_USER,
            password=self.POSTGRES_PASSWORD,
            host=self.POSTGRES_SERVER,
            port=self.POSTGRES_PORT,
            path=self.POSTGRES_DB,
        )

    @computed_field
    @property
    def DATABASE_URL_ASYNC(self) -> PostgresDsn:
        return PostgresDsn.build(
            scheme="postgresql+asyncpg",
            username=self.POSTGRES_USER,
            password=self.POSTGRES_PASSWORD,
            host=self.POSTGRES_SERVER,
            port=self.POSTGRES_PORT,
            path=self.POSTGRES_DB,
        )

    # ─── Redis (Arq + cache + rate-limit) ───────────────────────────
    REDIS_URL: str = "redis://localhost:6379/0"

    # ─── Clerk (auth) ───────────────────────────────────────────────
    # Leave blank to disable auth enforcement (local bootstrap only — never
    # leave unset in staging/production; handled by _guard_auth_required).
    CLERK_ISSUER: str | None = None  # e.g. https://clerk.<org>.com
    CLERK_JWKS_URL: str | None = None  # derived from issuer if not set
    CLERK_AUDIENCE: str | None = None  # optional; Clerk tokens omit `aud` by default

    # ─── Azure OpenAI ───────────────────────────────────────────────
    AZURE_OPENAI_ENDPOINT: str | None = None
    AZURE_OPENAI_API_KEY: str | None = None
    AZURE_OPENAI_DEPLOYMENT: str = "gpt-5-mini"
    AZURE_OPENAI_API_VERSION: str = "2025-04-01-preview"
    # Second region for LiteLLM fallback routing. Fill in when the user
    # provisions a Sweden Central deployment alongside East US 2.
    AZURE_OPENAI_ENDPOINT_FALLBACK: str | None = None
    AZURE_OPENAI_API_KEY_FALLBACK: str | None = None

    # ─── OpenAI / Anthropic fallback (LiteLLM cascade) ──────────────
    OPENAI_API_KEY: str | None = None
    ANTHROPIC_API_KEY: str | None = None

    # ─── Apply-model providers ──────────────────────────────────────
    MORPH_API_KEY: str | None = None
    RELACE_API_KEY: str | None = None

    # ─── Observability ──────────────────────────────────────────────
    SENTRY_DSN: HttpUrl | None = None
    LANGFUSE_HOST: HttpUrl | None = None
    LANGFUSE_PUBLIC_KEY: str | None = None
    LANGFUSE_SECRET_KEY: str | None = None

    # ─── Object store / sandboxes / deploy targets ──────────────────
    R2_ACCOUNT_ID: str | None = None
    R2_ACCESS_KEY_ID: str | None = None
    R2_SECRET_ACCESS_KEY: str | None = None
    R2_BUCKET: str | None = None

    DAYTONA_API_URL: str | None = None
    DAYTONA_API_KEY: str | None = None

    GITHUB_APP_ID: str | None = None
    GITHUB_APP_PRIVATE_KEY: str | None = None  # PEM contents

    VERCEL_TOKEN: str | None = None
    VERCEL_TEAM_ID: str | None = None

    # ─── Billing ────────────────────────────────────────────────────
    STRIPE_SECRET_KEY: str | None = None
    STRIPE_WEBHOOK_SECRET: str | None = None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
