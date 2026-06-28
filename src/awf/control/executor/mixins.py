"""Explicit delegate mixins for extracted orchestrator methods."""

from __future__ import annotations

from awf.control.executor import git_methods as _git_methods
from awf.control.executor import monitor_handoff as _monitor_handoff
from awf.control.executor import monitor_handoff_setup as _monitor_handoff_setup
from awf.control.executor import monitor_handoff_sync as _monitor_handoff_sync
from awf.control.executor import ollama_model as _ollama_model
from awf.control.executor import planning_conformance as _planning_conformance
from awf.control.executor import planning_ops as _planning_ops
from awf.control.executor import quality_methods as _quality_methods
from awf.control.executor import state_ops as _state_ops
from awf.control.executor import validation_ops as _validation_ops


class ExecutorDelegatesMixin:
    """Methods mechanically delegated to focused implementation modules."""

    _repair_agent_git_ownership = _git_methods._repair_agent_git_ownership
    _run_agent_git_writability_preflight = _git_methods._run_agent_git_writability_preflight
    _recover_missing_git_head_or_mark_failed = _git_methods._recover_missing_git_head_or_mark_failed
    _recover_orphan_history = _git_methods._recover_orphan_history
    _record_git_object_recovery_event = _git_methods._record_git_object_recovery_event
    _recover_feature_branch_remote_push_branch = (
        _git_methods._recover_feature_branch_remote_push_branch
    )
    _block_open_pr_reexecution_without_recovery = (
        _git_methods._block_open_pr_reexecution_without_recovery
    )
    _ensure_worktree_available = _git_methods._ensure_worktree_available
    _ensure_ollama_model_or_mark_failed = _ollama_model._ensure_ollama_model_or_mark_failed
    _git_rev_parse_head = _git_methods._git_rev_parse_head
    _git_commit_count_since = _git_methods._git_commit_count_since
    _changed_paths = _git_methods._changed_paths
    _committed_paths_since = _git_methods._committed_paths_since
    _protected_file_diffs_for_staged_paths = _git_methods._protected_file_diffs_for_staged_paths
    _begin_rebase_recovery_operation = _git_methods._begin_rebase_recovery_operation
    _find_active_rebase_recovery_operation = _git_methods._find_active_rebase_recovery_operation
    _finish_rebase_recovery_operation = _git_methods._finish_rebase_recovery_operation
    _run_monitor_rebase_recovery = _git_methods._run_monitor_rebase_recovery
    _record_current_rebase_recovery_head = _git_methods._record_current_rebase_recovery_head
    _record_rebase_recovery_success = _git_methods._record_rebase_recovery_success
    _clear_rebase_recovery_staleness = _git_methods._clear_rebase_recovery_staleness
    _start_pending_recovery_operations = _git_methods._start_pending_recovery_operations
    _finish_active_recovery_operations = _git_methods._finish_active_recovery_operations
    _finish_active_recovery_operations_in_session = (
        _git_methods._finish_active_recovery_operations_in_session
    )
    _finish_ignored_stale_callback_operations_in_session = (
        _git_methods._finish_ignored_stale_callback_operations_in_session
    )

    _record_executor_pr_audit_event = _monitor_handoff._record_executor_pr_audit_event
    _add_executor_pr_audit_event = _monitor_handoff._add_executor_pr_audit_event
    _record_setup_dependency_network_events = (
        _monitor_handoff._record_setup_dependency_network_events
    )
    _record_runtime_toolchain_findings = _monitor_handoff._record_runtime_toolchain_findings
    _record_runtime_toolchain_findings_safe = (
        _monitor_handoff._record_runtime_toolchain_findings_safe
    )
    _record_runtime_browser_findings = _monitor_handoff._record_runtime_browser_findings
    _record_runtime_browser_findings_safe = _monitor_handoff._record_runtime_browser_findings_safe
    _reject_unsupported_task_kind = _monitor_handoff._reject_unsupported_task_kind
    _dispatch_non_feature_task_kind = _monitor_handoff._dispatch_non_feature_task_kind
    _run_monitor_handoff_profile_setup = _monitor_handoff_setup._run_monitor_handoff_profile_setup
    _build_handoff_pr_monitor = _monitor_handoff._build_handoff_pr_monitor
    _handoff_sync_release_pr_monitor = _monitor_handoff_sync._handoff_sync_release_pr_monitor
    _complete_release_pr_sync_no_op = _monitor_handoff_sync._complete_release_pr_sync_no_op
    _handoff_sync_feature_pr_monitor = _monitor_handoff_sync._handoff_sync_feature_pr_monitor
    _record_monitor_runtime_restart_failed = _monitor_handoff._record_monitor_runtime_restart_failed

    _prepare_conformance_salvage_for_execution = (
        _planning_conformance._prepare_conformance_salvage_for_execution
    )
    _fail_conformance_salvage_execution = _planning_conformance._fail_conformance_salvage_execution
    _record_conformance_salvage_event = _planning_conformance._record_conformance_salvage_event
    _materialize_salvage_patch_for_agent = (
        _planning_conformance._materialize_salvage_patch_for_agent
    )
    _exclude_agent_salvage_artifacts = _planning_conformance._exclude_agent_salvage_artifacts
    _record_planning_validation_handoff_event = (
        _planning_conformance._record_planning_validation_handoff_event
    )
    _run_post_validation_conformance_check = (
        _planning_conformance._run_post_validation_conformance_check
    )
    _capture_post_validation_conformance_scope_baseline = (
        _planning_conformance._capture_post_validation_conformance_scope_baseline
    )
    _write_satisfied_post_validation_conformance_report = staticmethod(
        _planning_conformance._write_satisfied_post_validation_conformance_report
    )
    _record_post_validation_conformance_event = (
        _planning_conformance._record_post_validation_conformance_event
    )
    _validation_run_evidence_for_conformance = (
        _planning_conformance._validation_run_evidence_for_conformance
    )
    _auto_retry_planning_scope_failure = _planning_ops._auto_retry_planning_scope_failure
    _run_agent_task_with_optional_planning = _planning_ops._run_agent_task_with_optional_planning
    _build_conformance_stall_failure = _planning_ops._build_conformance_stall_failure
    _digest_dirty_content = _planning_ops._digest_dirty_content

    _run_baseline_coverage_preflight = _quality_methods._run_baseline_coverage_preflight
    _measure_and_persist_baseline_coverage = _quality_methods._measure_and_persist_baseline_coverage
    _run_final_coverage_gate = _quality_methods._run_final_coverage_gate
    _parallel_worker_cpu_limit_for_workspace = (
        _quality_methods._parallel_worker_cpu_limit_for_workspace
    )
    _refresh_supply_chain_policy_for_workspace = (
        _quality_methods._refresh_supply_chain_policy_for_workspace
    )
    _verify_recovered_post_agent_commit = _quality_methods._verify_recovered_post_agent_commit
    _verify_recovered_post_agent_commit_or_mark_failed = (
        _quality_methods._verify_recovered_post_agent_commit_or_mark_failed
    )
    _fail_if_plan_only_paths = _quality_methods._fail_if_plan_only_paths
    _committed_and_staged_output_is_plan_only = (
        _quality_methods._committed_and_staged_output_is_plan_only
    )
    _fail_if_plan_only_committed_output = _quality_methods._fail_if_plan_only_committed_output
    _fail_if_protected_quality_gate_committed_output = (
        _quality_methods._fail_if_protected_quality_gate_committed_output
    )
    _record_post_agent_commit_format_repair = (
        _quality_methods._record_post_agent_commit_format_repair
    )
    _run_post_agent_commit_repair = _quality_methods._run_post_agent_commit_repair
    _run_post_agent_deterministic_precommit_repair = (
        _quality_methods._run_post_agent_deterministic_precommit_repair
    )
    _run_post_agent_autofixable_precommit_repair = (
        _quality_methods._run_post_agent_autofixable_precommit_repair
    )
    _run_post_agent_semantic_precommit_repair = (
        _quality_methods._run_post_agent_semantic_precommit_repair
    )
    _mark_post_agent_commit_failed = _quality_methods._mark_post_agent_commit_failed
    _prepare_provider_recovery = _quality_methods._prepare_provider_recovery

    _load_workspace = _state_ops._load_workspace
    _persist_resolved_profile_snapshot_if_missing = (
        _state_ops._persist_resolved_profile_snapshot_if_missing
    )
    _claim_ready = _state_ops._claim_ready
    _begin_execution = _state_ops._begin_execution
    _update_subphase = _state_ops._update_subphase
    _recheck_status = _state_ops._recheck_status
    _transition_if_current = _state_ops._transition_if_current
    _record_stale_action_skip = _state_ops._record_stale_action_skip
    _record_health_check_failed_event = _state_ops._record_health_check_failed_event
    _mark_failed = _state_ops._mark_failed
    enter_blocked_for_protected_violation = _state_ops.enter_blocked_for_protected_violation
    enter_recovering_for_provider_failure = _state_ops.enter_recovering_for_provider_failure
    _persist_block_baseline_coverage = _state_ops._persist_block_baseline_coverage
    _persist_block_planning_conformance_handoff = (
        _state_ops._persist_block_planning_conformance_handoff
    )
    _active_operator_grant_specs = _state_ops._active_operator_grant_specs
    _consume_active_operator_grants = _state_ops._consume_active_operator_grants

    _start_validation_run = _validation_ops._start_validation_run
    _capture_workspace_head_sha = _validation_ops._capture_workspace_head_sha
    _finish_pending_validate_operations = _validation_ops._finish_pending_validate_operations
    _finish_pending_validate_operations_in_session = (
        _validation_ops._finish_pending_validate_operations_in_session
    )
    _finish_validation_run = _validation_ops._finish_validation_run
    _finish_validation_callback_if_terminal = (
        _validation_ops._finish_validation_callback_if_terminal
    )
    _set_validation_run_target_head_sha = _validation_ops._set_validation_run_target_head_sha
