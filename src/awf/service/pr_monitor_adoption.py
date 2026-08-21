"""First-class adoption of existing GitHub PRs into AWF monitoring."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from awf.api.schemas import (
    PullRequestMonitorAdoptionRequest,
    PullRequestMonitorAdoptionResponse,
)
from awf.common.auto_merge import (
    auto_merge_intent_from_policy,
    auto_merge_is_resolved,
    seed_auto_merge,
)
from awf.common.config import Settings, get_settings
from awf.common.github_client import (
    PullRequestAdoptionMetadata,
    PullRequestMetadataError,
    RepoRef,
)
from awf.common.workspace_policy import pr_adoption_execution_policy
from awf.db.enums import OperationStatus, OperationType
from awf.db.models import MergeCandidate, Task, Workspace
from awf.db.repositories import (
    MergeCandidateRepository,
    OperationRepository,
    QueueDecisionRepository,
    ResourceReservationRepository,
    TaskAttemptRepository,
    TaskExternalIdConflictError,
    TaskRepository,
    ValidationRunRepository,
    WorkspaceRepository,
)
from awf.service.node_identity import effective_worker_node_id
from awf.service.pr_monitor_adoption_helpers import *  # noqa: F403
from awf.service.pr_monitor_adoption_helpers import (  # noqa: F401
    _SUPERSEDED_EXTERNAL_ID_ALLOCATION_ATTEMPTS,
    PR_ADOPTION_ADMITTED_REASON,
    PR_ADOPTION_OPERATION_ACTION,
    PR_ADOPTION_REQUESTED_EVENT_TYPE,
    PR_ADOPTION_REQUESTED_REASON,
    PR_ADOPTION_SUPERSEDED_EVENT_TYPE,
    PR_ADOPTION_SUPERSEDED_REASON,
    PR_ADOPTION_TASK_KIND,
    PRMonitorAdoptionError,
    _adoption_external_id,
    _adoption_generation_external_id,
    _adoption_lineage_payload,
    _adoption_owns_task_identity,
    _adoption_policy,
    _adoption_repo_url,
    _adoption_task_policy,
    _adoption_task_prompt,
    _adoption_workspace_is_resumable,
    _allocate_superseded_adoption_idempotency_key,
    _cursor_auto_mode_provider_preflight,
    _default_metadata_fetcher,
    _effective_adoption_external_id,
    _github_repo_url_like,
    _log,
    _metadata_error_status_code,
    _next_adoption_task_idempotency_key,
    _normalize_request_identity,
    _optional_str,
    _raise_if_adoption_forge_mismatch,
    _raise_if_existing_workspace_is_not_requested_adoption,
    _raise_if_hosted_delegation_unconfigured,
    _raise_if_policy_conflicts,
    _raise_if_unsupported_agent,
    _redacted_optional_text,
    _release_superseded_adoption_external_id,
    _requested_execution_policy,
    _requested_inline_profile_policy,
    _select_live_adoption_workspace,
    _task_external_id_conflict_error,
    _task_has_existing_attempt,
    _task_has_shared_ownership_attempt,
    _terminal_adoption_lineage,
    _workspace_status_for_response,
    pr_adoption_idempotency_key,
)
from awf.service.scheduler import scheduler_score_from_workspace
from awf.service.validation_observability import validation_freshness_summary


class PullRequestMonitorAdoptionService:
    """Shared domain service for REST, CLI-backed scripts, and MCP."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        metadata_fetcher: Callable[..., Awaitable[PullRequestAdoptionMetadata]] | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._session = session
        self._settings = settings or get_settings()
        self._metadata_fetcher = metadata_fetcher or _default_metadata_fetcher

    async def adopt(
        self,
        request: PullRequestMonitorAdoptionRequest,
    ) -> PullRequestMonitorAdoptionResponse:
        repo, pr_number = _normalize_request_identity(request)
        _raise_if_hosted_delegation_unconfigured(request, self._settings)
        idempotency_key = pr_adoption_idempotency_key(
            repo_slug=repo.slug(),
            pr_number=pr_number,
        )
        # History lookup stays on the deterministic generated adoption id even when
        # the caller supplies an explicit external_id (policy/persistence only).
        history_external_id = _adoption_external_id(repo_slug=repo.slug(), pr_number=pr_number)
        effective_external_id = _effective_adoption_external_id(
            request,
            repo_slug=repo.slug(),
            pr_number=pr_number,
        )
        workspace_repo = WorkspaceRepository(self._session)
        await workspace_repo.acquire_idempotency_key_lock(idempotency_key)
        # Re-read under the transaction lock so a concurrent adopter that won
        # the race attaches here instead of surfacing the unique constraint.
        adoption_history = await workspace_repo.list_pr_adoption_history(
            task_external_id=history_external_id,
            idempotency_key=idempotency_key,
            task_kind=PR_ADOPTION_TASK_KIND,
            repo_slug=repo.slug(),
            pr_number=pr_number,
        )
        live_adoption = _select_live_adoption_workspace(adoption_history)
        if live_adoption is not None:
            _raise_if_adoption_forge_mismatch(live_adoption, repo=repo)
            _raise_if_policy_conflicts(live_adoption, request, repo=repo, pr_number=pr_number)
            return await self._response(live_adoption, attached_existing=True)

        existing = await workspace_repo.get_by_idempotency_key(idempotency_key)
        if existing is not None:
            _raise_if_adoption_forge_mismatch(existing, repo=repo)
            _raise_if_existing_workspace_is_not_requested_adoption(
                existing,
                repo=repo,
                pr_number=pr_number,
            )
            if _adoption_workspace_is_resumable(existing):
                _raise_if_policy_conflicts(existing, request, repo=repo, pr_number=pr_number)
                return await self._response(existing, attached_existing=True)

        _raise_if_unsupported_agent(request)
        provider_readiness_preflight = await _cursor_auto_mode_provider_preflight(
            self._settings,
            request,
        )

        metadata = await self._fetch_metadata(repo=repo, pr_number=pr_number)
        previous_terminal_adoptions = await _terminal_adoption_lineage(
            self._session,
            adoption_history,
        )
        # Mutating supersede + create must be atomic: a domain conflict after
        # partial writes must not survive commit (REST JSONResponse / MCP). Use a
        # savepoint so the outer advisory lock stays held until the request txn ends.
        try:
            async with self._session.begin_nested():
                superseded_adoption: dict[str, Any] | None = None
                superseded_workspace: Workspace | None = None
                if existing is not None:
                    superseded_adoption = await self._supersede_previous_adoption(
                        workspace=existing,
                        idempotency_key=idempotency_key,
                        repo=repo,
                        pr_number=pr_number,
                    )
                    superseded_workspace = existing
                workspace = await self._create_adoption_workspace(
                    request=request,
                    repo=repo,
                    metadata=metadata,
                    idempotency_key=idempotency_key,
                    logical_idempotency_key=idempotency_key,
                    previous_terminal_adoptions=previous_terminal_adoptions,
                    superseded_adoption=superseded_adoption,
                    superseded_workspace=superseded_workspace,
                    effective_external_id=effective_external_id,
                    provider_readiness_preflight=provider_readiness_preflight,
                )
        except TaskExternalIdConflictError as exc:
            raise _task_external_id_conflict_error(exc) from exc
        return await self._response(workspace, attached_existing=False)

    async def _supersede_previous_adoption(
        self,
        *,
        workspace: Workspace,
        idempotency_key: str,
        repo: RepoRef,
        pr_number: int,
    ) -> dict[str, Any]:
        previous_idempotency_key = workspace.idempotency_key
        workspace_id = workspace.id
        previous_status = workspace.status
        # Resolve ownership before rewriting identity. Assigning a colliding
        # superseded idempotency key before these SELECTs would autoflush into
        # uq_workspaces_idempotency_key outside the savepoint retry loop.
        previous_workspace_external_id = workspace.task_external_id
        owned_task: Task | None = None
        previous_task_external_id: str | None = None
        previous_task_idempotency_key: str | None = None
        attempt = await TaskAttemptRepository(self._session).get_by_workspace_id(workspace_id)
        if attempt is not None:
            task = await TaskRepository(self._session).get(attempt.task_id)
            if task is not None and await _adoption_owns_task_identity(
                self._session,
                task,
                adoption_idempotency_key=idempotency_key,
                workspace_id=workspace_id,
            ):
                # Only rewrite identity on adoption-owned tasks. A joined
                # same-scope source task keeps its external_id / key so prior
                # attempts and future lookups stay intact — including when
                # reuse stamped a null source key with the adoption key.
                owned_task = task
                previous_task_external_id = task.external_id
                previous_task_idempotency_key = task.idempotency_key

        # Probe + savepoint-retry both idempotency and external_id namespaces so
        # an occupied (or concurrently claimed) preferred superseded slot does
        # not raise an unhandled IntegrityError on flush and wedge re-adoptions.
        last_integrity_error: IntegrityError | None = None
        superseded_idempotency_key: str | None = None
        for _ in range(_SUPERSEDED_EXTERNAL_ID_ALLOCATION_ATTEMPTS):
            try:
                # Nested savepoint: recover unique-constraint races the same way
                # TaskRepository.create_or_get does, without poisoning the outer
                # adoption savepoint / advisory-lock transaction.
                async with self._session.begin_nested():
                    superseded_idempotency_key = (
                        await _allocate_superseded_adoption_idempotency_key(
                            self._session,
                            idempotency_key=idempotency_key,
                            workspace_id=workspace_id,
                        )
                    )
                    workspace.idempotency_key = superseded_idempotency_key
                    if owned_task is not None:
                        owned_task.idempotency_key = superseded_idempotency_key
                    # Matching prior IDs must share one allocation. Separate
                    # occupancy probes can see preferred free then occupied
                    # between awaits; workspace.task_external_id is not under
                    # uq_tasks_external_id, so flush would keep divergent slots.
                    if (
                        owned_task is not None
                        and previous_task_external_id == previous_workspace_external_id
                    ):
                        released_external_id = await _release_superseded_adoption_external_id(
                            self._session,
                            previous_workspace_external_id,
                            workspace_id=workspace_id,
                        )
                        workspace.task_external_id = released_external_id
                        owned_task.external_id = released_external_id
                    else:
                        workspace.task_external_id = await _release_superseded_adoption_external_id(
                            self._session,
                            previous_workspace_external_id,
                            workspace_id=workspace_id,
                        )
                        if owned_task is not None:
                            owned_task.external_id = await _release_superseded_adoption_external_id(
                                self._session,
                                previous_task_external_id,
                                workspace_id=workspace_id,
                            )
                    await WorkspaceRepository(self._session).advance_workspace_version(workspace)
                    await self._session.flush()
                break
            except IntegrityError as exc:
                # Savepoint rollback undoes the DB write, but ORM attributes still
                # hold the colliding candidate. Revert before the next probe so a
                # SELECT autoflush cannot re-emit the conflict outside the savepoint.
                workspace.idempotency_key = previous_idempotency_key
                workspace.task_external_id = previous_workspace_external_id
                if owned_task is not None:
                    owned_task.idempotency_key = previous_task_idempotency_key
                    owned_task.external_id = previous_task_external_id
                last_integrity_error = exc
                continue
        else:
            raise PRMonitorAdoptionError(
                error_code="TASK_EXTERNAL_ID_CONFLICT",
                message=(
                    "Unable to allocate a free superseded identity slot; "
                    "retry after clearing colliding idempotency keys or "
                    "external task IDs."
                ),
                detail={"workspace_id": workspace_id},
            ) from last_integrity_error

        return {
            "reason_code": PR_ADOPTION_SUPERSEDED_REASON,
            "repo_slug": repo.slug(),
            "pr_number": pr_number,
            "previous_workspace_id": workspace_id,
            "previous_status": previous_status,
            "previous_idempotency_key": previous_idempotency_key,
            "superseded_idempotency_key": superseded_idempotency_key,
        }

    async def _fetch_metadata(
        self,
        *,
        repo: RepoRef,
        pr_number: int,
    ) -> PullRequestAdoptionMetadata:
        try:
            metadata = await self._metadata_fetcher(repo=repo, pr_number=pr_number)
        except PullRequestMetadataError as exc:
            raise PRMonitorAdoptionError(
                error_code=exc.reason_code,
                message=exc.message,
                status_code=_metadata_error_status_code(exc.reason_code),
                detail=exc.detail,
            ) from exc
        if metadata.merged:
            raise PRMonitorAdoptionError(
                error_code="PR_ALREADY_MERGED",
                message=f"PR {repo.slug()}#{pr_number} is already merged.",
                detail={
                    "repo_slug": repo.slug(),
                    "pr_number": pr_number,
                    "state": metadata.state,
                },
            )
        if metadata.closed:
            raise PRMonitorAdoptionError(
                error_code="PR_ALREADY_CLOSED",
                message=f"PR {repo.slug()}#{pr_number} is closed.",
                detail={
                    "repo_slug": repo.slug(),
                    "pr_number": pr_number,
                    "state": metadata.state,
                },
            )
        return metadata

    async def _create_adoption_workspace(
        self,
        *,
        request: PullRequestMonitorAdoptionRequest,
        repo: RepoRef,
        metadata: PullRequestAdoptionMetadata,
        idempotency_key: str,
        logical_idempotency_key: str,
        previous_terminal_adoptions: list[dict[str, str | None]],
        superseded_adoption: dict[str, Any] | None = None,
        superseded_workspace: Workspace | None = None,
        effective_external_id: str,
        provider_readiness_preflight: Mapping[str, Any] | None = None,
    ) -> Workspace:
        requested_profile = _requested_inline_profile_policy(request)
        repo_url = _adoption_repo_url(request=request, repo=repo)
        task_policy = _adoption_task_policy(
            repo=repo,
            metadata=metadata,
            request=request,
            repo_url=repo_url,
            lineage=_adoption_lineage_payload(
                logical_idempotency_key=logical_idempotency_key,
                previous_terminal_adoptions=previous_terminal_adoptions,
            ),
        )
        if provider_readiness_preflight is not None:
            task_policy["provider_readiness_preflight"] = dict(provider_readiness_preflight)
        operator_reason = _redacted_optional_text(request.reason)
        task_class = request.task_class.value if request.task_class is not None else None
        explicit_external_id = request.external_id is not None
        workspace_repo = WorkspaceRepository(self._session)
        workspace = await workspace_repo.create(
            repo_url=repo_url,
            branch_base=metadata.base_ref,
            task_title=(
                request.task_title
                or metadata.title
                or f"PR monitor adoption: {repo.slug()}#{metadata.number}"
            ),
            task_prompt=request.task_prompt or _adoption_task_prompt(repo=repo, metadata=metadata),
            task_external_id=effective_external_id,
            task_class=task_class,
            task_tag=request.task_tag,
            agent=request.agent.value,
            test_commands=[],
            requires_database=False,
            owned_paths=list(request.owned_paths),
            task_policy=task_policy,
            auto_merge=seed_auto_merge(request.auto_merge),
            initial_review_grace_period_seconds=request.initial_review_grace_period_seconds,
            profile_ref=request.profile_ref,
            requested_profile=requested_profile,
            idempotency_key=idempotency_key,
            task_kind=PR_ADOPTION_TASK_KIND,
            remote_push_branch=metadata.head_ref,
        )
        workspace.pr_url = metadata.url
        workspace.pr_number = metadata.number
        workspace.base_commit = metadata.base_sha
        workspace.monitor_last_commit_sha = metadata.head_sha
        superseded_payload = (
            {
                **superseded_adoption,
                "replacement_workspace_id": workspace.id,
            }
            if superseded_adoption is not None
            else None
        )

        task_repo = TaskRepository(self._session)

        async def _create_or_get_task(
            *,
            external_id: str | None,
            task_idempotency_key: str | None,
        ) -> Task:
            return await task_repo.create_or_get(
                repo_url=workspace.repo_url,
                base_branch=workspace.branch_base,
                title=workspace.task_title,
                prompt=workspace.task_prompt,
                external_id=external_id,
                idempotency_key=task_idempotency_key,
                task_class=workspace.task_class,
                owned_paths=list(workspace.owned_paths),
            )

        async def _create_generated_task() -> Task:
            task_external_id = workspace.task_external_id or _adoption_external_id(
                repo_slug=repo.slug(),
                pr_number=metadata.number,
            )
            task_generation_idempotency_key = await _next_adoption_task_idempotency_key(
                self._session,
                logical_idempotency_key=logical_idempotency_key,
                task_external_id=task_external_id,
            )
            workspace.task_external_id = _adoption_generation_external_id(
                repo_slug=repo.slug(),
                pr_number=metadata.number,
                logical_idempotency_key=logical_idempotency_key,
                workspace_idempotency_key=task_generation_idempotency_key,
            )
            return await _create_or_get_task(
                external_id=workspace.task_external_id,
                task_idempotency_key=task_generation_idempotency_key,
            )

        try:
            task = await _create_or_get_task(
                external_id=workspace.task_external_id,
                task_idempotency_key=idempotency_key,
            )
        except TaskExternalIdConflictError:
            if explicit_external_id or not previous_terminal_adoptions:
                raise
            task = await _create_generated_task()
        else:
            terminal_workspace_ids = {
                workspace_id
                for entry in previous_terminal_adoptions
                if (workspace_id := entry.get("workspace_id")) is not None
            }
            if (
                previous_terminal_adoptions
                and task.idempotency_key == logical_idempotency_key
                and await _task_has_existing_attempt(self._session, task.id)
                # Shared ownership (peer adoption or joined same-scope source)
                # preserves the logical key on supersession; rejoin that row
                # instead of treating it as an owned generation slot.
                and not await _task_has_shared_ownership_attempt(
                    self._session,
                    task.id,
                    terminal_workspace_ids=terminal_workspace_ids,
                )
            ):
                if explicit_external_id:
                    raise TaskExternalIdConflictError(effective_external_id)
                task = await _create_generated_task()
        attempt = await TaskAttemptRepository(self._session).create_for_workspace(
            task=task,
            workspace=workspace,
        )
        node_id = effective_worker_node_id(self._settings)
        hosted_execution = request.execution.mode == "hosted"
        steady_cpu = 0.0 if hosted_execution else self._settings.workspace_steady_cpu
        steady_memory_gb = 0.0 if hosted_execution else self._settings.workspace_steady_memory_gb
        peak_cpu = 0.0 if hosted_execution else self._settings.workspace_peak_cpu
        peak_memory_gb = 0.0 if hosted_execution else self._settings.workspace_peak_memory_gb
        resource_summary: dict[str, Any] = {
            "node_id": node_id,
            "steady_cpu": steady_cpu,
            "steady_memory_gb": steady_memory_gb,
            "peak_cpu": peak_cpu,
            "peak_memory_gb": peak_memory_gb,
            "disk_mb": None,
            "dind_slots": 0,
            "phase": "workspace_lifecycle",
            "dind_mode": "none",
        }
        await ResourceReservationRepository(self._session).create(
            workspace_id=workspace.id,
            attempt_id=attempt.id,
            node_id=node_id,
            steady_cpu=steady_cpu,
            steady_memory_gb=steady_memory_gb,
            peak_cpu=peak_cpu,
            peak_memory_gb=peak_memory_gb,
            disk_mb=None,
            dind_slots=0,
            phase="workspace_lifecycle",
        )
        scheduler_score = scheduler_score_from_workspace(workspace, now=workspace.created_at)
        await QueueDecisionRepository(self._session).create(
            workspace_id=workspace.id,
            task_id=task.id,
            attempt_id=attempt.id,
            decision="admitted",
            reason_code=PR_ADOPTION_ADMITTED_REASON,
            class_priority=scheduler_score.class_priority,
            computed_priority=scheduler_score.effective_score,
            age_boost=scheduler_score.age_boost,
            retry_bonus=scheduler_score.retry_bonus,
            resource_summary=resource_summary,
            overlap_risk_summary={"count": 0, "overlaps": []},
            score_summary=scheduler_score.score_summary,
        )
        operation_payload: dict[str, Any] = {
            "action": PR_ADOPTION_OPERATION_ACTION,
            "repo_slug": repo.slug(),
            "pr_number": metadata.number,
            "pr_url": metadata.url,
            "auto_merge": request.auto_merge,
            "execution": _requested_execution_policy(request),
            "reason": operator_reason,
            "logical_idempotency_key": logical_idempotency_key,
            "workspace_idempotency_key": idempotency_key,
            "previous_terminal_adoptions": previous_terminal_adoptions,
        }
        if superseded_payload is not None:
            operation_payload["superseded_adoption"] = superseded_payload
        operation = await OperationRepository(self._session).create(
            workspace_id=workspace.id,
            operation_type=OperationType.adopt_pr,
            status=OperationStatus.succeeded,
            idempotency_key=idempotency_key,
            payload=operation_payload,
        )
        event_payload: dict[str, Any] = {
            "operation_id": operation.id,
            "repo_slug": repo.slug(),
            "pr_number": metadata.number,
            "pr_url": metadata.url,
            "head_ref": metadata.head_ref,
            "head_repo_slug": metadata.head_repo_slug,
            "head_repo_url": _github_repo_url_like(repo_url, metadata.head_repo_slug),
            "base_ref": metadata.base_ref,
            "head_sha": metadata.head_sha,
            "base_sha": metadata.base_sha,
            "auto_merge": request.auto_merge,
            "execution": _requested_execution_policy(request),
            "reason": operator_reason,
            "logical_idempotency_key": logical_idempotency_key,
            "workspace_idempotency_key": idempotency_key,
            "previous_terminal_adoptions": previous_terminal_adoptions,
        }
        if superseded_payload is not None:
            event_payload["superseded_adoption"] = superseded_payload
        await workspace_repo.add_event(
            workspace,
            event_type=PR_ADOPTION_REQUESTED_EVENT_TYPE,
            reason_code=PR_ADOPTION_REQUESTED_REASON,
            payload=event_payload,
        )
        if superseded_workspace is not None and superseded_payload is not None:
            await workspace_repo.add_event(
                superseded_workspace,
                event_type=PR_ADOPTION_SUPERSEDED_EVENT_TYPE,
                reason_code=PR_ADOPTION_SUPERSEDED_REASON,
                payload=superseded_payload,
            )
            _log.info(
                "pr_monitor_adoption.superseded_terminal_workspace",
                **superseded_payload,
            )
        await self._session.flush()
        return workspace

    async def _response(
        self,
        workspace: Workspace,
        *,
        attached_existing: bool,
    ) -> PullRequestMonitorAdoptionResponse:
        attempt = await TaskAttemptRepository(self._session).get_by_workspace_id(workspace.id)
        candidate: MergeCandidate | None = None
        if attempt is not None:
            candidate = await MergeCandidateRepository(self._session).get_by_attempt_id(attempt.id)
        validation_runs = await ValidationRunRepository(self._session).list_for_workspace(
            workspace.id
        )
        adoption = _adoption_policy(workspace)
        repo_slug = str(adoption.get("repo_slug") or RepoRef.from_url(workspace.repo_url).slug())
        pr_number = int(adoption.get("pr_number") or workspace.pr_number or 0)
        pr_url = str(adoption.get("pr_url") or workspace.pr_url or "")
        # The persisted ``auto_merge`` column is only authoritative once the
        # provisioner has resolved it against the materialized profile; before then
        # it is the provisional seed, which lies for an adoption that omitted an
        # explicit intent and whose (trusted) profile resolves ``monitor.auto_merge``
        # on. ``auto_merge_is_resolved`` owns that rule (shared with the workspace
        # GET/list projection), so report ``None`` rather than a false ``manual``
        # policy while the setting is still unresolved.
        auto_merge_intent = auto_merge_intent_from_policy(workspace.task_policy)
        auto_merge_resolved = auto_merge_is_resolved(workspace.status, workspace.task_policy)
        auto_merge_value: bool | None = workspace.auto_merge if auto_merge_resolved else None
        return PullRequestMonitorAdoptionResponse(
            workspace_id=workspace.id,
            status=_workspace_status_for_response(workspace.status),
            version=workspace.version,
            task_id=attempt.task_id if attempt is not None else None,
            attempt_id=attempt.id if attempt is not None else None,
            candidate_id=candidate.id if candidate is not None else None,
            repo_slug=repo_slug,
            repo_url=workspace.repo_url,
            pr_number=pr_number,
            pr_url=pr_url,
            head_ref=str(adoption.get("head_ref") or workspace.remote_push_branch or ""),
            base_ref=str(adoption.get("base_ref") or workspace.branch_base),
            head_sha=_optional_str(adoption.get("head_sha")) or workspace.monitor_last_commit_sha,
            base_sha=_optional_str(adoption.get("base_sha")),
            auto_merge=auto_merge_value,
            monitor_policy={
                "auto_merge": auto_merge_value,
                "auto_merge_intent": auto_merge_intent,
                "auto_merge_resolved": auto_merge_resolved,
                "initial_review_grace_period_seconds": (
                    workspace.initial_review_grace_period_seconds
                ),
                "execution": pr_adoption_execution_policy(workspace.task_policy),
            },
            attached_existing=attached_existing,
            validation_provenance=validation_freshness_summary(
                workspace,
                validation_runs,
                candidate=candidate,
            ),
            status_url=f"/v1/workspaces/{workspace.id}",
            events_url=f"/v1/workspaces/{workspace.id}/events",
            logs_url=f"/v1/workspaces/{workspace.id}/logs",
        )
