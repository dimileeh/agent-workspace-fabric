"""Explicit delegate mixins for extracted orchestrator methods."""

from __future__ import annotations

from awf.runtime.pr_monitor_runner import ci_ops as _ci_ops
from awf.runtime.pr_monitor_runner import comments as _comments
from awf.runtime.pr_monitor_runner import feedback_state as _feedback_state
from awf.runtime.pr_monitor_runner import fix_cycle as _fix_cycle
from awf.runtime.pr_monitor_runner import gate_events as _gate_events
from awf.runtime.pr_monitor_runner import gates as _gates
from awf.runtime.pr_monitor_runner import lifecycle as _lifecycle
from awf.runtime.pr_monitor_runner import loop as _loop
from awf.runtime.pr_monitor_runner import merge_attention as _merge_attention
from awf.runtime.pr_monitor_runner import operations as _operations
from awf.runtime.pr_monitor_runner import operator_hints as _operator_hints
from awf.runtime.pr_monitor_runner import outdated_resolution as _outdated_resolution
from awf.runtime.pr_monitor_runner import pre_push_validation as _pre_push_validation
from awf.runtime.pr_monitor_runner import provider_ops as _provider_ops
from awf.runtime.pr_monitor_runner import remote_ops as _remote_ops
from awf.runtime.pr_monitor_runner import remote_prompt_ops as _remote_prompt_ops
from awf.runtime.pr_monitor_runner import remote_repair as _remote_repair
from awf.runtime.pr_monitor_runner import remote_repair_diffs as _remote_repair_diffs
from awf.runtime.pr_monitor_runner import remote_repair_protected as _remote_repair_protected
from awf.runtime.pr_monitor_runner import (
    remote_repair_protected_grants as _remote_repair_protected_grants,
)
from awf.runtime.pr_monitor_runner import transient_ops as _transient_ops


