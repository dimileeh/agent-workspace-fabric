"""Retired agent adapter implementation."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from awf.adapters.base import AgentAdapter
from awf.adapters.run_results import AgentRunError, AgentRunResult
from awf.db.enums import AgentRuntime

if TYPE_CHECKING:
    from awf.adapters.runtime_executor import AgentRuntimeExecutor
    from awf.adapters.usage import UsageSampler
    from awf.common.commands import AsyncCommandRunner
    from awf.runtime.logs import LogStore


class RetiredAgentAdapter(AgentAdapter):
    """Fallback adapter for retired or unsupported agent runtimes.

    Retained so historical or monitor-only workspaces can construct a monitor
    and inspect clean/merge-ready PRs without failing at resume. If an actionable
    repair invokes the agent, `run` fails fast with UNSUPPORTED_AGENT_RUNTIME.
    """

    def __init__(
        self,
        runtime: AgentRuntime | str,
        *,
        runner: AsyncCommandRunner,
        default_model: str | None = None,
        default_effort: str | None = None,
        log_store: LogStore | None = None,
        agent_wall_timeout_seconds: float = 7200.0,
        agent_idle_timeout_seconds: float = 3600.0,
        usage_sampler: UsageSampler | None = None,
        runtime_executor: AgentRuntimeExecutor | None = None,
    ) -> None:
        super().__init__(
            runner=runner,
            default_model=default_model,
            default_effort=default_effort,
            log_store=log_store,
            agent_wall_timeout_seconds=agent_wall_timeout_seconds,
            agent_idle_timeout_seconds=agent_idle_timeout_seconds,
            usage_sampler=usage_sampler,
            runtime_executor=runtime_executor,
        )
        self._runtime: AgentRuntime | str
        if isinstance(runtime, AgentRuntime):
            self._runtime = runtime
            self._runtime_str = runtime.value
        else:
            self._runtime_str = str(runtime)
            try:
                self._runtime = AgentRuntime(runtime)
            except ValueError:
                self._runtime = str(runtime)

    @property
    def is_retired(self) -> bool:
        return True

    @property
    def name(self) -> AgentRuntime | str:
        return self._runtime

    def get_provider(self, model: str | None) -> str:
        del model
        return "unsupported"

    def _cli_args(self, *, model: str | None) -> list[str]:
        del model
        return []

    async def run(
        self,
        *,
        compose_project: str,
        compose_file: Path,
        prompt: str,
        model: str | None = None,
        workspace_id: str | None = None,
        log_source: str = "agent",
        **kwargs: Any,
    ) -> AgentRunResult:
        del compose_project, compose_file, prompt, workspace_id, log_source, kwargs
        from awf.adapters.provider_failures import classify_provider_failure
        from awf.common.commands import CommandResult
        from awf.service.provider_readiness import supported_launchable_agents

        supported = ", ".join(sorted(supported_launchable_agents()))
        message = f"agent runtime {self._runtime_str!r} is not supported; supported runtimes: {supported}."
        provider = self.get_provider(model)
        classification = classify_provider_failure(
            reason_code="UNSUPPORTED_AGENT_RUNTIME",
            stdout="",
            stderr=message,
            provider=provider,
            model=model,
        )
        provider_recovery = (
            classification.to_metadata()
            if classification is not None
            else {
                "reason_code": "UNSUPPORTED_AGENT_RUNTIME",
                "failure_type": "unsupported_runtime",
                "failure_scope": "provider",
                "provider": provider,
                "model": model,
                "retryable": True,
                "cooldown_seconds": 0,
                "recommended_action": "Dispatch an approved fallback agent runtime.",
                "fallback_allowed": True,
            }
        )
        raise AgentRunError(
            agent=self._runtime,
            result=CommandResult(returncode=1, stdout="", stderr=message),
            reason_code="UNSUPPORTED_AGENT_RUNTIME",
            details={
                "agent": self._runtime_str,
                "provider_recovery": provider_recovery,
            },
        )
