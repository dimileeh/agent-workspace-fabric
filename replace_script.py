import re

with open("src/awf/runtime/pr_monitor_runner.py", "r") as f:
    content = f.read()

# 1. Fix _invoke_cli_for_verdict
old_invoke = """
        try:
            result = await self._deps.adapter.run(
                compose_project=compose_project,
                compose_file=compose_file,
                prompt=prompt,
                workspace_id=workspace_id,
                log_source="recovery",
            )
            result_stdout = result.stdout
        except AgentRunError as exc:
            try:
                await self._handle_provider_agent_run_error(workspace_id, exc)
            except ProviderRecoveryRetryError:
                raise
            cli_failed = True
            result_stdout = exc.result.stdout
            _log.warning(
                "monitor.cli_nonzero_exit",
                returncode=exc.result.returncode,
            )
        committed_dirty_changes = await self._commit_dirty_worktree(
            workspace_id=workspace_id,
            message=commit_message,
        )
        if committed_dirty_changes:
            return "fix_committed"
        if cli_failed:
            return "agent_failed"
        return _parse_verdict(result_stdout)
"""

new_invoke = """
        agent_run_err = None
        try:
            result = await self._deps.adapter.run(
                compose_project=compose_project,
                compose_file=compose_file,
                prompt=prompt,
                workspace_id=workspace_id,
                log_source="recovery",
            )
            result_stdout = result.stdout
        except AgentRunError as exc:
            cli_failed = True
            result_stdout = exc.result.stdout
            agent_run_err = exc

        committed_dirty_changes = await self._commit_dirty_worktree(
            workspace_id=workspace_id,
            message=commit_message,
        )

        if agent_run_err is not None:
            await self._handle_provider_agent_run_error(workspace_id, agent_run_err)
            _log.warning(
                "monitor.cli_nonzero_exit",
                returncode=agent_run_err.result.returncode,
            )

        if committed_dirty_changes:
            return "fix_committed"
        if cli_failed:
            return "agent_failed"
        return _parse_verdict(result_stdout)
"""

# 2. Fix _run_sync_base
old_sync_base = """
            try:
                if not await self._provider_recovery_suppresses_cli(workspace_id):
                    await self._deps.adapter.run(
                        compose_project=compose_project,
                        compose_file=compose_file,
                        prompt=prompt,
                        workspace_id=workspace_id,
                        log_source="recovery",
                    )
            except AgentRunError as exc:
                await self._handle_provider_agent_run_error(workspace_id, exc)
                _log.warning(
                    "monitor.sync_base_cli_failed",
                    workspace_id=workspace_id,
                    stderr=exc.result.stderr[:400],
                )
            await self._commit_dirty_worktree(
                workspace_id=workspace_id,
                message=f"fix: resolve PR #{pr_number} base conflicts",
            )
"""

new_sync_base = """
            agent_run_err = None
            try:
                if not await self._provider_recovery_suppresses_cli(workspace_id):
                    await self._deps.adapter.run(
                        compose_project=compose_project,
                        compose_file=compose_file,
                        prompt=prompt,
                        workspace_id=workspace_id,
                        log_source="recovery",
                    )
            except AgentRunError as exc:
                agent_run_err = exc

            await self._commit_dirty_worktree(
                workspace_id=workspace_id,
                message=f"fix: resolve PR #{pr_number} base conflicts",
            )

            if agent_run_err is not None:
                await self._handle_provider_agent_run_error(workspace_id, agent_run_err)
                _log.warning(
                    "monitor.sync_base_cli_failed",
                    workspace_id=workspace_id,
                    stderr=agent_run_err.result.stderr[:400],
                )
"""

# 3. Fix _run_ci_fix
old_ci_fix = """
        try:
            if not await self._provider_recovery_suppresses_cli(workspace_id):
                await self._deps.adapter.run(
                    compose_project=compose_project,
                    compose_file=compose_file,
                    prompt=prompt,
                    workspace_id=workspace_id,
                    log_source="recovery",
                )
        except AgentRunError as exc:
            await self._handle_provider_agent_run_error(workspace_id, exc)
            _log.warning(
                "monitor.ci_fix_cli_failed",
                workspace_id=workspace_id,
                stderr=exc.result.stderr[:400],
            )
        await self._commit_dirty_worktree(
            workspace_id=workspace_id,
            message=f"fix: address PR #{pr_number} CI failure",
        )
"""

new_ci_fix = """
        agent_run_err = None
        try:
            if not await self._provider_recovery_suppresses_cli(workspace_id):
                await self._deps.adapter.run(
                    compose_project=compose_project,
                    compose_file=compose_file,
                    prompt=prompt,
                    workspace_id=workspace_id,
                    log_source="recovery",
                )
        except AgentRunError as exc:
            agent_run_err = exc

        await self._commit_dirty_worktree(
            workspace_id=workspace_id,
            message=f"fix: address PR #{pr_number} CI failure",
        )

        if agent_run_err is not None:
            await self._handle_provider_agent_run_error(workspace_id, agent_run_err)
            _log.warning(
                "monitor.ci_fix_cli_failed",
                workspace_id=workspace_id,
                stderr=agent_run_err.result.stderr[:400],
            )
"""

content = content.replace(old_invoke.lstrip('\n'), new_invoke.lstrip('\n'))
content = content.replace(old_sync_base.lstrip('\n'), new_sync_base.lstrip('\n'))
content = content.replace(old_ci_fix.lstrip('\n'), new_ci_fix.lstrip('\n'))

with open("src/awf/runtime/pr_monitor_runner.py", "w") as f:
    f.write(content)

print("Replacement done.")