class RunnerDelegatesMixin:
    """Methods mechanically delegated to focused implementation modules."""

    _run_ci_fix = _ci_ops._run_ci_fix

    _post_human_notification_once = _comments._post_human_notification_once
    _address_thread = _comments._address_thread
    _address_review_comment = _comments._address_review_comment
    _address_review_comment_result = _comments._address_review_comment_result
    _invoke_cli_for_verdict = _comments._invoke_cli_for_verdict
    _invoke_cli_for_verdict_result = _comments._invoke_cli_for_verdict_result

    _record_pr_feedback_resolution = _feedback_state._record_pr_feedback_resolution
    _refresh_pr_feedback_resolution_state = _feedback_state._refresh_pr_feedback_resolution_state
    _apply_pr_feedback_resolution_state = _feedback_state._apply_pr_feedback_resolution_state

    _run_fix_cycle = _fix_cycle._run_fix_cycle
    _run_operator_hint_cycle = _operator_hints._run_operator_hint_cycle
    _resolve_addressed_outdated_threads = _outdated_resolution._resolve_addressed_outdated_threads

    _active_policy_block_message = _gate_events._active_policy_block_message
    _record_merge_coordination_event = _gate_events._record_merge_coordination_event
    _record_pre_merge_settle_event = _gate_events._record_pre_merge_settle_event
    _record_non_check_reviewer_settle_decision = (
        _gate_events._record_non_check_reviewer_settle_decision
    )

    _refresh_scope_policy_for_merge = _gates._refresh_scope_policy_for_merge
    _merge_gate_for_workspace = _gates._merge_gate_for_workspace
    _merge_gate_with_legacy_head_support = _gates._merge_gate_with_legacy_head_support
    _handle_merge_gate_blocker = _gates._handle_merge_gate_blocker
    _merge_queue_blockers_for_workspace = _gates._merge_queue_blockers_for_workspace
    _wait_for_merge_queue = _gates._wait_for_merge_queue

    _load_workspace = _lifecycle._load_workspace
    _load_state = _lifecycle._load_state
    _refresh_operator_state_from_workspace = _lifecycle._refresh_operator_state_from_workspace
    _persist_state = _lifecycle._persist_state
    _set_workspace_attention = _merge_attention._set_workspace_attention
    _set_workspace_attention_with_merge_block_marker = (
        _merge_attention._set_workspace_attention_with_merge_block_marker
    )
    _clear_workspace_attention = _merge_attention._clear_workspace_attention
    _clear_stale_merge_attention = _merge_attention._clear_stale_merge_attention
    _persist_merge_block_attention_durably = _merge_attention._persist_merge_block_attention_durably
    _clear_merge_block_attention_durably = _merge_attention._clear_merge_block_attention_durably
    _persist_forge_transient_retry_count = _lifecycle._persist_forge_transient_retry_count
    _remove_forge_transient_retry_count = _lifecycle._remove_forge_transient_retry_count
    _clear_preserved_head_marker_durably = _lifecycle._clear_preserved_head_marker_durably
    _clear_preserved_marker_and_consume_grants_durably = (
        _lifecycle._clear_preserved_marker_and_consume_grants_durably
    )
    _terminate_completed = _lifecycle._terminate_completed
    _reconcile_target_branch_after_merge = _lifecycle._reconcile_target_branch_after_merge
    _teardown_compose_stack = _lifecycle._teardown_compose_stack
    _gc_completed_workspace_filesystem = _lifecycle._gc_completed_workspace_filesystem
    _terminate_failed = _lifecycle._terminate_failed

    _execute = _loop._execute

    _begin_monitor_operation = _operations._begin_monitor_operation
    _finish_monitor_operation = _operations._finish_monitor_operation
    _finish_provider_auth_failed_operation = _operations._finish_provider_auth_failed_operation
    _begin_monitor_state_operation = _operations._begin_monitor_state_operation
    _record_monitor_state_operation = _operations._record_monitor_state_operation
    _sleep_with_monitor_state_operation = _operations._sleep_with_monitor_state_operation

    _validated_git_push_result = _pre_push_validation._validated_git_push_result
    _run_pre_push_validation = _pre_push_validation._run_pre_push_validation
    _run_pre_push_validation_with_fix_passes = (
        _pre_push_validation._run_pre_push_validation_with_fix_passes
    )

    _open_monitor_log = _provider_ops._open_monitor_log
    _write_monitor_log = _provider_ops._write_monitor_log
    _record_stale_pending_check_warnings = _provider_ops._record_stale_pending_check_warnings
    _append_workspace_events = _provider_ops._append_workspace_events
    _workspace_test_commands = _provider_ops._workspace_test_commands
    _provider_recovery_suppresses_cli = _provider_ops._provider_recovery_suppresses_cli
    _record_provider_agent_run_error = _provider_ops._record_provider_agent_run_error
    _handle_provider_agent_run_error = _provider_ops._handle_provider_agent_run_error
    _record_pr_monitor_audit_event = _provider_ops._record_pr_monitor_audit_event

    _refresh_supply_chain_policy_before_push = _remote_ops._refresh_supply_chain_policy_before_push
    _run_sync_base = _remote_ops._run_sync_base
    _refresh_staleness_after_sync_base = _remote_ops._refresh_staleness_after_sync_base
    _fetch_base = _remote_ops._fetch_base
    _fetch_base_once = _remote_ops._fetch_base_once
    _repair_orphaned_broken_awf_ref = _remote_ops._repair_orphaned_broken_awf_ref
    _can_remove_broken_awf_ref = _remote_ops._can_remove_broken_awf_ref
    _count_base_behind = _remote_ops._count_base_behind
    _rev_parse_head = _remote_ops._rev_parse_head
    _git_push = _remote_ops._git_push
    _git_push_result = _remote_ops._git_push_result

    _pre_existing_dirty_repair_worktree_result = (
        _remote_repair._pre_existing_dirty_repair_worktree_result
    )
    _repair_operation_start_head_result = _remote_repair._repair_operation_start_head_result
    _open_merge_candidate_head_sha = _remote_repair._open_merge_candidate_head_sha
    _resolve_task_tag = _remote_repair._resolve_task_tag
    _resolve_block_resume_phase = _remote_repair._resolve_block_resume_phase
    _clear_block_resume_phase = _remote_repair._clear_block_resume_phase
    _commit_dirty_worktree = _remote_repair._commit_dirty_worktree
    _repair_protected_scope_commits_before_push = (
        _remote_repair_protected._repair_protected_scope_commits_before_push
    )
    _pause_monitor_for_protected_scope_block = (
        _remote_repair_protected._pause_monitor_for_protected_scope_block
    )
    _post_protected_block_notification = _remote_repair_protected._post_protected_block_notification
    _active_operator_grant_specs = _remote_repair_protected_grants._active_operator_grant_specs
    _preserved_protected_change_fully_granted = (
        _remote_repair_protected_grants._preserved_protected_change_fully_granted
    )
    _consume_active_operator_grants = (
        _remote_repair_protected_grants._consume_active_operator_grants
    )
    _preserved_commit_already_on_remote = (
        _remote_repair_protected_grants._preserved_commit_already_on_remote
    )
    _preserved_commit_in_unpushed_range = (
        _remote_repair_protected_grants._preserved_commit_in_unpushed_range
    )
    _preserved_head_on_remote_fetch_head = (
        _remote_repair_protected_grants._preserved_head_on_remote_fetch_head
    )
    _preserved_commit_reachable_from_head = (
        _remote_repair_protected_grants._preserved_commit_reachable_from_head
    )
    _rollback_protected_scope_repair_delta_before_push = (
        _remote_repair_protected._rollback_protected_scope_repair_delta_before_push
    )
    _protected_scope_repair_delta_paths = (
        _remote_repair_protected._protected_scope_repair_delta_paths
    )
    _record_protected_scope_rollback_result = (
        _remote_repair_protected._record_protected_scope_rollback_result
    )
    _repair_protected_scope_changes_before_commit = (
        _remote_repair_protected._repair_protected_scope_changes_before_commit
    )
    _protected_scope_violations_not_restored_to_remote_branch = (
        _remote_repair_protected._protected_scope_violations_not_restored_to_remote_branch
    )
    _protected_scope_violations_for_status = (
        _remote_repair_protected._protected_scope_violations_for_status
    )
    _protected_scope_violations_for_unpushed_commits = (
        _remote_repair_protected._protected_scope_violations_for_unpushed_commits
    )
    _changed_paths_since_remote_branch = _remote_repair_diffs._changed_paths_since_remote_branch
    _remote_branch_diff_base_and_changed_paths = (
        _remote_repair_diffs._remote_branch_diff_base_and_changed_paths
    )
    _remote_branch_fetch_once = _remote_repair_diffs._remote_branch_fetch_once
    _merge_base_with_head = _remote_repair_diffs._merge_base_with_head
    _changed_paths_between_ref_and_head = _remote_repair_diffs._changed_paths_between_ref_and_head
    _protected_file_diffs_for_status_paths = (
        _remote_repair_diffs._protected_file_diffs_for_status_paths
    )
    _protected_scope_violations_for_sync_base_push = (
        _remote_repair_protected._protected_scope_violations_for_sync_base_push
    )
    _protected_scope_diff_unavailable_block = (
        _remote_repair_protected._protected_scope_diff_unavailable_block
    )
    _protected_scope_diff_unavailable_push_result = (
        _remote_repair_protected._protected_scope_diff_unavailable_push_result
    )
    _protected_scope_push_block = _remote_repair_protected._protected_scope_push_block
    _protected_scope_repair_prompt = _remote_prompt_ops._protected_scope_repair_prompt
    _protected_scope_committed_repair_prompt = (
        _remote_prompt_ops._protected_scope_committed_repair_prompt
    )
    _protected_scope_prompt_sections = _remote_prompt_ops._protected_scope_prompt_sections
    _record_sync_base_progress = _remote_prompt_ops._record_sync_base_progress

    _fetch_status_for_decision = _transient_ops._fetch_status_for_decision
    _wait_after_transient_github_error = _transient_ops._wait_after_transient_github_error
    _wait_after_transient_bitbucket_error = _transient_ops._wait_after_transient_bitbucket_error
    _wait_after_transient_forge_error = _transient_ops._wait_after_transient_forge_error
    _clear_forge_transient_retry_state_on_success = (
        _transient_ops._clear_forge_transient_retry_state_on_success
    )
    _wait_after_transient_base_fetch_error = _transient_ops._wait_after_transient_base_fetch_error
    _write_defer_signal = _transient_ops._write_defer_signal
