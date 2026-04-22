"""AppSpec — the user-editable description of an Alloy-generated application.

The Intake Agent asks clarifying questions and the Spec Agent produces an
`AppSpec`. The UI shows this to the user before any code is written — editing
the spec is the single biggest trust-builder for non-technical users.

Phase 0 pins the outer shape; Phase 1 expands field coverage (validators,
permissions DSL, theme customization).
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=False, str_strip_whitespace=True)


# ── Auth ─────────────────────────────────────────────────────────────


class AuthProvider(str, Enum):
    fastapi_users_jwt = "fastapi_users_jwt"
    custom_jwt = "custom_jwt"
    clerk = "clerk"


class AuthConfig(_Base):
    provider: AuthProvider = AuthProvider.clerk
    allow_signup: bool = True
    require_email_verify: bool = True


# ── Data model ───────────────────────────────────────────────────────


FieldType = Literal[
    "string", "text", "int", "float", "bool", "datetime", "date", "uuid", "json", "ref",
]


class EntityField(_Base):
    name: str = Field(..., pattern=r"^[a-z][a-z0-9_]*$")
    type: FieldType
    required: bool = True
    unique: bool = False
    indexed: bool = False
    # For type="ref": target entity name. Phase 1 expands this into a full
    # Relation model with on_delete semantics.
    ref: str | None = None


class Entity(_Base):
    name: str = Field(..., pattern=r"^[A-Z][A-Za-z0-9]*$")
    plural: str | None = None  # derived if None
    fields: list[EntityField]
    auditable: bool = True  # created_at / updated_at auto-added


# ── HTTP surface ─────────────────────────────────────────────────────


RoutePermission = Literal["public", "authenticated", "owner", "admin"]
HttpMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE"]


class Route(_Base):
    method: HttpMethod
    path: str  # e.g. "/todos/{id}"
    handler_name: str  # snake_case; the Coder Agent implements this function
    permission: RoutePermission = "authenticated"
    description: str | None = None


# ── UI surface ───────────────────────────────────────────────────────


class Page(_Base):
    name: str  # PascalCase component name
    path: str  # react-router path, e.g. "/dashboard/:id"
    description: str | None = None
    # Names of routes whose responses this page consumes (stable ID, not path).
    data_deps: list[str] = Field(default_factory=list)


# ── Integrations ─────────────────────────────────────────────────────


IntegrationKind = Literal["stripe", "r2", "resend", "clerk", "vercel", "github", "daytona"]


class Integration(_Base):
    kind: IntegrationKind
    # Phase 1 lets each integration attach its own config block.


# ── Top-level spec ───────────────────────────────────────────────────


class AppSpec(_Base):
    name: str
    slug: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]*$")]
    description: str

    auth: AuthConfig = AuthConfig()
    entities: list[Entity] = Field(default_factory=list)
    routes: list[Route] = Field(default_factory=list)
    pages: list[Page] = Field(default_factory=list)
    integrations: list[Integration] = Field(default_factory=list)

    # Schema versioning so future Alloy releases can migrate old specs.
    schema_version: Literal[1] = 1
