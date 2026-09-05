"""Feature-PR and hosted adoption helpers for workspace retry.

Mechanically extracted from ``awf.service.workspaces_retry`` so that module stays
under the first-party line-count guardrail with headroom for remaining narrowly
justified corrections. Retry-row orchestration and forge prefetch remain in
``workspaces_retry``; this module owns feature/sync PR identity, adoption live-ref
sync, trusted-profile freeze cleanup, and hosted adoption qualification.
Re-exported from ``workspaces_retry`` for import compatibility only — inter-helper
name lookups stay in this module's namespace, so patching the ``workspaces_retry``
aliases does not redirect those calls.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from awf.common.config import Settings
from awf.common.forge import concrete_forge_for_repo
from awf.common.forge_lifecycle import PullRequestLifecycle
from awf.common.github_client import RepoRef, parse_forge_pull_request_url
from awf.common.workspace_policy import (
    PR_ADOPTION_EXECUTION_MODE_LOCAL,
    pr_adoption_is_hosted,
)
from awf.db.enums import TaskKind
from awf.db.models import Workspace
from awf.runtime.hosted_delegation import HostedDelegationConfigError
from awf.runtime.pr_monitor_actions import AbortReason
from awf.runtime.pr_push_remote import retained_fork_pr_adoption
from awf.service.config import (
    hosted_delegation_config_from_service_settings,
    resolve_service_settings,
)
from awf.service.workspaces_retry_errors import WorkspaceHostedDelegationNotConfiguredError
from awf.service.workspaces_retry_payloads import _latest_failed_state_event
from awf.service.workspaces_retry_recovery import _planning_scope_retry_context


def _workspace_service() -> Any:
    """Import workspace service symbols lazily to avoid module-level cycles."""
    from awf.service import workspaces

    return workspaces


_PR_NUMBER_RE = re.compile(r"/pull(?:-requests)?/(\d+)(?=[/?#]|$)")
PrLifecycleChecker = Callable[[Workspace, int], Awaitable[PullRequestLifecycle]]

# Explicit nonblocking readiness reason for retained open hosted PR-adoption
# retries that intentionally skip local Codex/CLI/Router probes. Must keep
# ``blocks_launch`` false so deferred Cursor Auto preflight does not re-probe.
HOSTED_PR_ADOPTION_LOCAL_PREFLIGHT_BYPASSED_REASON = "HOSTED_PR_ADOPTION_LOCAL_PREFLIGHT_BYPASSED"


def _hosted_open_adoption_local_preflight_bypass(
    *,
    source_workspace_id: str,
    agent: str,
) -> dict[str, Any]:
    """Build the readiness snapshot that preserves the hosted local-auth bypass.

    Hosted open-adoption retries skip local provider probes. Leaving
    ``provider_readiness_preflight`` absent would still make
    ``_needs_deferred_cursor_auto_router_preflight`` true whenever
    ``cursor_auto_mode`` is present, causing provision-time Router probing
    without Core credentials. A nonblocking snapshot replaces any stale
    ``blocks_launch=true`` source copy and documents the intentional bypass.

    The snapshot must include every field required by
    ``ProviderReadinessPreflightResponse`` so retry/GET responses can serialize
    it after admission (incomplete payloads raise ValidationError).
    """
    from datetime import UTC, datetime

    from awf.db.enums import AgentRuntime
    from awf.service.provider_readiness import _LAUNCH_PROVIDER_BY_AGENT

    try:
        runtime = AgentRuntime(agent)
    except ValueError:
        agent_name = str(agent)
        provider: str = "unknown"
    else:
        agent_name = runtime.value
        provider = _LAUNCH_PROVIDER_BY_AGENT.get(runtime, "unknown")

    return {
        "provider": provider,
        "agent": agent_name,
        "model": None,
        "model_source": None,
        "readiness_status": "ready",
        # Local auth was intentionally not probed; credentials are leased to
        # hosted execution. Mirror the no-provider_result defaults from
        # ``_launch_preflight_payload``.
        "auth_status": "unknown",
        "auth_source": "not_observed",
        "credential_scope": "not_observed",
        "isolation": "none",
        "probe_status": "skipped",
        "reason_code": HOSTED_PR_ADOPTION_LOCAL_PREFLIGHT_BYPASSED_REASON,
        "message": (
            "Retained open hosted PR adoption skips local provider readiness; "
            "credentials are leased to hosted execution."
        ),
        "override_required": False,
        "override_requested": False,
        "override_used": False,
        "blocks_launch": False,
        "checked_at": datetime.now(UTC).isoformat(),
        "credential_sources": [],
        "source_workspace_id": source_workspace_id,
    }


@dataclass(frozen=True, slots=True)
class _PrefetchedFeaturePrState:
    """Forge PR state captured before ``get_for_update`` holds the source row."""

    pr_number: int
    lifecycle: PullRequestLifecycle
    head_ref: str | None = None
    base_sha: str | None = None
    head_sha: str | None = None
    from_snapshot: bool = False


def _source_pr_closed_externally(source: Workspace) -> bool:
    """Return whether the source's terminal transition recorded a closed PR."""
    latest_failed_event = _latest_failed_state_event(source)
    return (
        latest_failed_event is not None
        and latest_failed_event.reason_code == AbortReason.pr_closed_externally.value
    )


