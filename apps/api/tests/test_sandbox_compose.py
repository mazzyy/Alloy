"""`compose.alloy.yml` generator — pure string rendering tests."""

from __future__ import annotations

import yaml

from app.sandboxes.compose import ComposeRenderParams, preview_url_for, render_alloy_compose


def _params(**overrides: object) -> ComposeRenderParams:
    defaults: dict[str, object] = {
        "compose_project": "alloy-sbx-abc12345",
        "backend_port": 20001,
        "frontend_port": 20002,
        "postgres_user": "alloy",
        "postgres_password": "s3cret",
        "postgres_db": "alloy",
        "secret_key": "topsecret",
        "backend_env": None,
    }
    defaults.update(overrides)
    return ComposeRenderParams(**defaults)  # type: ignore[arg-type]


def test_render_alloy_compose_parses_as_valid_yaml():
    text = render_alloy_compose(_params())
    data = yaml.safe_load(text)
    assert data["name"] == "alloy-sbx-abc12345"
    assert set(data["services"]) == {"db", "backend", "frontend"}
    # DB must not publish a host port — sandbox isolation rule.
    assert "ports" not in data["services"]["db"]
    # Backend + frontend publish on 127.0.0.1, not 0.0.0.0.
    backend_ports = data["services"]["backend"]["ports"]
    assert any("127.0.0.1:20001:8000" in p for p in backend_ports)
    frontend_ports = data["services"]["frontend"]["ports"]
    assert any("127.0.0.1:20002:80" in p for p in frontend_ports)


def test_render_alloy_compose_injects_pg_env_into_backend():
    text = render_alloy_compose(_params())
    data = yaml.safe_load(text)
    env = data["services"]["backend"]["environment"]
    assert env["POSTGRES_SERVER"] == "db"
    assert env["POSTGRES_PORT"] == "5432"
    assert env["POSTGRES_USER"] == "alloy"
    assert env["POSTGRES_PASSWORD"] == "s3cret"
    assert env["POSTGRES_DB"] == "alloy"
    assert env["SECRET_KEY"] == "topsecret"
    assert env["BACKEND_PUBLIC_URL"] == "http://localhost:20001"


def test_render_alloy_compose_merges_extra_env():
    text = render_alloy_compose(
        _params(backend_env={"CLERK_JWKS_URL": "https://x.clerk.dev/.well-known/jwks.json"})
    )
    data = yaml.safe_load(text)
    assert (
        data["services"]["backend"]["environment"]["CLERK_JWKS_URL"]
        == "https://x.clerk.dev/.well-known/jwks.json"
    )


def test_render_alloy_compose_frontend_build_arg_points_at_backend():
    text = render_alloy_compose(_params(backend_port=20050, frontend_port=20051))
    data = yaml.safe_load(text)
    args = data["services"]["frontend"]["build"]["args"]
    assert args["VITE_API_URL"] == "http://localhost:20050"


def test_render_alloy_compose_quotes_values_with_special_chars():
    text = render_alloy_compose(_params(postgres_password="a:b#c!d"))
    data = yaml.safe_load(text)
    # YAML round-trip must preserve it.
    assert data["services"]["backend"]["environment"]["POSTGRES_PASSWORD"] == "a:b#c!d"


def test_render_alloy_compose_is_deterministic():
    p = _params(backend_env={"B": "2", "A": "1"})
    first = render_alloy_compose(p)
    second = render_alloy_compose(p)
    assert first == second


def test_preview_url_for():
    assert preview_url_for(20050) == "http://localhost:20050"
    assert preview_url_for(20050, host="sandbox.alloy.dev") == "http://sandbox.alloy.dev:20050"
