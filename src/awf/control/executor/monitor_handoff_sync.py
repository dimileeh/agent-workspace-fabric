"""Sync-task PR-monitor handoff operations for ``WorkspaceExecutor``.

Mechanically extracted from ``awf.control.executor.monitor_handoff`` to keep
each module under the first-party line limit; behavior is unchanged. These
functions drive the ``sync_release_pr`` / ``sync_feature_pr`` task kinds and are
wired onto ``WorkspaceExecutor`` through ``mixins.py`` exactly as before.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from awf.common.audit import redact_audit_text
from awf.common.bitbucket_client import BitBucketClientError
from awf.common.forge import ForgeNotSupportedError, concrete_forge_for_repo, make_forge_client
from awf.common.github_client import (
    GitHubClientError,
    PullRequestMetadataError,
    RepoRef,
)
from awf.control.executor.constants import (
    _PR_ADOPTION_METADATA_MISSING_REASON_CODE,
    _PR_ADOPTION_SKIP_AGENT_REASON_CODE,
    _PR_MONITOR_ADOPTED_EVENT,
    _PR_MONITOR_ADOPTED_REASON_CODE,
    _RELEASE_SYNC_GITHUB_ERROR_REASON_CODE,
    _RELEASE_SYNC_NO_CHANGES_EVENT,
    _RELEASE_SYNC_REPO_INVALID_REASON_CODE,
)
from awf.control.executor.helpers import (
    _missing_sync_feature_pr_adoption_metadata,
    _release_sync_source_branch,
    _release_sync_target_branch,
    _required_metadata_str,
    _sync_feature_pr_adoption_metadata,
    _sync_feature_pr_missing_metadata_message,
    _with_release_sync_pr_metadata,
)
from awf.control.executor.metadata import _metadata_int
from awf.control.executor.monitor_handoff import (
    _gate_sync_handoff_unsupported_forge,
    _mark_monitor_unavailable_failed,
    _prepare_handoff_pr_monitor_profile,
)
from awf.control.executor.quality_gates import _log
from awf.db.enums import FailureReason, WorkspaceStatus
from awf.db.models import Workspace
from awf.db.repositories import WorkspaceRepository
from awf.runtime.release_pr_sync import (
    NO_CHANGES_REASON_CODE,
    ReleasePrSyncError,
    count_commits_ahead,
    ensure_release_sync_forge_supported,
    find_or_create_release_pr,
    release_pr_body,
    release_pr_title,
)


async def _handoff_sync_release_pr_monitor(
    self: Any,
    *,
    workspace_id: str,
    workspace: Workspace,
    compose_project: str,
    compose_file: Path,
    worktree_path: Path,
) -> None:
    """Open/reuse a source→target release PR, then monitor it (never auto-merge).

    No coding agent, no feature PR. When the source branch has no commits
    ahead of the target, the workspace completes cleanly without a PR.
    """
    if not await self._ensure_worktree_available(
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        expected=WorkspaceStatus.running,
        action="sync_release_pr_handoff",
    ):
        return

    # Re-gate the forge before the commits-ahead probe so an explicit
    # ``forge: bitbucket`` (with a github-detecting repo_url and no snapshot)
    # fails fast with FORGE_NOT_SUPPORTED instead of silently no-op completing
    # when nothing is ahead, or reaching the forge client only on the ahead path.
    if await _gate_sync_handoff_unsupported_forge(
        self,
        workspace_id=workspace_id,
        workspace=workspace,
        worktree_path=worktree_path,
    ):
        return

    source_branch = _release_sync_source_branch(workspace)
    target_branch = _release_sync_target_branch(workspace)
    try:
        repo = RepoRef.from_url(workspace.repo_url)
    except ValueError as exc:
        await self._mark_failed(
            workspace_id=workspace_id,
            from_status=WorkspaceStatus.running,
            failure_reason=FailureReason.infrastructure_failure,
            message=f"sync_release_pr cannot parse repo URL: {redact_audit_text(str(exc))}",
            reason_code=_RELEASE_SYNC_REPO_INVALID_REASON_CODE,
        )
        return

    # Release-PR sync is GitHub-only, but BitBucket is a *globally* supported forge
    # (issue #345 Part 2), so it clears ``_gate_sync_handoff_unsupported_forge``
    # above. Apply the release-sync-specific GitHub-only gate here — before the
    # commits-ahead probe and the no-op completion — so a BitBucket release sync
    # with zero commits ahead fails ``RELEASE_SYNC_FORGE_NOT_SUPPORTED`` instead of
    # silently completing as ``NO_CHANGES_TO_SYNC``. The concrete forge is read from
    # the snapshot ``_gate_sync_handoff_unsupported_forge`` just resolved+persisted
    # (mirroring ``unsupported_forge_error``); the same guard runs again before
    # ``make_forge_client`` below as defense-in-depth on the commits-ahead path.
    try:
        ensure_release_sync_forge_supported(
            concrete_forge_for_repo(
                (workspace.resolved_profile or {}).get("forge"),
                workspace.repo_url,
            ),
            repo_slug=repo.slug(),
            source_branch=source_branch,
            target_branch=target_branch,
        )
    except ReleasePrSyncError as exc:
        await self._mark_failed(
            workspace_id=workspace_id,
            from_status=WorkspaceStatus.running,
            failure_reason=FailureReason.infrastructure_failure,
            message=f"sync_release_pr failed: {exc.message}",
            reason_code=exc.reason_code,
            details=exc.detail,
        )
        return

    # Profile setup installs/repairs the monitor toolchain; source/target
    # divergence can still change while it runs, so re-count before PR adoption.
    try:
        commits_ahead = await count_commits_ahead(
            runner=self._runner,
            cwd=str(worktree_path),
            source_branch=source_branch,
            target_branch=target_branch,
        )
    except ReleasePrSyncError as exc:
        await self._mark_failed(
            workspace_id=workspace_id,
            from_status=WorkspaceStatus.running,
            failure_reason=FailureReason.infrastructure_failure,
            message=f"sync_release_pr failed: {exc.message}",
            reason_code=exc.reason_code,
            details=exc.detail,
        )
        return

    if commits_ahead <= 0:
        await self._complete_release_pr_sync_no_op(
            workspace_id=workspace_id,
            source_branch=source_branch,
            target_branch=target_branch,
        )
        return

    if self._pr_monitor is None and self._pr_monitor_factory is None:
        await _mark_monitor_unavailable_failed(
            self,
            workspace_id=workspace_id,
            message="release PR monitor handoff failed: no PR monitor configured",
        )
        return

    profile = await _prepare_handoff_pr_monitor_profile(
        self,
        workspace_id=workspace_id,
        workspace=workspace,
        worktree_path=worktree_path,
        compose_project=compose_project,
        compose_file=compose_file,
        build_failed_log_event="executor.sync_release_pr_monitor_build_failed",
        build_failed_message_prefix="release PR monitor handoff failed: ",
    )
    if profile is None:
        return

    if not await self._recheck_status(
        workspace_id,
        expected=WorkspaceStatus.running,
        action="sync_release_pr_handoff",
    ):
        return

    try:
        commits_ahead = await count_commits_ahead(
            runner=self._runner,
            cwd=str(worktree_path),
            source_branch=source_branch,
            target_branch=target_branch,
        )
    except ReleasePrSyncError as exc:
        await self._mark_failed(
            workspace_id=workspace_id,
            from_status=WorkspaceStatus.running,
            failure_reason=FailureReason.infrastructure_failure,
            message=f"sync_release_pr failed: {exc.message}",
            reason_code=exc.reason_code,
            details=exc.detail,
        )
        return

    if commits_ahead <= 0:
        await self._complete_release_pr_sync_no_op(
            workspace_id=workspace_id,
            source_branch=source_branch,
            target_branch=target_branch,
        )
        return

    try:
        # ``async with`` so the forge client is closed on every exit path. Unlike
        # the worker PR-monitor factory (whose client lives for the monitor's
        # lifetime and is closed by the runner), this client is used only for the
        # one-shot PR find/create below, so a BitBucket client would otherwise
        # leak its httpx connection pool on every release-sync handoff. The
        # construct-and-enter is inside the try so an unsupported forge / missing
        # BitBucket auth still maps to the reason-coded failures below.
        # Reconstructed forge (not re-resolved); unsupported forges fail fast.
        # Use concrete_forge_for_repo (not plain concrete_forge) to mirror the
        # worker PR-monitor factory and the execution_flow forge gate: a
        # legacy/missing snapshot normalizes profile.forge to "auto", so fall
        # back to the workspace repo_url's host. Without this, a BitBucket
        # workspace whose snapshot predates the forge field would silently
        # construct a GitHubClient here instead of failing fast.
        client_forge = concrete_forge_for_repo(profile.forge, workspace.repo_url)
        # Gate the *concrete client forge* before constructing the client: for a
        # BitBucket release-sync workspace, ``make_forge_client`` would call
        # ``BitBucketClient.from_env()`` and raise BITBUCKET_AUTH_NOT_CONFIGURED
        # when credentials are absent — masking the intended
        # RELEASE_SYNC_FORGE_NOT_SUPPORTED that release sync (GitHub-only) should
        # report. Failing here (caught by the ``ReleasePrSyncError`` handler
        # below) keeps the honest reason code and never builds a BitBucket client
        # for this unsupported path.
        ensure_release_sync_forge_supported(
            client_forge,
            repo_slug=repo.slug(),
            source_branch=source_branch,
            target_branch=target_branch,
        )
        async with make_forge_client(client_forge, self._runner) as gh:
            metadata, created = await find_or_create_release_pr(
                runner=self._runner,
                gh=gh,
                repo=repo,
                source_branch=source_branch,
                target_branch=target_branch,
                title=release_pr_title(source_branch=source_branch, target_branch=target_branch),
                body=release_pr_body(source_branch=source_branch, target_branch=target_branch),
            )
    except (ReleasePrSyncError, PullRequestMetadataError) as exc:
        await self._mark_failed(
            workspace_id=workspace_id,
            from_status=WorkspaceStatus.running,
            failure_reason=FailureReason.infrastructure_failure,
            message=f"sync_release_pr failed: {exc.message}",
            reason_code=exc.reason_code,
            details=exc.detail,
        )
        return
    except ForgeNotSupportedError as exc:
        # Defense-in-depth: ``make_forge_client`` is constructed inside this
        # try-block, so an unsupported forge that slips past the early
        # execution_flow gate fails cleanly with FORGE_NOT_SUPPORTED instead of
        # propagating uncaught and stranding the workspace in ``running``.
        await self._mark_failed(
            workspace_id=workspace_id,
            from_status=WorkspaceStatus.running,
            failure_reason=FailureReason.infrastructure_failure,
            message=f"sync_release_pr failed: {exc.message}",
            reason_code=exc.reason_code,
        )
        return
    except BitBucketClientError as exc:
        # Defense-in-depth: ``ensure_release_sync_forge_supported`` above raises
        # ``ReleasePrSyncError`` for every non-GitHub forge, so today
        # ``client_forge`` is always ``"github"`` here and ``make_forge_client``
        # only ever builds a ``GitHubClient`` — this handler is currently
        # unreachable. It is kept deliberately, mirroring the
        # ForgeNotSupportedError handler above: if that release-sync gate is later
        # widened to allow BitBucket, ``BitBucketClient.from_env()`` (e.g.
        # BITBUCKET_AUTH_NOT_CONFIGURED on missing credentials) must map to a
        # reason-coded failure here instead of escaping uncaught and stranding the
        # workspace in ``running``.
        safe_exception = redact_audit_text(repr(exc), limit=1900)
        await self._mark_failed(
            workspace_id=workspace_id,
            from_status=WorkspaceStatus.running,
            failure_reason=FailureReason.infrastructure_failure,
            message=f"sync_release_pr failed: {safe_exception}"[:2000],
            reason_code=exc.reason_code,
        )
        return
    except GitHubClientError as exc:
        await self._mark_failed(
            workspace_id=workspace_id,
            from_status=WorkspaceStatus.running,
            failure_reason=FailureReason.infrastructure_failure,
            message=f"sync_release_pr GitHub error ({exc.operation}): {exc.stderr or str(exc)}",
            reason_code=_RELEASE_SYNC_GITHUB_ERROR_REASON_CODE,
        )
        return

    _log.info(
        "release_pr_sync.pr_ready",
        repo=repo.slug(),
        source_branch=source_branch,
        target_branch=target_branch,
        pr_number=metadata.number,
        created=created,
        commits_ahead=commits_ahead,
    )

    monitor = await self._build_handoff_pr_monitor(
        workspace_id=workspace_id,
        workspace=workspace,
        worktree_path=worktree_path,
        compose_project=compose_project,
        compose_file=compose_file,
        build_failed_log_event="executor.sync_release_pr_monitor_build_failed",
        build_failed_message_prefix="release PR monitor handoff failed: ",
        profile=profile,
        run_profile_setup=False,
        stale_action="sync_release_pr_monitor_build",
    )
    if monitor is None:
        return

    async with self._session_factory() as session:
        repo_db = WorkspaceRepository(session)
        persisted = await repo_db.get(workspace_id)
        if persisted is None:  # pragma: no cover - destroyed mid-flight
            return
        if persisted.status != WorkspaceStatus.running.value:
            await self._record_stale_action_skip(
                repo_db,
                persisted,
                action="sync_release_pr_handoff",
                expected=WorkspaceStatus.running,
                reason_code="EXECUTOR_STALE_STATUS",
            )
            await session.commit()
            return

        persisted.pr_url = metadata.url
        persisted.pr_number = metadata.number
        persisted.remote_push_branch = source_branch
        persisted.monitor_last_commit_sha = metadata.head_sha
        persisted.base_commit = metadata.base_sha
        persisted.task_policy = _with_release_sync_pr_metadata(
            persisted.task_policy,
            metadata=metadata,
            created=created,
        )
        await repo_db.add_event(
            persisted,
            event_type=_PR_MONITOR_ADOPTED_EVENT,
            reason_code=_PR_MONITOR_ADOPTED_REASON_CODE,
            payload={
                "pr_number": metadata.number,
                "pr_url": metadata.url,
                "head_ref": metadata.head_ref,
                "base_ref": metadata.base_ref,
                "head_sha": metadata.head_sha,
                "base_sha": metadata.base_sha,
                "source_branch": source_branch,
                "target_branch": target_branch,
                "created": created,
                "source": "release_pr_sync",
            },
        )
        await repo_db.transition(
            persisted,
            to=WorkspaceStatus.validating,
            reason_code=_PR_ADOPTION_SKIP_AGENT_REASON_CODE,
            payload={"source": "release_pr_sync"},
        )
        await repo_db.transition(
            persisted,
            to=WorkspaceStatus.monitoring_pr,
            reason_code=_PR_MONITOR_ADOPTED_REASON_CODE,
            payload={
                "pr_number": metadata.number,
                "pr_url": metadata.url,
                "head_sha": metadata.head_sha,
                "base_sha": metadata.base_sha,
                "source": "release_pr_sync",
            },
        )
        await session.commit()

    _log.info(
        "executor.sync_release_pr_handoff_to_monitor",
        workspace_id=workspace_id,
        pr_url=metadata.url,
        created=created,
    )
    if not await self._recheck_status(
        workspace_id,
        expected=WorkspaceStatus.monitoring_pr,
        action="run_pr_monitor",
    ):
        return
    await monitor.run(
        workspace_id=workspace_id,
        compose_project=compose_project,
        compose_file=compose_file,
    )


async def _complete_release_pr_sync_no_op(
    self: Any,
    *,
    workspace_id: str,
    source_branch: str,
    target_branch: str,
) -> None:
    """Complete a ``sync_release_pr`` workspace that has nothing to sync."""
    async with self._session_factory() as session:
        repo_db = WorkspaceRepository(session)
        persisted = await repo_db.get(workspace_id)
        if persisted is None:  # pragma: no cover - destroyed mid-flight
            return
        if persisted.status != WorkspaceStatus.running.value:
            await self._record_stale_action_skip(
                repo_db,
                persisted,
                action="sync_release_pr_handoff",
                expected=WorkspaceStatus.running,
                reason_code="EXECUTOR_STALE_STATUS",
            )
            await session.commit()
            return
        await repo_db.add_event(
            persisted,
            event_type=_RELEASE_SYNC_NO_CHANGES_EVENT,
            reason_code=NO_CHANGES_REASON_CODE,
            payload={"source_branch": source_branch, "target_branch": target_branch},
        )
        await repo_db.transition(
            persisted,
            to=WorkspaceStatus.validating,
            reason_code=NO_CHANGES_REASON_CODE,
            payload={"source": "release_pr_sync"},
        )
        await repo_db.transition(
            persisted,
            to=WorkspaceStatus.completed,
            reason_code=NO_CHANGES_REASON_CODE,
            payload={
                "source_branch": source_branch,
                "target_branch": target_branch,
                "source": "release_pr_sync",
            },
        )
        await session.commit()
    _log.info(
        "executor.sync_release_pr_no_changes",
        workspace_id=workspace_id,
        source_branch=source_branch,
        target_branch=target_branch,
    )


async def _handoff_sync_feature_pr_monitor(
    self: Any,
    *,
    workspace_id: str,
    workspace: Workspace,
    compose_project: str,
    compose_file: Path,
    worktree_path: Path,
) -> None:
    metadata = _sync_feature_pr_adoption_metadata(workspace)
    missing = _missing_sync_feature_pr_adoption_metadata(workspace, metadata)
    if missing:
        await self._mark_failed(
            workspace_id=workspace_id,
            from_status=WorkspaceStatus.running,
            failure_reason=FailureReason.infrastructure_failure,
            message=_sync_feature_pr_missing_metadata_message(missing),
            reason_code=_PR_ADOPTION_METADATA_MISSING_REASON_CODE,
            details={"missing": missing},
        )
        return

    if not await self._ensure_worktree_available(
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        expected=WorkspaceStatus.running,
        action="sync_feature_pr_handoff",
    ):
        return

    # Re-gate the forge before building the monitor so an explicit
    # ``forge: bitbucket`` (with a github-detecting repo_url and no snapshot)
    # fails fast with FORGE_NOT_SUPPORTED instead of being flattened into
    # MONITOR_UNAVAILABLE when the factory's forge-client construction raises.
    if await _gate_sync_handoff_unsupported_forge(
        self,
        workspace_id=workspace_id,
        workspace=workspace,
        worktree_path=worktree_path,
    ):
        return

    monitor = await self._build_handoff_pr_monitor(
        workspace_id=workspace_id,
        workspace=workspace,
        worktree_path=worktree_path,
        compose_project=compose_project,
        compose_file=compose_file,
        build_failed_log_event="executor.sync_feature_pr_monitor_build_failed",
        build_failed_message_prefix="adopted PR monitor handoff failed: ",
        stale_action="sync_feature_pr_handoff",
    )
    if monitor is None:
        return

    async with self._session_factory() as session:
        repo = WorkspaceRepository(session)
        persisted = await repo.get(workspace_id)
        if persisted is None:  # pragma: no cover - destroyed mid-flight
            return
        if persisted.status != WorkspaceStatus.running.value:
            await self._record_stale_action_skip(
                repo,
                persisted,
                action="sync_feature_pr_handoff",
                expected=WorkspaceStatus.running,
                reason_code="EXECUTOR_STALE_STATUS",
            )
            await session.commit()
            return

        persisted_metadata = _sync_feature_pr_adoption_metadata(persisted)
        missing = _missing_sync_feature_pr_adoption_metadata(
            persisted,
            persisted_metadata,
        )
        if missing:
            safe_message = redact_audit_text(
                _sync_feature_pr_missing_metadata_message(missing),
                limit=2000,
            )
            persisted.failure_reason = FailureReason.infrastructure_failure.value
            persisted.failure_message = safe_message
            await repo.transition(
                persisted,
                to=WorkspaceStatus.failed,
                reason_code=_PR_ADOPTION_METADATA_MISSING_REASON_CODE,
                payload={
                    "failure_reason": FailureReason.infrastructure_failure.value,
                    "reason_code": _PR_ADOPTION_METADATA_MISSING_REASON_CODE,
                    "message": safe_message,
                    "details": {"missing": missing},
                },
            )
            await session.commit()
            return

        head_sha = _required_metadata_str(persisted_metadata, "head_sha")
        base_sha = _required_metadata_str(persisted_metadata, "base_sha")
        head_ref = _required_metadata_str(persisted_metadata, "head_ref")
        base_ref = _required_metadata_str(persisted_metadata, "base_ref")
        pr_url = persisted.pr_url or _required_metadata_str(
            persisted_metadata,
            "pr_url",
        )
        pr_number = persisted.pr_number or _metadata_int(
            persisted_metadata,
            "pr_number",
        )
        remote_branch = persisted.remote_push_branch or head_ref

        persisted.pr_url = pr_url
        persisted.pr_number = pr_number
        persisted.remote_push_branch = remote_branch
        persisted.monitor_last_commit_sha = head_sha
        persisted.base_commit = base_sha
        await repo.add_event(
            persisted,
            event_type=_PR_MONITOR_ADOPTED_EVENT,
            reason_code=_PR_MONITOR_ADOPTED_REASON_CODE,
            payload={
                "pr_number": pr_number,
                "pr_url": pr_url,
                "head_ref": head_ref,
                "base_ref": base_ref,
                "head_sha": head_sha,
                "base_sha": base_sha,
                "remote_branch": remote_branch,
                "source": "existing_github_pr",
            },
        )
        await repo.transition(
            persisted,
            to=WorkspaceStatus.validating,
            reason_code=_PR_ADOPTION_SKIP_AGENT_REASON_CODE,
            payload={"source": "existing_github_pr"},
        )
        await repo.transition(
            persisted,
            to=WorkspaceStatus.monitoring_pr,
            reason_code=_PR_MONITOR_ADOPTED_REASON_CODE,
            payload={
                "pr_number": pr_number,
                "pr_url": pr_url,
                "head_sha": head_sha,
                "base_sha": base_sha,
                "source": "existing_github_pr",
            },
        )
        await session.commit()

    _log.info(
        "executor.sync_feature_pr_handoff_to_monitor",
        workspace_id=workspace_id,
        pr_url=workspace.pr_url,
    )
    if not await self._recheck_status(
        workspace_id,
        expected=WorkspaceStatus.monitoring_pr,
        action="run_pr_monitor",
    ):
        return
    await monitor.run(
        workspace_id=workspace_id,
        compose_project=compose_project,
        compose_file=compose_file,
    )