def _pr_number_from_url(pr_url: str) -> int | None:
    """Recover a positive PR number from a GitHub or Bitbucket PR URL."""
    match = _PR_NUMBER_RE.search(pr_url)
    if match is None:
        return None
    pr_number = int(match.group(1))
    return pr_number if pr_number > 0 else None


def _sync_feature_pr_adoption(source: Workspace) -> Mapping[str, Any] | None:
    """Return ``task_policy.pr_adoption`` for an adopted sync-feature-PR workspace."""
    if source.task_kind != TaskKind.sync_feature_pr.value:
        return None
    policy = source.task_policy if isinstance(source.task_policy, Mapping) else {}
    adoption = policy.get("pr_adoption")
    return adoption if isinstance(adoption, Mapping) else None


def _existing_feature_pr_url(source: Workspace) -> str | None:
    """Return the source's feature/adopted PR URL from columns or adoption policy."""
    if source.pr_url:
        return source.pr_url
    adoption = _sync_feature_pr_adoption(source)
    if adoption is None:
        return None
    pr_url = adoption.get("pr_url")
    if isinstance(pr_url, str) and pr_url.strip():
        return pr_url.strip()
    return None


def _existing_feature_pr_number(source: Workspace) -> int | None:
    """Return the source's feature/adopted PR number from columns, URL, or adoption."""
    if source.pr_number is not None:
        return source.pr_number
    pr_url = _existing_feature_pr_url(source)
    if pr_url:
        from_url = _pr_number_from_url(pr_url)
        if from_url is not None:
            return from_url
    adoption = _sync_feature_pr_adoption(source)
    if adoption is None:
        return None
    raw = adoption.get("pr_number")
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw > 0 else None
    if isinstance(raw, str) and raw.strip().isdigit():
        parsed = int(raw.strip())
        return parsed if parsed > 0 else None
    return None


def _adoption_policy_str(source: Workspace, key: str) -> str | None:
    """Return a non-empty string from ``task_policy.pr_adoption[key]``, if present."""
    adoption = _sync_feature_pr_adoption(source)
    if adoption is None:
        return None
    raw = adoption.get(key)
    if not isinstance(raw, str):
        return None
    stripped = raw.strip()
    return stripped or None


def _existing_feature_pr_adoption_head_ref(source: Workspace) -> str | None:
    """Return the adopted PR head ref from ``pr_adoption.head_ref``."""
    return _adoption_policy_str(source, "head_ref")


def _existing_feature_pr_adoption_head_sha(source: Workspace) -> str | None:
    """Return the adopted PR head SHA from ``pr_adoption.head_sha``."""
    return _adoption_policy_str(source, "head_sha")


def _existing_feature_pr_adoption_base_sha(source: Workspace) -> str | None:
    """Return the adopted PR base SHA from ``pr_adoption.base_sha``."""
    return _adoption_policy_str(source, "base_sha")


