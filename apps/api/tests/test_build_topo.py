"""Unit tests for `app.agents.build.topo.topological_order`.

Pure function, no async, no fixtures beyond BuildPlan/FileOp construction.
"""

from __future__ import annotations

import pytest

from alloy_shared.plan import BuildPlan, FileOp, FileOpKind
from app.agents.build.topo import (
    DuplicateTaskIdError,
    PlanCycleError,
    UnknownDependencyError,
    topological_order,
)


def _op(op_id: str, *, depends_on: list[str] | None = None) -> FileOp:
    """Minimal FileOp factory — most fields don't affect ordering."""
    return FileOp(
        kind=FileOpKind.create,
        path=f"apps/api/{op_id}.py",
        intent=f"Add {op_id}",
        depends_on=depends_on or [],
        id=op_id,
    )


def _plan(*ops: FileOp) -> BuildPlan:
    return BuildPlan(spec_slug="test", ops=list(ops))


def test_linear_chain_is_ordered_by_dependency() -> None:
    """a → b → c must come out [a, b, c]."""
    plan = _plan(
        _op("c", depends_on=["b"]),
        _op("b", depends_on=["a"]),
        _op("a"),
    )
    assert topological_order(plan) == ["a", "b", "c"]


def test_deterministic_tie_breaking_by_id() -> None:
    """Independent tasks come out in ascending id order — lets us write
    stable test assertions and reproducible Langfuse traces."""
    plan = _plan(_op("zzz"), _op("aaa"), _op("mid"))
    assert topological_order(plan) == ["aaa", "mid", "zzz"]


def test_parallel_branches_share_successor() -> None:
    """Diamond: a → b, a → c, (b, c) → d.

    Both b and c are ready after a; tie-break puts b before c. d runs
    last since it depends on both.
    """
    plan = _plan(
        _op("a"),
        _op("b", depends_on=["a"]),
        _op("c", depends_on=["a"]),
        _op("d", depends_on=["b", "c"]),
    )
    assert topological_order(plan) == ["a", "b", "c", "d"]


def test_empty_plan_returns_empty_list() -> None:
    assert topological_order(_plan()) == []


def test_duplicate_id_raises() -> None:
    with pytest.raises(DuplicateTaskIdError) as exc:
        topological_order(_plan(_op("x"), _op("x")))
    assert exc.value.task_id == "x"


def test_unknown_dependency_raises_with_specific_error() -> None:
    """A Planner typo (`depends_on=["b"]` when op `b` doesn't exist)
    must NOT be silently treated as a cycle — it's a planner bug and
    the error message needs to name the missing id."""
    with pytest.raises(UnknownDependencyError) as exc:
        topological_order(_plan(_op("a", depends_on=["ghost"])))
    assert exc.value.task_id == "a"
    assert exc.value.missing == "ghost"


def test_self_dependency_is_a_cycle() -> None:
    with pytest.raises(PlanCycleError) as exc:
        topological_order(_plan(_op("a", depends_on=["a"])))
    assert exc.value.cycle == ["a"]


def test_two_node_cycle_surfaces_chain() -> None:
    """a ↔ b — the error must enumerate the chain, not just say
    'cycle detected'."""
    plan = _plan(
        _op("a", depends_on=["b"]),
        _op("b", depends_on=["a"]),
    )
    with pytest.raises(PlanCycleError) as exc:
        topological_order(plan)
    # Order of chain depends on which node we walked from; we guarantee
    # smallest id first, so `a → b → a`.
    assert exc.value.cycle[0] == "a"
    assert "b" in exc.value.cycle


def test_cycle_plus_acyclic_prefix_only_reports_the_cycle() -> None:
    """prefix → (a ↔ b). The acyclic prefix schedules; the error
    names only the cyclic nodes so the Planner sees the actual loop."""
    plan = _plan(
        _op("prefix"),
        _op("a", depends_on=["prefix", "b"]),
        _op("b", depends_on=["a"]),
    )
    with pytest.raises(PlanCycleError) as exc:
        topological_order(plan)
    assert "prefix" not in exc.value.cycle
    assert set(exc.value.cycle) == {"a", "b"}
