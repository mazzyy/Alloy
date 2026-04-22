"""Smoke tests for unauthenticated endpoints and auth bypass in local env."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


def test_health(client: TestClient) -> None:
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["environment"] in {"local", "staging", "production"}


def test_ping_allows_local_bootstrap(client: TestClient) -> None:
    # With CLERK_ISSUER unset in local env, auth is bypassed with a dev identity.
    r = client.get("/api/v1/ping")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["user_id"] == "dev_user"
    assert body["tenant_id"] == "dev_tenant"


def test_ping_rejects_malformed_bearer(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    # Simulate production-like config: issuer set, so any request without a
    # valid Clerk token must be rejected.
    from app.core import config as config_module

    monkeypatch.setattr(config_module.settings, "ENVIRONMENT", "staging")
    monkeypatch.setattr(config_module.settings, "CLERK_ISSUER", "https://clerk.example.com")

    r = client.get("/api/v1/ping", headers={"Authorization": "Bearer not-a-jwt"})
    assert r.status_code == 401