def _sync_retried_adoption_live_refs(
    task_policy: dict[str, Any],
    *,
    head_ref: str | None,
    base_sha: str | None,
    head_sha: str | None = None,
    base_ref: str | None = None,
) -> None:
    """Keep ``pr_adoption`` head/base aligned with live forge refs on retry.

    Provisioning prefers ``pr_adoption.head_ref`` over ``remote_push_branch``
    via ``_provision_remote_push_branch``. If retry only updates the column and
    leaves a stale adoption head (e.g. after a forge rename), provision
    overwrites the live push target and sends fixes to the wrong branch.

    Hosted identity similarly reads ``pr_adoption.head_sha`` as
    ``expected_head_sha``. After a hosted repair advances the tip, retry must
    refresh that OID or an enforcing delegate rejects / validates the wrong
    revision.

    Incomplete hosted adoptions that fall through to local preserve-existing
    repair may still lack ``base_ref`` even after head/base SHAs are restored.
    Callers pass ``workspace.branch_base`` so monitor handoff does not fail
    ``PR_ADOPTION_METADATA_MISSING`` for a recoverable row.
    """
    adoption = task_policy.get("pr_adoption")
    if not isinstance(adoption, dict):
        return
    if isinstance(head_ref, str) and head_ref.strip():
        adoption["head_ref"] = head_ref.strip()
    if isinstance(base_sha, str) and base_sha.strip():
        adoption["base_sha"] = base_sha.strip()
    if isinstance(head_sha, str) and head_sha.strip():
        adoption["head_sha"] = head_sha.strip()
    if isinstance(base_ref, str) and base_ref.strip():
        adoption["base_ref"] = base_ref.strip()


_PROFILE_TRUSTED_BASE_SHA_KEY = "profile_trusted_base_sha"


def _is_exact_full_commit_sha(value: object) -> bool:
    """Return True only for an immutable full Git commit object name (40 hex)."""
    return (
        isinstance(value, str)
        and len(value) == 40
        and all(char in "0123456789abcdefABCDEF" for char in value)
    )


def _auto_selection_profile_ref(profile_ref: str | None) -> str | None:
    """Keep unset/``auto`` selection; clear a post-provision concrete name.

    Successful auto adoption persists the resolved profile name into
    ``profile_ref``. That concrete name must not survive retry when a trusted
    freeze may still need credential rehydration — otherwise
    ``_should_resolve_adopted_auto_profile_from_trusted_base`` rejects the
    trusted-base path, ``ProfileResolver`` prefers the PR-head repo marker,
    and a matching trusted stamp would treat the attacker-controlled profile
    as verified.
    """
    if profile_ref is None:
        return None
    stripped = profile_ref.strip()
    if not stripped or stripped == "auto":
        return profile_ref
    return None


