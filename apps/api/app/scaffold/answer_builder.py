"""AppSpec -> Copier-answers derivation.

The base template (`templates/base-react-fastapi`) ships its own `copier.yml`
with questions like `project_name`, `stack_name`, `secret_key`, `postgres_*`.
When the scaffolder runs non-interactively (every Alloy flow is
non-interactive), it must supply complete answers up front — Copier raises
otherwise.

This module is a pure function from `(AppSpec, extras)` to the answer dict.
Isolated here so both the scaffolder and a future `copier update` command can
regenerate identical answers.
"""

from __future__ import annotations

import secrets

from alloy_shared.spec import AppSpec


def _slugify_stack(slug: str) -> str:
    # Docker Compose labels can't contain most punctuation; underscores are
    # legal but awkward in DNS names, so we stick to lowercase + hyphens.
    cleaned = "".join(c if c.isalnum() or c == "-" else "-" for c in slug.lower())
    return cleaned.strip("-") or "alloy-app"


def build_answers(
    spec: AppSpec,
    *,
    first_superuser_email: str,
    domain_base: str = "localhost",
) -> dict[str, str]:
    """Return a complete Copier answer set for the base-react-fastapi template.

    `first_superuser_email` is required — the base template refuses to render
    without a valid email here. Other secrets are generated on the fly; the
    caller is responsible for replacing them before any deploy.
    """
    stack = _slugify_stack(spec.slug)
    secret = secrets.token_urlsafe(32)
    postgres_pw = secrets.token_urlsafe(24)
    first_pw = secrets.token_urlsafe(16)
    sentry_dsn = ""  # opt-in; user fills from dashboard later.

    return {
        "project_name": spec.name,
        "stack_name": stack,
        "secret_key": secret,
        "first_superuser": first_superuser_email,
        "first_superuser_password": first_pw,
        "smtp_host": "",
        "smtp_user": "",
        "smtp_password": "",
        "emails_from_email": f"no-reply@{domain_base}",
        "postgres_password": postgres_pw,
        "sentry_dsn": sentry_dsn,
        "domain": domain_base,
        "docker_image_backend": f"{stack}-backend",
        "docker_image_frontend": f"{stack}-frontend",
    }
