"""Introspection helpers for REST / CLI / MCP contract tests."""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from types import NoneType
from typing import Any, get_args, get_origin

from fastapi.routing import APIRoute
from typer.models import ArgumentInfo, OptionInfo

from awf.api.app import create_app
from awf.cli.main import app as cli_app
from awf.mcp.server import build_mcp_server


@dataclass(frozen=True)
class RestRouteInfo:
    method: str
    path: str
    response_model: str | None
    path_fields: frozenset[str]
    query_fields: frozenset[str]
    header_fields: frozenset[str]
    body_fields: frozenset[str]
    dependencies: frozenset[str]


@dataclass(frozen=True)
class CliCommandInfo:
    tokens: tuple[str, ...]
    callback_name: str
    options: frozenset[str]
    arguments: frozenset[str]
    argument_order: tuple[str, ...]


@dataclass(frozen=True)
class McpToolInfo:
    name: str
    properties: frozenset[str]
    required: frozenset[str]
    schema: dict[str, Any]


def rest_routes() -> dict[tuple[str, str], RestRouteInfo]:
    """Return FastAPI route metadata keyed by ``(method, path)``."""
    app = create_app(use_lifespan=False)
    out: dict[tuple[str, str], RestRouteInfo] = {}
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        for method in route.methods or []:
            out[(method, route.path)] = RestRouteInfo(
                method=method,
                path=route.path,
                response_model=_model_name(route.response_model),
                path_fields=frozenset(param.name for param in route.dependant.path_params),
                query_fields=frozenset(
                    param.alias or param.name for param in route.dependant.query_params
                ),
                header_fields=frozenset(param.alias for param in route.dependant.header_params),
                body_fields=_body_fields(route),
                dependencies=frozenset(
                    getattr(dependency.call, "__name__", str(dependency.call))
                    for dependency in route.dependant.dependencies
                ),
            )
    return out


def cli_commands() -> dict[tuple[str, ...], CliCommandInfo]:
    """Return Typer command metadata keyed by visible command tokens."""
    out: dict[tuple[str, ...], CliCommandInfo] = {}

    def walk(typer_app: Any, prefix: tuple[str, ...] = ()) -> None:
        for command in getattr(typer_app, "registered_commands", []):
            callback = command.callback
            if callback is None:
                continue
            name = command.name or callback.__name__.replace("_", "-")
            tokens = prefix + (name,)
            out[tokens] = _cli_command_info(tokens, callback)
        for group in getattr(typer_app, "registered_groups", []):
            name = group.name
            if name:
                walk(group.typer_instance, prefix + (name,))

    walk(cli_app)
    return out


async def mcp_tools() -> dict[str, McpToolInfo]:
    """Return MCP tool schema metadata keyed by tool name."""
    mcp = build_mcp_server(service=object())  # type: ignore[arg-type]
    tools = await mcp.list_tools()
    out: dict[str, McpToolInfo] = {}
    for tool in tools:
        schema = tool.inputSchema
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        out[tool.name] = McpToolInfo(
            name=tool.name,
            properties=frozenset(properties),
            required=frozenset(required),
            schema=schema,
        )
    return out


def _cli_command_info(tokens: tuple[str, ...], callback: Any) -> CliCommandInfo:
    options: set[str] = set()
    arguments: set[str] = set()
    argument_order: list[str] = []
    for name, parameter in inspect.signature(callback).parameters.items():
        default = parameter.default
        if isinstance(default, OptionInfo):
            declarations = getattr(default, "param_decls", ()) or ()
            options.update(
                str(declaration)
                for declaration in declarations
                if str(declaration).startswith("--")
            )
        elif isinstance(default, ArgumentInfo):
            arguments.add(name)
            argument_order.append(name)
    return CliCommandInfo(
        tokens=tokens,
        callback_name=callback.__name__,
        options=frozenset(options),
        arguments=frozenset(arguments),
        argument_order=tuple(argument_order),
    )


def _body_fields(route: APIRoute) -> frozenset[str]:
    body_field = route.body_field
    if body_field is None:
        return frozenset()
    annotation = getattr(body_field.field_info, "annotation", None)
    fields = getattr(annotation, "model_fields", None)
    if isinstance(fields, dict):
        return frozenset(fields)
    return frozenset()


def _model_name(model: Any) -> str | None:
    if model is None or model is NoneType:
        return None
    origin = get_origin(model)
    if origin is list:
        args = get_args(model)
        if args:
            return f"list[{_model_name(args[0])}]"
        return "list"
    if origin is dict:
        args = get_args(model)
        if args:
            return f"dict[{', '.join(_model_name(arg) or str(arg) for arg in args)}]"
        return "dict"
    return getattr(model, "__name__", str(model))
