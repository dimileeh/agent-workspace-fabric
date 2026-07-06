"""Automatic salvage of implementation diffs after conformance failures."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from awf.common.git_identity import git_safe_directory_config_args
from awf.control.quality_gates import (
    find_protected_quality_gate_changes,
    requires_protected_file_diff,
    unowned_protected_paths,
)
from awf.control.quality_gates_common import ProtectedFileDiff
from awf.runtime.planning import build_conformance_retry_prompt
from awf.service._git_salvage_utils import (
    GIT_TIMEOUT_SECONDS,
    CompletedProcessLike,
    SubprocessRun,
)
from awf.service._git_salvage_utils import (
    paths_from_name_status as _paths_from_name_status,
)
from awf.service._git_salvage_utils import (
    run_git as _run_git_shared,
)

CONFORMANCE_SALVAGE_POLICY_KEY = "conformance_salvage"
INTERNAL_PLAN_ARTIFACT_PREFIX = "docs/awf-plans/"

SALVAGE_SOURCE_UNAVAILABLE = "SALVAGE_SOURCE_UNAVAILABLE"
SALVAGE_BASE_UNAVAILABLE = "SALVAGE_BASE_UNAVAILABLE"
SALVAGE_NO_IMPLEMENTATION_DIFF = "SALVAGE_NO_IMPLEMENTATION_DIFF"
SALVAGE_PATCH_UNAVAILABLE = "SALVAGE_PATCH_UNAVAILABLE"
SALVAGE_PATCH_DIGEST_MISMATCH = "SALVAGE_PATCH_DIGEST_MISMATCH"
SALVAGE_PATCH_APPLY_FAILED = "SALVAGE_PATCH_APPLY_FAILED"

CONFORMANCE_SALVAGE_APPLIED_EVENT_TYPE = "workspace.conformance_salvage_applied"
CONFORMANCE_SALVAGE_APPLIED_REASON = "CONFORMANCE_SALVAGE_APPLIED"
CONFORMANCE_SALVAGE_CONFLICT_EVENT_TYPE = "workspace.conformance_salvage_conflict"
CONFORMANCE_SALVAGE_CONFLICT_REASON = "CONFORMANCE_SALVAGE_CONFLICT"


@dataclass(frozen=True)
class ConformanceSalvageCapture:
    """Captures a salvage operation's metadata and artifact location."""

    source_workspace_id: str
    source_base_commit: str
    patch_path: Path
    patch_sha256: str
    patch_bytes: int
    implementation_paths: list[str]
    plan_artifact_paths: list[str]
    remaining_gaps: list[str]
    conformance_evidence_ref: dict[str, str] | None
    source_branch_name: str | None
    source_remote_push_branch: str | None
    created_at: str
    plan_path: str | None = None
    report_path: str | None = None
    quarantined_protected_paths: list[str] = field(default_factory=list)

    def as_policy(self) -> dict[str, Any]:
        """Convert capture metadata into a plan-policy payload."""
        payload: dict[str, Any] = {
            "status": "captured",
            "source_workspace_id": self.source_workspace_id,
            "source_base_commit": self.source_base_commit,
            "patch_path": str(self.patch_path),
            "patch_sha256": self.patch_sha256,
            "patch_bytes": self.patch_bytes,
            "implementation_paths": self.implementation_paths,
            "plan_artifact_paths": self.plan_artifact_paths,
            "remaining_gaps": self.remaining_gaps,
            "conformance_evidence_ref": self.conformance_evidence_ref,
            "created_at": self.created_at,
        }
        if self.source_branch_name:
            payload["source_branch_name"] = self.source_branch_name
        if self.source_remote_push_branch:
            payload["source_remote_push_branch"] = self.source_remote_push_branch
        if self.plan_path:
            payload["plan_path"] = self.plan_path
        if self.report_path:
            payload["report_path"] = self.report_path
        if self.quarantined_protected_paths:
            payload["quarantined_protected_paths"] = self.quarantined_protected_paths
        return payload


