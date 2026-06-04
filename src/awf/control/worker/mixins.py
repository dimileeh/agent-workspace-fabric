"""Explicit delegate mixins for extracted worker methods."""

from __future__ import annotations

from awf.control.worker import claims as _claims
from awf.control.worker import cleanup as _cleanup
from awf.control.worker import dispatch_methods as _dispatch_methods
from awf.control.worker import recovery as _recovery
from awf.control.worker import scheduler_methods as _scheduler_methods


class WorkerDelegatesMixin:
    """Methods mechanically delegated to focused implementation modules."""

    _claim_requested_ids = _claims._claim_requested_ids
    _claim_requested_ids_with_capacity = _claims._claim_requested_ids_with_capacity
    _claim_requested_capacity_candidates = _claims._claim_requested_capacity_candidates
    _log_stale_requested_claims = _claims._log_stale_requested_claims
    _claim_requested_for_provisioning = _claims._claim_requested_for_provisioning
    _claim_monitoring_pr_ids = _claims._claim_monitoring_pr_ids
    _claim_monitoring_pr = _claims._claim_monitoring_pr
    _safely_resume_claimed_pr_monitor = _claims._safely_resume_claimed_pr_monitor
    _finish_monitor_recovery_operation = _claims._finish_monitor_recovery_operation
    _finish_monitor_recovery_operation_after_cancellation = (
        _claims._finish_monitor_recovery_operation_after_cancellation
    )
    _refresh_monitoring_pr_claim = _claims._refresh_monitoring_pr_claim
    _refresh_execution_claim = _claims._refresh_execution_claim
    _release_execution_claim = _claims._release_execution_claim
    _release_monitoring_pr_claim = _claims._release_monitoring_pr_claim

    _maybe_expire_due_secret_leases = _cleanup._maybe_expire_due_secret_leases
    _expire_due_secret_leases = _cleanup._expire_due_secret_leases
    _maybe_release_terminal_runtime = _cleanup._maybe_release_terminal_runtime
    _maybe_reconcile_orphan_dirs = _cleanup._maybe_reconcile_orphan_dirs
    _maybe_reap_classified_orphans = _cleanup._maybe_reap_classified_orphans
    _release_terminal_runtime_resources = _cleanup._release_terminal_runtime_resources
    _resume_pending_planning_scope_auto_retries_after_terminal_release = (
        _cleanup._resume_pending_planning_scope_auto_retries_after_terminal_release
    )
    _list_terminal_released_pending_planning_scope_auto_retry_candidates = (
        _cleanup._list_terminal_released_pending_planning_scope_auto_retry_candidates
    )
    _list_terminal_runtime_candidates = _cleanup._list_terminal_runtime_candidates
    _release_terminal_runtime_for_candidate = _cleanup._release_terminal_runtime_for_candidate
    _teardown_terminal_auth_overlay = _cleanup._teardown_terminal_auth_overlay
    _record_terminal_runtime_released = _cleanup._record_terminal_runtime_released
    _record_terminal_runtime_release_failed = _cleanup._record_terminal_runtime_release_failed
    _has_terminal_runtime_release_event = _cleanup._has_terminal_runtime_release_event
    _has_terminal_runtime_release_failure_event = (
        _cleanup._has_terminal_runtime_release_failure_event
    )

    _draining_execution_task_count = _dispatch_methods._draining_execution_task_count
    _available_execution_slots = _dispatch_methods._available_execution_slots
    _can_dispatch_execution_when_slot_available = (
        _dispatch_methods._can_dispatch_execution_when_slot_available
    )
    _preserved_active_validation_can_continue = (
        _dispatch_methods._preserved_active_validation_can_continue
    )
    _dispatchable_execution_ids = _dispatch_methods._dispatchable_execution_ids
    _dispatch_ready_executions = _dispatch_methods._dispatch_ready_executions
    _dispatch_monitor_resumes = _dispatch_methods._dispatch_monitor_resumes
    _dispatch_preserved_active_validation = _dispatch_methods._dispatch_preserved_active_validation
    _track_execution_task = _dispatch_methods._track_execution_task
    _forget_execution_task = _dispatch_methods._forget_execution_task
    _tracked_monitor_workspace_ids = _dispatch_methods._tracked_monitor_workspace_ids
    _tracked_draining_workspace_ids = _dispatch_methods._tracked_draining_workspace_ids
    _reconcile_stale_monitor_execution_tasks = (
        _dispatch_methods._reconcile_stale_monitor_execution_tasks
    )
    _update_execution_slot_saturation = _dispatch_methods._update_execution_slot_saturation
    _safely_provision_claimed = _dispatch_methods._safely_provision_claimed
    _safely_execute = _dispatch_methods._safely_execute
    _safely_execute_claimed = _dispatch_methods._safely_execute_claimed
    _safely_resume_pr_monitor = _dispatch_methods._safely_resume_pr_monitor
    _refresh_monitoring_pr_claim_loop = _dispatch_methods._refresh_monitoring_pr_claim_loop
    _refresh_execution_claim_loop = _dispatch_methods._refresh_execution_claim_loop

    _maybe_recover_stale_active_executions = _recovery._maybe_recover_stale_active_executions
    _recover_stale_active_executions = _recovery._recover_stale_active_executions
    _record_db_connection_closed_event = _recovery._record_db_connection_closed_event
    _list_stale_active_execution_candidates = _recovery._list_stale_active_execution_candidates
    _recover_stale_active_execution = _recovery._recover_stale_active_execution
    _stale_active_candidate_is_current = _recovery._stale_active_candidate_is_current
    _candidate_has_claim = _recovery._candidate_has_claim
    _has_preserved_active_recovery_evidence = _recovery._has_preserved_active_recovery_evidence
    _recover_preserved_active_execution = _recovery._recover_preserved_active_execution
    _record_stale_active_execution_detected = _recovery._record_stale_active_execution_detected
    _has_stale_active_execution_event = _recovery._has_stale_active_execution_event
    _record_preserved_active_execution_after_restart = (
        _recovery._record_preserved_active_execution_after_restart
    )
    _attach_preserved_active_pr_monitor = _recovery._attach_preserved_active_pr_monitor
    _request_preserved_active_validation = _recovery._request_preserved_active_validation
    _cancel_superseded_active_execution_operations = (
        _recovery._cancel_superseded_active_execution_operations
    )
    _create_preserved_active_replacement = _recovery._create_preserved_active_replacement
    _record_preserved_active_operator_required = (
        _recovery._record_preserved_active_operator_required
    )
    _record_preserved_active_salvage_not_possible = (
        _recovery._record_preserved_active_salvage_not_possible
    )
    _record_preserved_active_salvage_blocked = _recovery._record_preserved_active_salvage_blocked
    _has_operator_refresh_after_latest_preservation = (
        _recovery._has_operator_refresh_after_latest_preservation
    )
    _has_operator_refresh_after_latest_preservation_for_workspace = (
        _recovery._has_operator_refresh_after_latest_preservation_for_workspace
    )
    _has_current_preserved_active_execution = _recovery._has_current_preserved_active_execution
    _has_expired_preserved_active_execution = _recovery._has_expired_preserved_active_execution
    _active_execution_preservation_is_expired = _recovery._active_execution_preservation_is_expired
    _has_preserved_active_execution_event = _recovery._has_preserved_active_execution_event
    _latest_preserved_active_execution_at = _recovery._latest_preserved_active_execution_at
    _latest_preserved_active_execution_event = _recovery._latest_preserved_active_execution_event
    _has_current_salvage_event = _recovery._has_current_salvage_event
    _latest_current_salvage_blocked_reason = _recovery._latest_current_salvage_blocked_reason
    _has_active_preserved_validation_recovery = _recovery._has_active_preserved_validation_recovery
    _latest_operator_refresh_requested_at = _recovery._latest_operator_refresh_requested_at
    _active_execution_preservation_event_floor = (
        _recovery._active_execution_preservation_event_floor
    )
    _active_execution_preservation_salvage_event_floor = (
        _recovery._active_execution_preservation_salvage_event_floor
    )
    _resolve_preserved_active_branch_open_pr = _recovery._resolve_preserved_active_branch_open_pr
    _classify_preserved_active_worktree = _recovery._classify_preserved_active_worktree
    _preserved_active_worktree_path = _recovery._preserved_active_worktree_path
    _run_preserved_active_git = _recovery._run_preserved_active_git
    _cleanup_and_fail_stale_active_execution = _recovery._cleanup_and_fail_stale_active_execution
    _stale_active_execution_can_fail = _recovery._stale_active_execution_can_fail
    _record_stale_active_execution_cleanup_failed = (
        _recovery._record_stale_active_execution_cleanup_failed
    )
    _fail_stale_active_execution = _recovery._fail_stale_active_execution
    _fail_stranded_workspace = _recovery._fail_stranded_workspace
    _record_recoverable_runtime_stranding = _recovery._record_recoverable_runtime_stranding
    _has_runtime_stranding_event = _recovery._has_runtime_stranding_event
    _remember_active_salvage_monitor_recovery_operation_id = (
        _recovery._remember_active_salvage_monitor_recovery_operation_id
    )
    _forget_active_salvage_monitor_recovery_operation_id = (
        _recovery._forget_active_salvage_monitor_recovery_operation_id
    )
    _active_salvage_monitor_resume_cooldown_blocks_claim = (
        _recovery._active_salvage_monitor_resume_cooldown_blocks_claim
    )
    _persisted_active_salvage_monitor_resume_cooldown_active = (
        _recovery._persisted_active_salvage_monitor_resume_cooldown_active
    )
    _record_active_salvage_monitor_resume_cooldown = (
        _recovery._record_active_salvage_monitor_resume_cooldown
    )
    _remember_active_salvage_monitor_resume_cooldown = (
        _recovery._remember_active_salvage_monitor_resume_cooldown
    )
    _evict_expired_salvage_monitor_cooldowns = _recovery._evict_expired_salvage_monitor_cooldowns
    _active_salvage_monitor_resume_cooldown_active = (
        _recovery._active_salvage_monitor_resume_cooldown_active
    )

    _list_scheduler_dispatchable_ids = _scheduler_methods._list_scheduler_dispatchable_ids
    _list_scheduler_dispatchable_ids_from_pages = (
        _scheduler_methods._list_scheduler_dispatchable_ids_from_pages
    )
    _filter_scheduler_candidate_workspaces = (
        _scheduler_methods._filter_scheduler_candidate_workspaces
    )
    _filter_scheduler_candidate_workspaces_with_result = (
        _scheduler_methods._filter_scheduler_candidate_workspaces_with_result
    )
    _filter_provider_recovery_suppressed = _scheduler_methods._filter_provider_recovery_suppressed
    _filter_provider_recovery_suppressed_with_result = (
        _scheduler_methods._filter_provider_recovery_suppressed_with_result
    )
    _record_ordered_decisions = _scheduler_methods._record_ordered_decisions
    _load_workspace_statuses = _scheduler_methods._load_workspace_statuses
    _filter_current_status = _scheduler_methods._filter_current_status