def _drop_mismatched_trusted_profile_freeze_on_retry(
    task_policy: dict[str, Any],
    *,
    resolved_profile: dict[str, Any] | None,
    profile_ref: str | None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Clear a frozen repo profile when its trusted-base stamp no longer matches.

    Retry may refresh ``pr_adoption.base_sha`` to the live forge tip while
    copying ``resolved_profile`` and ``profile_trusted_base_sha`` from the
    failed attempt. A stamp that no longer equals ``base_sha`` makes
    provenance verification fail and silently forces ``auto_merge=False`` for
    an otherwise genuine trusted freeze. Drop the mismatched freeze and stamp
    so provisioning re-resolves from the new base. Matching stamps keep the
    freeze but restore auto ``profile_ref`` selection (see
    ``_auto_selection_profile_ref``).

    Successful auto adoption also persists the concrete resolved name into
    ``profile_ref``. When dropping the freeze, clear that name too so
    ``_should_resolve_adopted_auto_profile_from_trusted_base`` still sees auto
    selection; otherwise retry would resolve from the untrusted PR-head tree.
    """
    if resolved_profile is None:
        return None, profile_ref
    adoption_raw = task_policy.get("pr_adoption")
    if not isinstance(adoption_raw, dict):
        return resolved_profile, profile_ref
    stamped = adoption_raw.get(_PROFILE_TRUSTED_BASE_SHA_KEY)
    base_sha = adoption_raw.get("base_sha")
    if not isinstance(stamped, str) or not _is_exact_full_commit_sha(stamped):
        return resolved_profile, profile_ref
    if (
        isinstance(base_sha, str)
        and _is_exact_full_commit_sha(base_sha.strip())
        and stamped.lower() == base_sha.strip().lower()
    ):
        return resolved_profile, _auto_selection_profile_ref(profile_ref)
    adoption = dict(adoption_raw)
    adoption.pop(_PROFILE_TRUSTED_BASE_SHA_KEY, None)
    task_policy["pr_adoption"] = adoption
    return None, None


def _clear_closed_sync_feature_pr_adoption(
    task_policy: dict[str, Any],
    *,
    source_task_kind: str,
    repo_url: str | None = None,
) -> str:
    """Drop closed adoption identity so retry can open a replacement PR.

    ``sync_feature_pr`` provisioning prefers ``pr_adoption`` / ``refs/pull/<n>/head``
    over a cleared ``remote_push_branch``. Leaving the closed PR's adoption block
    would re-checkout and re-push that head. Adoption is also monitor-only, so
    the replacement must become a coding ``feature_branch_pr``.

    Distinct fork ``head_repo_slug`` / ``head_repo_url`` are retained (same as
    execution-time ``_apply_sync_feature_replacement_policy``) so replacement
    pushes stay on the fork via ``remote_push_url_for_workspace``.
    """
    if source_task_kind != TaskKind.sync_feature_pr.value:
        return source_task_kind
    adoption = task_policy.get("pr_adoption")
    retained = retained_fork_pr_adoption(
        repo_url=repo_url,
        adoption=adoption if isinstance(adoption, dict) else None,
    )
    task_policy.pop("pr_adoption", None)
    if retained is not None:
        task_policy["pr_adoption"] = retained
    task_policy["task_kind"] = TaskKind.feature_branch_pr.value
    return TaskKind.feature_branch_pr.value


def _has_existing_feature_pr_identity(source: Workspace) -> bool:
    """Return whether the source carries feature/sync PR number + URL identity."""
    pr_number = _existing_feature_pr_number(source)
    return (
        source.task_kind in {TaskKind.feature_branch_pr.value, TaskKind.sync_feature_pr.value}
        and _existing_feature_pr_url(source) is not None
        and pr_number is not None
    )


def _is_existing_feature_pr_preserve_candidate(source: Workspace) -> bool:
    """Return whether retry should consult live forge state for this source PR."""
    if _planning_scope_retry_context(source) is not None:
        return False
    return _has_existing_feature_pr_identity(source)


def _is_hosted_adoption_forge_prefetch_candidate(source: Workspace) -> bool:
    """Return whether hosted auth-bypass qualification needs forge prefetch.

    Planning-scope retries deliberately skip preserve-existing-PR live rebinding,
    but retained hosted sync adoptions still need an open-PR forge check so Core
    without local credentials can skip Codex preflight.
    """
    return (
        source.task_kind == TaskKind.sync_feature_pr.value
        and pr_adoption_is_hosted(source.task_policy)
        and _has_existing_feature_pr_identity(source)
    )


def _adoption_identity_pr_number(adoption: Mapping[str, Any]) -> int | None:
    """Return a positive PR number from the adoption block itself (not columns)."""
    raw = adoption.get("pr_number")
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw > 0 else None
    if isinstance(raw, str) and raw.strip().isdigit():
        parsed = int(raw.strip())
        return parsed if parsed > 0 else None
    return None


def _retained_hosted_adoption_identity_is_complete_and_consistent(
    source: Workspace,
    prefetched_feature_pr: _PrefetchedFeaturePrState,
) -> bool:
    """Return whether ``pr_adoption`` itself carries a complete retained identity.

    Hosted retry may skip local provider authentication, so admission must not
    trust a hosted marker plus generic workspace PR columns. The adoption block
    must include repo/PR/head/base identity (including ``head_sha``), and that
    identity must agree with the trusted source repository and the prefetched
    open PR (and any PR number already resolved on the row).

    When ``Workspace.pr_url`` is set, it must parse to the same forge/repo/PR as
    the validated adoption identity. ``_existing_feature_pr_url`` and
    ``hosted_pr_identity_for_workspace`` both prefer the column over adoption,
    so a spoofed or stale column with a matching PR number would otherwise
    admit the local-auth bypass while handing the delegate a foreign URL.

    Target identity is ``repo_slug`` + parseable ``pr_url`` for the *base*
    repository. Optional fork ``head_repo_slug`` is distinct and is not required
    to match the target. Live forge head/base SHAs may advance after adoption,
    so this gate does not demand equality with the original snapshot SHAs.

    When prefetch carried a live ``PullRequestSnapshot`` (``from_snapshot``),
    usable live ``head_ref`` and full ``head_sha`` are also required. An open PR
    whose head was deleted can report null live head fields; admitting on the
    stored adoption alone would retain a stale revision for hosted delegation.
    """
    adoption = _sync_feature_pr_adoption(source)
    if adoption is None:
        return False

    required_strings = (
        "repo_slug",
        "pr_url",
        "head_ref",
        "base_ref",
        "base_sha",
        "head_sha",
    )
    values: dict[str, str] = {}
    for key in required_strings:
        raw = adoption.get(key)
        if not isinstance(raw, str) or not raw.strip():
            return False
        values[key] = raw.strip()

    if not _is_exact_full_commit_sha(values["base_sha"]):
        return False
    if not _is_exact_full_commit_sha(values["head_sha"]):
        return False

    if prefetched_feature_pr.from_snapshot and not _prefetched_live_head_is_complete(
        prefetched_feature_pr
    ):
        return False

    adoption_pr_number = _adoption_identity_pr_number(adoption)
    if adoption_pr_number is None:
        return False
    if adoption_pr_number != prefetched_feature_pr.pr_number:
        return False

    resolved_pr_number = _existing_feature_pr_number(source)
    if resolved_pr_number is not None and resolved_pr_number != adoption_pr_number:
        return False

    try:
        # Same concrete_forge_for_repo policy as _live_pr_snapshot / prefetch:
        # resolved_profile.forge wins over a hostless owner/repo URL that would
        # otherwise default to github in RepoRef.from_url.
        source_forge = concrete_forge_for_repo(
            (source.resolved_profile or {}).get("forge"),
            source.repo_url,
        )
        source_repo = RepoRef.from_url(source.repo_url)
        source_repo = RepoRef(
            owner=source_repo.owner,
            name=source_repo.name,
            forge=source_forge,
        )
        adoption_repo = RepoRef.from_url(values["repo_slug"])
        # Bare ``owner/repo`` slugs default to github in RepoRef.from_url; bind
        # forge from the trusted source so Bitbucket adoptions compare correctly.
        if "github.com" not in values["repo_slug"] and "bitbucket.org" not in values["repo_slug"]:
            adoption_repo = RepoRef(
                owner=adoption_repo.owner,
                name=adoption_repo.name,
                forge=source_forge,
            )
        url_repo, url_pr_number = parse_forge_pull_request_url(
            values["pr_url"],
            forge=source_forge,
        )
    except ValueError:
        return False

    if url_pr_number != adoption_pr_number:
        return False
    if (
        adoption_repo.forge != source_forge
        or adoption_repo.slug().lower() != source_repo.slug().lower()
    ):
        return False
    if not (
        url_repo.forge == source_forge and url_repo.slug().lower() == source_repo.slug().lower()
    ):
        return False

    column_pr_url = source.pr_url
    if isinstance(column_pr_url, str) and column_pr_url.strip():
        try:
            column_repo, column_pr_number = parse_forge_pull_request_url(
                column_pr_url.strip(),
                forge=source_forge,
            )
        except ValueError:
            return False
        if column_pr_number != adoption_pr_number:
            return False
        if (
            column_repo.forge != source_forge
            or column_repo.slug().lower() != source_repo.slug().lower()
        ):
            return False
    return True


def _prefetched_live_head_is_complete(
    prefetched_feature_pr: _PrefetchedFeaturePrState,
) -> bool:
    """Return whether prefetched forge state carries a usable live PR head."""
    head_ref = prefetched_feature_pr.head_ref
    head_sha = prefetched_feature_pr.head_sha
    return (
        isinstance(head_ref, str)
        and bool(head_ref.strip())
        and isinstance(head_sha, str)
        and _is_exact_full_commit_sha(head_sha.strip())
    )


def _raise_if_open_hosted_adoption_lacks_live_head(
    source: Workspace,
    prefetched_feature_pr: _PrefetchedFeaturePrState | None,
) -> None:
    """Fail closed when an open hosted adoption snapshot lacks a live head.

    Lifecycle-only injectors (``from_snapshot=False``) are unchanged. Production
    forge prefetch always carries a snapshot; an open PR with a deleted or
    otherwise unavailable head must not qualify hosted retry on stale stored
    identity.
    """
    if not _is_hosted_adoption_forge_prefetch_candidate(source):
        return
    if prefetched_feature_pr is None or not prefetched_feature_pr.from_snapshot:
        return
    if prefetched_feature_pr.lifecycle is not PullRequestLifecycle.open:
        return
    pr_number = _existing_feature_pr_number(source)
    if pr_number is None or prefetched_feature_pr.pr_number != pr_number:
        return
    if _prefetched_live_head_is_complete(prefetched_feature_pr):
        return
    workspaces = _workspace_service()
    raise workspaces.WorkspaceRetryPrStateUnavailableError(
        "Could not verify whether the existing pull request is still open.",
        detail={
            "source_workspace_id": source.id,
            "pr_number": pr_number,
            "reason_code": "PR_STATE_LOOKUP_FAILED",
        },
    )


def _is_retained_open_hosted_pr_adoption_retry(
    source: Workspace,
    prefetched_feature_pr: _PrefetchedFeaturePrState | None,
) -> bool:
    """Return whether retry admits a retained open hosted PR adoption.

    Qualification is strict and must key off prefetched forge state as well as
    source policy. A closed PR with a stale hosted marker falls through to the
    local provider preflight (and then closed-PR feature-task fallback), so a
    hosted marker alone must never skip local auth. A malformed or spoofed
    ``sync_feature_pr`` adoption that lacks complete, consistent identity must
    likewise fall through — later retry code can fill missing head/base from
    workspace columns, so identity must be proven before the auth bypass.

    Planning-scope retries are not preserve candidates, but they still retain
    hosted sync adoption; qualification therefore keys off PR identity + forge
    prefetch rather than ``_is_existing_feature_pr_preserve_candidate``.
    """
    if source.task_kind != TaskKind.sync_feature_pr.value:
        return False
    if not pr_adoption_is_hosted(source.task_policy):
        return False
    if not _has_existing_feature_pr_identity(source):
        return False
    if prefetched_feature_pr is None:
        return False
    pr_number = _existing_feature_pr_number(source)
    if pr_number is None or prefetched_feature_pr.pr_number != pr_number:
        return False
    if prefetched_feature_pr.lifecycle is not PullRequestLifecycle.open:
        return False
    return _retained_hosted_adoption_identity_is_complete_and_consistent(
        source,
        prefetched_feature_pr,
    )


def _raise_if_hosted_delegation_unconfigured_for_retry(settings: Settings) -> None:
    """Fail closed when hosted adoption retry lacks delegation configuration."""
    try:
        service_settings = resolve_service_settings(settings)
        hosted_delegation_config_from_service_settings(service_settings, required=True)
    except HostedDelegationConfigError as exc:
        raise WorkspaceHostedDelegationNotConfiguredError(
            detail=exc.detail(),
        ) from exc


def _downgrade_unqualified_hosted_adoption_to_local(task_policy: dict[str, Any]) -> None:
    """Convert hosted execution to local when hosted retry qualification failed.

    Hosted mode may only survive when ``_is_retained_open_hosted_pr_adoption_retry``
    admits the auth bypass. Falling through to local preflight while leaving
    ``execution.mode=hosted`` would provision and execute as hosted with
    incomplete/inconsistent identity and skip hosted-delegation gates.
    Closed-PR fallback still clears adoption later; this only normalizes mode.
    """
    if not pr_adoption_is_hosted(task_policy):
        return
    adoption = task_policy.get("pr_adoption")
    if not isinstance(adoption, dict):
        return
    execution = adoption.get("execution")
    updated_execution = (
        {**execution, "mode": PR_ADOPTION_EXECUTION_MODE_LOCAL}
        if isinstance(execution, dict)
        else {"mode": PR_ADOPTION_EXECUTION_MODE_LOCAL}
    )
    task_policy["pr_adoption"] = {**adoption, "execution": updated_execution}
