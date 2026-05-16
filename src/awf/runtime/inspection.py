"""Docker runtime inspection for active workspace compose stacks."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field


@dataclass(frozen=True)
class RuntimeService:
    name: str
    container_id: str | None
    image: str | None
    state: str
    command: str | None = None
    status: str | None = None
    health: str | None = None
    ports: list[str] = field(default_factory=list)
    started_at: str | None = None


@dataclass(frozen=True)
class RuntimeSnapshot:
    stack_state: str
    services: list[RuntimeService] = field(default_factory=list)
    reason: str | None = None


class RuntimeInspector:
    """Inspect compose-managed workspace containers through the Docker CLI."""

    async def inspect(self, compose_project_name: str | None) -> RuntimeSnapshot:
        if not compose_project_name:
            return RuntimeSnapshot(stack_state="unknown", reason="workspace has no compose project")
        ps = await _run(
            [
                "docker",
                "ps",
                "-a",
                "--filter",
                f"label=com.docker.compose.project={compose_project_name}",
                "--format",
                "{{json .}}",
            ]
        )
        if ps.returncode != 0:
            return RuntimeSnapshot(stack_state="unavailable", reason=ps.stderr or ps.stdout)
        services: list[RuntimeService] = []
        for line in ps.stdout.splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            container_id = row.get("ID")
            inspect_data = await _inspect_container(str(container_id)) if container_id else {}
            state_data = inspect_data.get("State") if isinstance(inspect_data, dict) else {}
            health = None
            if isinstance(state_data, dict):
                health_data = state_data.get("Health")
                if isinstance(health_data, dict):
                    health = health_data.get("Status")
            service_name = _service_name(row, inspect_data)
            services.append(
                RuntimeService(
                    name=service_name,
                    container_id=container_id,
                    image=row.get("Image"),
                    state=_state_from(row, state_data),
                    command=_command_from(row, inspect_data),
                    status=row.get("Status"),
                    health=health,
                    ports=_ports_from(row),
                    started_at=state_data.get("StartedAt")
                    if isinstance(state_data, dict)
                    else None,
                )
            )
        if not services:
            return RuntimeSnapshot(stack_state="stopped", services=[])
        if any(s.state == "running" for s in services):
            return RuntimeSnapshot(stack_state="running", services=services)
        return RuntimeSnapshot(stack_state="stopped", services=services)


@dataclass(frozen=True)
class _ProcessResult:
    returncode: int
    stdout: str
    stderr: str


async def _run(args: list[str]) -> _ProcessResult:
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        return _ProcessResult(
            returncode=127,
            stdout="",
            stderr=f"{args[0]} executable is not available: {exc}",
        )
    stdout, stderr = await proc.communicate()
    assert proc.returncode is not None
    return _ProcessResult(
        returncode=proc.returncode,
        stdout=stdout.decode("utf-8", errors="replace"),
        stderr=stderr.decode("utf-8", errors="replace"),
    )


async def _inspect_container(container_id: str) -> dict[str, object]:
    result = await _run(["docker", "inspect", container_id])
    if result.returncode != 0:
        return {}
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}
    if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
        return parsed[0]
    return {}


def _service_name(row: dict[str, object], inspect_data: dict[str, object]) -> str:
    labels: object = {}
    config = inspect_data.get("Config") if inspect_data else None
    if isinstance(config, dict):
        labels = config.get("Labels", {})
    if isinstance(labels, dict):
        service = labels.get("com.docker.compose.service")
        if isinstance(service, str) and service:
            return service
    names = row.get("Names")
    return str(names or row.get("ID") or "unknown")


def _command_from(
    row: dict[str, object],
    inspect_data: dict[str, object],
) -> str | None:
    command = _text_or_none(row.get("Command"))
    if command is not None:
        return command
    config = inspect_data.get("Config")
    if isinstance(config, dict):
        config_cmd = config.get("Cmd")
        if isinstance(config_cmd, list):
            parts: list[str] = []
            for value in config_cmd:
                part = _text_or_none(value)
                if part is not None:
                    parts.append(part)
            if parts:
                return " ".join(parts)
        else:
            command = _text_or_none(config_cmd)
        if command is not None:
            return command
    return None


def _text_or_none(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if value else None


def _state_from(row: dict[str, object], state_data: object) -> str:
    if isinstance(state_data, dict):
        status = state_data.get("Status")
        if isinstance(status, str) and status:
            return status
    state = row.get("State")
    return str(state or "unknown").lower()


def _ports_from(row: dict[str, object]) -> list[str]:
    ports = row.get("Ports")
    if isinstance(ports, str) and ports:
        return [p.strip() for p in ports.split(",") if p.strip()]
    return []
