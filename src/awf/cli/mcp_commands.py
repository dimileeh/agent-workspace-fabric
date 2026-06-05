"""MCP CLI command group."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

import typer

if TYPE_CHECKING:
    from awf.common.config import Settings

mcp_app = typer.Typer(help="MCP server integration.")


@mcp_app.command("serve")
def mcp_serve(
    env_file: Path | None = typer.Option(
        None,
        "--env-file",
        help=(
            "Optional dotenv file for the AWF local service environment. "
            "Use the root .env in source checkouts."
        ),
    ),
) -> None:
    """Run AWF's local MCP server over stdio."""
    try:
        _run_mcp_server(env_file=env_file)
    except ValueError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from None
    except Exception as exc:
        import logging
        import traceback

        logging.debug("MCP server setup failed:\n%s", traceback.format_exc())
        typer.echo(f"error: MCP server setup failed: {exc}", err=True)
        raise typer.Exit(code=2) from None


def _run_mcp_server(*, env_file: Path | None) -> None:
    """Build and run the AWF MCP server.

    Keep stdout reserved for the MCP stdio transport. Operator-facing errors are
    raised to the Typer wrapper, which prints them on stderr.
    """
    from awf.db.session import make_engine, make_session_factory
    from awf.mcp.server import build_mcp_server
    from awf.service import config as service_config
    from awf.service.workspaces import WorkspaceService

    settings = _resolve_mcp_settings(env_file=env_file)
    compose_env_file = (
        env_file.expanduser().resolve()
        if env_file is not None
        else service_config.COMPOSE_ENV_FILE_OMITTED
    )

    async def _run_stdio_and_dispose() -> None:
        engine = make_engine(settings.database_url)
        try:
            server = build_mcp_server(
                service=WorkspaceService(make_session_factory(engine), settings=settings),
                settings=settings,
                compose_env_file=compose_env_file,
            )
            await server.run_stdio_async()
        finally:
            await engine.dispose()

    asyncio.run(_run_stdio_and_dispose())


def _resolve_mcp_settings(*, env_file: Path | None) -> Settings:
    """Resolve Settings for the local MCP server command."""
    import dataclasses

    from awf.common.config import Settings
    from awf.service.config import local_service_environ, resolve_service_settings

    if env_file is None:
        base_settings = Settings()
        service_settings = resolve_service_settings(base_settings)
    else:
        resolved_env_file = env_file.expanduser().resolve()
        if not resolved_env_file.is_file():
            raise ValueError(f"MCP env file does not exist: {resolved_env_file}")
        service_environ = local_service_environ(env_file=resolved_env_file)
        base_settings = Settings(_env_file=resolved_env_file)
        service_settings = resolve_service_settings(base_settings, environ=service_environ)

    update_dict = dataclasses.asdict(service_settings)

    # Map renamed fields
    if "node_id" in update_dict:
        update_dict["worker_node_id"] = update_dict.pop("node_id")
    if "branch_prefix" in update_dict:
        update_dict["worker_branch_prefix"] = update_dict.pop("branch_prefix")

    # Only include keys if they have non-None values and are actually present in the target Settings
    valid_keys = type(base_settings).model_fields.keys()
    final_update = {k: v for k, v in update_dict.items() if v is not None and k in valid_keys}

    return base_settings.model_copy(update=final_update)
