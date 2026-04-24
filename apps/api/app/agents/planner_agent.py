"""Planner Agent — AppSpec → BuildPlan.

Roadmap §3 Phase 2: "The Planner Agent turns AppSpec into an ordered
`BuildPlan`: a DAG of file operations with explicit dependencies (models →
migrations → schemas → CRUD helpers → routers → tests → OpenAPI export → TS
client → hooks → pages). It also selects the base template and the list of
blocks to merge."

For Phase 1 first slice we split the work:

* **Deterministic block resolution** (`resolve_blocks_for_spec`) picks
  feature blocks from `spec.integrations` and `spec.auth` — pure function,
  fully testable, no LLM.
* **LLM planner** produces the ordered `FileOp` DAG. Its job is *not* to be
  creative — the prompt lists the canonical op order and asks the model to
  emit the right set of ops for the given entities/routes/pages with stable
  IDs and proper `depends_on` edges. We accept some LLM involvement here
  because per-entity ordering + handler-name → op-id routing is fiddly and
  the LLM is fast/cheap at this.

When we move to the Phase 3 scaffolder we'll make this fully deterministic;
until then this agent bridges the gap.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from alloy_shared.plan import BuildPlan
from alloy_shared.spec import AppSpec
from pydantic_ai import Agent

from app.agents.models import default_settings, get_planner_model

_PROMPTS_DIR = Path(__file__).parent / "prompts"

# Alloy's initial block catalogue. Extend as Phase 1 wk3 ships more blocks.
_BLOCK_CATALOGUE: dict[str, set[str]] = {
    "clerk": {"auth/clerk"},
    # Both `fastapi_users_jwt` and `custom_jwt` AppSpec auth providers map
    # to the same self-hosted JWT block — the difference between them is a
    # surface-level "do we want fastapi_users' helper or roll our own
    # router". Phase 1 ships one canonical implementation; Phase 2 can
    # split if usage tells us it's worth two.
    "jwt": {"auth/jwt"},
    "r2": {"storage/r2"},
    "stripe": {"billing/stripe-subscriptions"},
    "resend": {"email/resend"},
    # Phase 2+ blocks — intentionally left out of the default resolver until
    # their block.yaml manifests exist.
}


def resolve_blocks_for_spec(spec: AppSpec) -> list[str]:
    """Pick the set of feature blocks this spec needs.

    Deterministic. Input is the user-editable AppSpec, output is a stable
    sorted list of block identifiers the scaffolder will apply.
    """
    blocks: set[str] = set()

    # Auth provider → block
    provider = spec.auth.provider.value
    if provider == "clerk":
        blocks.update(_BLOCK_CATALOGUE["clerk"])
    elif provider in {"fastapi_users_jwt", "custom_jwt"}:
        blocks.update(_BLOCK_CATALOGUE["jwt"])

    # Integrations → blocks
    for integ in spec.integrations:
        blocks.update(_BLOCK_CATALOGUE.get(integ.kind, set()))

    return sorted(blocks)


def _load_prompt(name: str) -> str:
    return (_PROMPTS_DIR / name).read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def build_planner_agent() -> Agent[None, BuildPlan]:
    """Return the singleton Planner Agent."""
    return Agent[None, BuildPlan](
        model=get_planner_model(),
        output_type=BuildPlan,
        system_prompt=_load_prompt("planner_agent.md"),
        model_settings=default_settings(reasoning_effort="low", max_output_tokens=6000),
        retries=2,
        name="alloy.planner_agent",
    )


def build_planner_user_prompt(spec: AppSpec, blocks: list[str]) -> str:
    """Render the Planner's user message: the AppSpec JSON + pre-resolved blocks."""
    spec_json = spec.model_dump_json(indent=2)
    blocks_list = "\n".join(f"- {b}" for b in blocks) if blocks else "- (none)"
    return (
        f"<app_spec>\n{spec_json}\n</app_spec>\n\n"
        f"<resolved_blocks>\n{blocks_list}\n</resolved_blocks>\n\n"
        "Emit a `BuildPlan` whose `blocks` field matches the resolved list "
        "above exactly, and whose `ops` list contains the canonical ordered "
        "FileOp set for this spec's entities, routes, and pages."
    )