class ConformanceSalvageError(Exception):
    """Raised when salvage capture or validation fails."""

    def __init__(
        self,
        *,
        reason_code: str,
        message: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        """Initialise an error with machine-readable salvage metadata."""
        self.reason_code = reason_code
        self.detail = detail or {}
        super().__init__(message)


def capture_conformance_salvage(
    *,
    work_dir: str | Path,
    source_workspace_id: str,
    source_base_commit: str | None,
    conformance_evidence: Mapping[str, Any] | None,
    conformance_evidence_ref: dict[str, str] | None,
    source_branch_name: str | None,
    source_remote_push_branch: str | None,
    owned_paths: Sequence[str] = (),
    run_subprocess: SubprocessRun | None = None,
) -> ConformanceSalvageCapture:
    """Capture a salvage patch and metadata from a failed workspace run.

    ``owned_paths`` is the source attempt's declared ownership. Any changed
    protected quality-gate file NOT covered by it (e.g. an ``.awf/workspace.yml``
    edit that caused a block) is quarantined: excluded from both the recorded
    implementation paths and the re-applied binary patch, so a retry cannot
    replay the exact protected change that caused the block (#743).
    """
    if not source_base_commit:
        raise ConformanceSalvageError(
            reason_code=SALVAGE_BASE_UNAVAILABLE,
            message="Source workspace has no recorded base commit for salvage.",
        )

    root = Path(work_dir)
    source_worktree = root / "git" / "worktrees" / source_workspace_id
    if not source_worktree.is_dir():
        raise ConformanceSalvageError(
            reason_code=SALVAGE_SOURCE_UNAVAILABLE,
            message="Source workspace worktree is unavailable for salvage.",
            detail={"worktree_path": str(source_worktree)},
        )

    run = run_subprocess or subprocess.run
    env_base = dict(os.environ)

    _run_git(
        source_worktree,
        ["cat-file", "-e", f"{source_base_commit}^{{commit}}"],
        run=run,
        env=env_base,
        failure_reason=SALVAGE_BASE_UNAVAILABLE,
    )

    artifacts_dir = root / "artifacts" / "salvage"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="awf-salvage-index-") as tmp_dir:
        index_path = str(Path(tmp_dir) / "index")
        git_env = {**env_base, "GIT_INDEX_FILE": index_path}
        _run_git(source_worktree, ["read-tree", "HEAD"], run=run, env=git_env)
        _run_git(source_worktree, ["add", "-A", "--", "."], run=run, env=git_env)
        # Build the changed-path set from rename-aware ``--name-status -z`` so a
        # renamed protected source (e.g. ``git mv .awf/workspace.yml
        # sub/workspace.yml``) appears on BOTH sides. ``--name-only`` would only
        # record the destination, so the quarantine set built from it would miss
        # the protected source and the binary patch below would still replay the
        # protected rename into the retry workspace (#PRRT_kwDOSJAM6s6Og3Be).
        name_status_args_common: list[str] = [
            "diff",
            "--cached",
            "--name-status",
            "-z",
            source_base_commit,
            "--",
        ]
        name_status_impl_stdout = _run_git(
            source_worktree,
            [
                *name_status_args_common,
                ".",
                f":(exclude){INTERNAL_PLAN_ARTIFACT_PREFIX}**",
            ],
            run=run,
            env=git_env,
        ).stdout
        all_implementation_paths = _paths_from_name_status(name_status_impl_stdout)
        plan_artifact_paths = _paths_from_name_status(
            _run_git(
                source_worktree,
                [
                    *name_status_args_common,
                    INTERNAL_PLAN_ARTIFACT_PREFIX,
                ],
                run=run,
                env=git_env,
            ).stdout
        )
        # Quarantine the changed protected quality-gate files the source attempt
        # did not own AND that the protected gate would actually block. Dropping
        # them from BOTH the recorded implementation paths and the binary patch
        # below is what stops a retry from replaying the exact protected change
        # (e.g. the `.awf/workspace.yml` edit) that caused the block (#743). The
        # name list alone is cosmetic; the patch exclusion is load-bearing, and
        # both use the same quarantine set so they can't drift.
        #
        # A conformance/timeout failure is NOT necessarily a protected-file
        # block: the gate may have already classified the agent's unowned
        # protected edit as safe (e.g. a ``pyproject.toml`` dependency addition
        # or a pinned workflow-action bump) and let the workspace proceed past
        # it before failing for an unrelated conformance/timeout reason. Such a
        # safe edit must NOT be quarantined — the retry needs the dependency/CI
        # update the agent made, or the salvaged implementation diff will not
        # build or pass. So before excluding anything from the patch, re-run the
        # gate's own classifier over the unowned protected paths and keep only
        # the ones that produce real violations. The classifier is the same one
        # the gate runs at commit time, so the salvage can never quarantine a
        # path the gate would have permitted (#PRRT_kwDOSJAM6s6OhM1p).
        candidate_protected_paths = list(
            unowned_protected_paths(all_implementation_paths, owned_paths=owned_paths)
        )
        quarantined_protected_paths = _quarantine_blocking_protected_paths(
            candidate_protected_paths,
            source_worktree=source_worktree,
            source_base_commit=source_base_commit,
            run=run,
            git_env=git_env,
            env_base=env_base,
        )
        quarantined_set = set(quarantined_protected_paths)
        # A rename whose protected source is quarantined also has a
        # non-protected destination that must be excluded from the binary patch
        # (otherwise ``--no-renames`` below emits it as an unrelated add). Pair
        # the destination back in so both sides are dropped (#PRRT_kwDOSJAM6s6Og3Be).
        rename_partner_paths = _rename_partners(name_status_impl_stdout, quarantined_set)
        for path in rename_partner_paths:
            if path not in quarantined_set:
                quarantined_protected_paths.append(path)
                quarantined_set.add(path)
        implementation_paths = [
            path for path in all_implementation_paths if path not in quarantined_set
        ]
        if not implementation_paths:
            raise ConformanceSalvageError(
                reason_code=SALVAGE_NO_IMPLEMENTATION_DIFF,
                message=(
                    "Conformance retry found no salvageable implementation diff; only "
                    "AWF plan/conformance artifacts or quarantined protected "
                    "quality-gate files changed."
                ),
                detail={
                    "plan_artifact_paths": plan_artifact_paths,
                    "quarantined_protected_paths": quarantined_protected_paths,
                },
            )
        exclude_pathspecs = [f":(exclude){INTERNAL_PLAN_ARTIFACT_PREFIX}**"] + [
            f":(exclude){path}" for path in quarantined_protected_paths
        ]
        # ``--no-renames`` forces git to treat a rename as a delete+add pair, so
        # excluding the quarantined source AND destination pathspecs drops both
        # halves of a renamed protected file. With rename detection on, excluding
        # only the source would still emit the destination as an unrelated add,
        # and excluding only the destination would still emit the source deletion
        # — replaying the protected edit either way (#PRRT_kwDOSJAM6s6Og3Be).
        patch = _run_git(
            source_worktree,
            [
                "diff",
                "--cached",
                "--binary",
                "--no-renames",
                source_base_commit,
                "--",
                ".",
                *exclude_pathspecs,
            ],
            run=run,
            env=git_env,
        ).stdout.encode("utf-8")

    patch_sha256 = hashlib.sha256(patch).hexdigest()
    patch_path = artifacts_dir / f"{source_workspace_id}-{patch_sha256[:12]}.patch"
    patch_path.write_bytes(patch)

    evidence = conformance_evidence or {}
    capture = ConformanceSalvageCapture(
        source_workspace_id=source_workspace_id,
        source_base_commit=source_base_commit,
        patch_path=patch_path,
        patch_sha256=patch_sha256,
        patch_bytes=len(patch),
        implementation_paths=implementation_paths,
        plan_artifact_paths=plan_artifact_paths,
        remaining_gaps=_evidence_gaps(evidence),
        conformance_evidence_ref=conformance_evidence_ref,
        source_branch_name=source_branch_name,
        source_remote_push_branch=source_remote_push_branch,
        created_at=datetime.now(UTC).isoformat(),
        plan_path=_optional_str(evidence.get("plan_path")),
        report_path=_optional_str(evidence.get("report_path")),
        quarantined_protected_paths=quarantined_protected_paths,
    )
    metadata_path = patch_path.with_suffix(".json")
    metadata_path.write_text(
        json.dumps(capture.as_policy(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return capture


def build_conformance_salvage_retry_prompt(
    *,
    task_prompt: str,
    evidence: Mapping[str, Any],
    salvage: Mapping[str, Any],
) -> str:
    """Build a retry prompt that replays recovered conformance implementation diffs."""
    paths = _implementation_path_lines(salvage)
    quarantined = salvage.get("quarantined_protected_paths") or []
    quarantine_section = ""
    if quarantined:
        quarantine_lines = "\n".join(f"- `{path}`" for path in quarantined)
        quarantine_section = (
            "### Quarantined protected quality-gate edits\n"
            "AWF intentionally DROPPED the following protected quality-gate file "
            "edit(s) from the salvage because the prior attempt did not own them — "
            "they caused a protected-file block. Do NOT re-create these edits. If a "
            "policy change is genuinely required, ask the operator to grant explicit "
            "ownership of the file instead of editing it yourself:\n"
            f"{quarantine_lines}\n\n"
        )
    return (
        "## Automatic AWF salvage\n\n"
        "AWF automatically captured the prior implementation diff from the failed "
        "conformance attempt. The retry workspace will restore that diff before "
        "the agent runs. Continue from the recovered implementation; do not "
        "restart from scratch unless the recovered code is unusable.\n\n"
        f"### Salvaged implementation paths\n{paths}\n\n"
        + quarantine_section
        + build_conformance_retry_prompt(task_prompt=task_prompt, evidence=evidence)
    )


def build_agent_timeout_salvage_retry_prompt(
    *,
    task_prompt: str,
    evidence: Mapping[str, Any],
    salvage: Mapping[str, Any],
) -> str:
    """Build a retry prompt for timeout recovery with recovered implementation diff."""
    reason_code = _optional_str(evidence.get("reason_code")) or "AGENT_IDLE_TIMEOUT"
    message = _optional_str(evidence.get("message"))
    message_line = f"\n\n### Timeout message\n{message[:1000]}" if message else ""
    return (
        "## Automatic AWF timeout salvage\n\n"
        "AWF automatically captured the prior implementation diff from an agent "
        "run that timed out before it could finish. The retry workspace will "
        "restore that diff before the agent runs. Continue from the recovered "
        "implementation; do not restart from scratch unless the recovered code "
        "is unusable.\n\n"
        f"### Source reason\n`{reason_code}`{message_line}\n\n"
        f"### Salvaged implementation paths\n{_implementation_path_lines(salvage)}\n\n"
        f"### Original task\n{task_prompt}\n"
    )


def build_conformance_salvage_conflict_prompt(
    *,
    task_prompt: str,
    salvage: Mapping[str, Any],
    agent_patch_path: str,
    apply_error: str,
) -> str:
    """Build a prompt that guides a conflict-resolution retry attempt."""
    gaps = _string_list(salvage.get("remaining_gaps"))
    gap_lines = "\n".join(f"- {gap}" for gap in gaps) or "- Re-check conformance evidence."
    path_lines = _implementation_path_lines(salvage)
    return (
        "## Automatic AWF salvage conflict\n\n"
        "AWF captured the prior implementation diff, but it could not be applied "
        "cleanly to this fresh retry workspace. This is a self-recovery task: "
        "use the salvage patch as source material, resolve the conflict against "
        "the current base, and finish the original conformance gaps.\n\n"
        f"### Salvage patch\n`{agent_patch_path}`\n\n"
        f"### Salvaged implementation paths\n{path_lines}\n\n"
        f"### Remaining conformance gaps\n{gap_lines}\n\n"
        f"### Apply error\n{apply_error[:2000] or 'git apply --check failed.'}\n\n"
        f"### Original task\n{task_prompt}\n"
    )


def conformance_salvage_from_task_policy(
    task_policy: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Extract conformance-salvage metadata from a task policy blob."""
    if not isinstance(task_policy, Mapping):
        return None
    value = task_policy.get(CONFORMANCE_SALVAGE_POLICY_KEY)
    return dict(value) if isinstance(value, Mapping) else None


def _run_git(
    worktree: Path,
    args: list[str],
    *,
    run: SubprocessRun,
    env: Mapping[str, str],
    failure_reason: str = SALVAGE_SOURCE_UNAVAILABLE,
) -> CompletedProcessLike:
    """Run a git command for salvage with deterministic timeout and failure mapping."""
    return _run_git_shared(
        worktree,
        args,
        run=run,
        env=env,
        raise_error=ConformanceSalvageError,
        failure_reason=failure_reason,
        failure_context="conformance salvage",
    )


def _run_git_nocheck(
    worktree: Path,
    args: list[str],
    *,
    run: SubprocessRun,
    env: Mapping[str, str],
) -> CompletedProcessLike:
    """Run a git command for salvage WITHOUT raising on a nonzero exit.

    Used for ``git cat-file -e <refspec>`` probes where a nonzero exit means a
    path is absent (a normal case for a newly-added protected file), not a
    salvage failure. The shared ``_run_git`` raises on nonzero exit, which would
    conflate a missing path with a real git error.
    """
    return run(
        ["git", *git_safe_directory_config_args(worktree), "-C", str(worktree), *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_SECONDS,
        env=env,
    )


def _evidence_gaps(evidence: Mapping[str, Any]) -> list[str]:
    """Convert salvage evidence gaps into a stable list of non-empty strings."""
    value = evidence.get("gaps")
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _string_list(value: object) -> list[str]:
    """Return a filtered list of strings, dropping non-string and empty values."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _implementation_path_lines(salvage: Mapping[str, Any]) -> str:
    """Format captured implementation paths as prompt bullet lines with truncation."""
    implementation_paths = _string_list(salvage.get("implementation_paths"))
    paths = "\n".join(f"- `{path}`" for path in implementation_paths[:20])
    if len(implementation_paths) > 20:
        paths += f"\n- ... and {len(implementation_paths) - 20} more"
    return paths or "- No paths recorded."


def _optional_str(value: object) -> str | None:
    """Normalize an optional string value to ``str`` or ``None``."""
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _rename_partners(
    name_status_z_stdout: str,
    quarantined_set: set[str],
) -> list[str]:
    """Return rename destinations whose source is quarantined, and vice versa.

    Parses ``git diff --name-status -z`` output to find rename/copy records
    (status ``R``/``C``) where one side is in ``quarantined_set`` and returns the
    other side so the binary patch exclusion can drop both halves of the
    protected rename (#PRRT_kwDOSJAM6s6Og3Be). Destinations are appended in
    first-seen order.
    """
    if not name_status_z_stdout or not quarantined_set:
        return []
    if "\0" not in name_status_z_stdout:
        return []
    fields = name_status_z_stdout.split("\0")
    if not fields or fields[-1] != "":
        return []
    fields = fields[:-1]
    partners: list[str] = []
    seen: set[str] = set()
    index = 0
    while index < len(fields):
        status = fields[index]
        index += 1
        if not status:
            continue
        path_count = 2 if status.startswith(("R", "C")) else 1
        if index + path_count > len(fields):
            break
        if path_count == 2:
            source = fields[index]
            destination = fields[index + 1]
            if source in quarantined_set and destination not in seen:
                partners.append(destination)
                seen.add(destination)
            elif destination in quarantined_set and source not in seen:
                partners.append(source)
                seen.add(source)
        index += path_count
    return partners


def _quarantine_blocking_protected_paths(
    candidate_paths: Sequence[str],
    *,
    source_worktree: Path,
    source_base_commit: str,
    run: SubprocessRun,
    git_env: Mapping[str, str],
    env_base: Mapping[str, str],
) -> list[str]:
    """Return the unowned protected paths the gate would actually block.

    Re-runs the protected quality-gate classifier (the same one the executor
    runs at commit time) over ``candidate_paths`` so a conformance/timeout
    retry does not quarantine an unowned protected edit the gate already
    classified as safe (e.g. a ``pyproject.toml`` dependency addition or a
    pinned workflow-action bump). Only paths that produce real violations are
    returned; the rest stay in the salvage patch so the retry keeps the
    dependency/CI update the agent made (#PRRT_kwDOSJAM6s6OhM1p).

    Paths that do not require diff classification (anything other than
    ``pyproject.toml`` and workflow YAML) are always violations when unowned,
    mirroring :func:`find_protected_quality_gate_changes` exactly, so they are
    kept without invoking the classifier.
    """
    if not candidate_paths:
        return []
    classified_paths = [path for path in candidate_paths if requires_protected_file_diff(path)]
    violation_paths: set[str] = set()
    if classified_paths:
        protected_file_diffs = _load_protected_file_diffs(
            classified_paths,
            source_worktree=source_worktree,
            source_base_commit=source_base_commit,
            run=run,
            git_env=git_env,
            env_base=env_base,
        )
        violations = find_protected_quality_gate_changes(
            changed_paths=list(protected_file_diffs.keys()),
            owned_paths=(),
            protected_file_diffs=protected_file_diffs,
        )
        violation_paths = {violation.path for violation in violations}
    quarantined: list[str] = []
    for path in candidate_paths:
        if requires_protected_file_diff(path):
            if path in violation_paths:
                quarantined.append(path)
        else:
            quarantined.append(path)
    return quarantined


def _load_protected_file_diffs(
    paths: Sequence[str],
    *,
    source_worktree: Path,
    source_base_commit: str,
    run: SubprocessRun,
    git_env: Mapping[str, str],
    env_base: Mapping[str, str],
) -> dict[str, ProtectedFileDiff]:
    """Load old/new text for protected files using the salvage temp index.

    ``:<path>`` reads the agent's staged content from the temp index (the same
    index the patch is built from), and ``<source_base_commit>:<path>`` reads
    the base content. This mirrors the executor's
    ``_protected_file_diffs_for_staged_paths`` so the classifier sees the same
    inputs the commit-time gate saw.
    """
    diffs: dict[str, ProtectedFileDiff] = {}
    for path in paths:
        old_text = _git_show_text(
            source_worktree,
            refspec=f"{source_base_commit}:{path}",
            run=run,
            env=env_base,
            failure_reason=SALVAGE_BASE_UNAVAILABLE,
        )
        new_text = _git_show_text(
            source_worktree,
            refspec=f":{path}",
            run=run,
            env=git_env,
            failure_reason=SALVAGE_SOURCE_UNAVAILABLE,
        )
        diffs[path] = ProtectedFileDiff(path=path, old_text=old_text, new_text=new_text)
    return diffs


def _git_show_text(
    worktree: Path,
    *,
    refspec: str,
    run: SubprocessRun,
    env: Mapping[str, str],
    failure_reason: str,
) -> str | None:
    """Return ``git show <refspec>`` text, treating a missing path as ``None``.

    Mirrors the async ``git_show_text`` contract used by the commit-time gate:
    ``git cat-file -e <refspec>`` decides whether the object exists (exit 0) or
    the path is absent (nonzero), and ``git show <refspec>`` returns the content
    only when it exists. A missing path (e.g. a newly-added protected file) maps
    to ``None`` so the classifier can treat it as absent content.
    """
    probe = _run_git_nocheck(
        worktree,
        ["cat-file", "-e", refspec],
        run=run,
        env=env,
    )
    if probe.returncode != 0:
        return None
    show = _run_git(
        worktree,
        ["show", refspec],
        run=run,
        env=env,
        failure_reason=failure_reason,
    )
    return show.stdout
