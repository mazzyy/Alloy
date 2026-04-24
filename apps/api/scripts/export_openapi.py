"""Write the current FastAPI OpenAPI schema to disk.

Used by the frontend's `@hey-api/openapi-ts` generator and by CI's
"contract drift" check — generating the TS client from a file (not a
running server) keeps the pipeline deterministic and offline.

Usage:

    cd apps/api
    uv run python -m scripts.export_openapi                   # → ../web/openapi.json
    uv run python -m scripts.export_openapi -o schema.json    # custom path
    uv run python -m scripts.export_openapi --check           # diff-against-disk

The `--check` mode is for CI: exit code 1 if the committed schema on
disk is stale versus what the current code produces. That forces a
regenerate-client commit whenever route signatures change.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer


def _load_schema() -> dict[str, object]:
    # Import inside the function so `--help` doesn't pay the FastAPI
    # startup cost (pydantic-settings validation, LiteLLM import, etc.).
    from app.main import app

    return app.openapi()


def _canonical(schema: dict[str, object]) -> str:
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


def main(
    output: Annotated[
        Path,
        typer.Option(
            "-o",
            "--output",
            help="Where to write the schema. Defaults to apps/web/openapi.json.",
        ),
    ] = Path("../web/openapi.json"),
    check: Annotated[
        bool,
        typer.Option(
            "--check",
            help="Exit 1 if the on-disk schema differs from the current code.",
        ),
    ] = False,
) -> None:
    schema = _load_schema()
    body = _canonical(schema)
    resolved = output.resolve()

    if check:
        if not resolved.exists():
            typer.secho(
                f"missing: {resolved} — run without --check to generate it.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=1)
        on_disk = resolved.read_text(encoding="utf-8")
        if on_disk != body:
            typer.secho(
                "openapi schema drift — regenerate with `uv run python -m scripts.export_openapi`"
                " and `pnpm --filter web generate-client`.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=1)
        typer.secho("openapi schema up to date.", fg=typer.colors.GREEN)
        return

    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(body, encoding="utf-8")
    typer.echo(f"wrote {len(body)} bytes to {resolved}")


if __name__ == "__main__":  # pragma: no cover — entry point
    typer.run(main)
