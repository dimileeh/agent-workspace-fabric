"""Adapter registration and construction API."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from awf.db.enums import AgentRuntime

if TYPE_CHECKING:
    from awf.adapters.base import AgentAdapter, AgentDefaults
    from awf.adapters.runtime_executor import AgentRuntimeExecutor
    from awf.adapters.usage import UsageSampler
    from awf.common.commands import AsyncCommandRunner
    from awf.runtime.logs import LogStore


_REGISTRY: dict[AgentRuntime, type[object]] = {}
"""Adapter classes keyed by their declared runtime."""


def register_adapter[T](cls: type[T]) -> type[T]:
    """Class decorator used by each adapter module to self-register."""
    instance = cls.__new__(cls)  # bypass __init__ just to read .name
    runtime: AgentRuntime = getattr(cls, "runtime")  # noqa: B009 - structural check
    _REGISTRY[runtime] = cls
    del instance
    return cls


def get_adapter(
    runtime: AgentRuntime | str,
    *,
    runner: AsyncCommandRunner,
    default_model: str | None = None,
    default_effort: str | None = None,
    defaults: AgentDefaults | None = None,
    log_store: LogStore | None = None,
    agent_wall_timeout_seconds: float = 7200.0,
    agent_idle_timeout_seconds: float = 3600.0,
    usage_sampler: UsageSampler | None = None,
    runtime_executor: AgentRuntimeExecutor | None = None,
) -> AgentAdapter:
    """Instantiate the adapter for the given runtime."""
    if isinstance(runtime, str) and not isinstance(runtime, AgentRuntime):
        import contextlib

        with contextlib.suppress(ValueError):
            runtime = AgentRuntime(runtime)
    if defaults is not None:
        default_model = defaults.model
        default_effort = defaults.effort
    cls: Any = _REGISTRY.get(runtime)  # type: ignore[arg-type]
    if cls is None:
        from awf.adapters.base import RetiredAgentAdapter

        return RetiredAgentAdapter(
            runtime,
            runner=runner,
            default_model=default_model,
            default_effort=default_effort,
            log_store=log_store,
            agent_wall_timeout_seconds=agent_wall_timeout_seconds,
            agent_idle_timeout_seconds=agent_idle_timeout_seconds,
            usage_sampler=usage_sampler,
            runtime_executor=runtime_executor,
        )
    return cast(
        "AgentAdapter",
        cls(
            runner=runner,
            default_model=default_model,
            default_effort=default_effort,
            log_store=log_store,
            agent_wall_timeout_seconds=agent_wall_timeout_seconds,
            agent_idle_timeout_seconds=agent_idle_timeout_seconds,
            usage_sampler=usage_sampler,
            runtime_executor=runtime_executor,
        ),
    )
