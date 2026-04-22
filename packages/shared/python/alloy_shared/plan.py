"""BuildPlan — the Planner Agent's ordered DAG of file operations.

Phase 0 ships the outer shape. Phase 1 expands to include explicit dependency
declarations so the LangGraph executor can parallelize independent tasks.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class FileOpKind(str, Enum):
    create = "create"
    modify = "modify"
    delete = "delete"
    move = "move"


class FileOp(_Base):
    kind: FileOpKind
    path: str
    # Human-readable one-liner used as the git commit message when this op
    # completes. The Coder Agent also sees this as its task description.
    intent: str
    # IDs of FileOps that must complete before this one runs.
    depends_on: list[str] = Field(default_factory=list)
    # The Planner assigns stable ids. Phase 1 also stores which blocks each op
    # came from (e.g. "billing/stripe").
    id: str


class BuildPlan(_Base):
    spec_slug: str
    base_template: Literal["react-fastapi"] = "react-fastapi"
    blocks: list[str] = Field(default_factory=list)  # e.g. ["auth/clerk", "storage/r2"]
    ops: list[FileOp] = Field(default_factory=list)
    schema_version: Literal[1] = 1
