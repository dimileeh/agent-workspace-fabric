"""Result types for validation worktree cleanliness checks and cleanup.

Split out of ``validation_worktree`` (which re-exports both) to keep that
module under the first-party line budget.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ValidationWorktreeCheck:
    """Result payload describing whether the validation worktree is clean."""

    clean: bool
    skipped: bool = False
    paths: tuple[str, ...] = ()
    untracked_paths: tuple[str, ...] = ()
    ignored_paths: tuple[str, ...] = ()
    reason_code: str | None = None
    message: str = ""
    command_stderr: str = ""

    @property
    def tracked_paths(self) -> tuple[str, ...]:
        """Return changed tracked paths, excluding any untracked entries."""
        untracked = set(self.untracked_paths)
        return tuple(path for path in self.paths if path not in untracked)

    def details(self) -> dict[str, object]:
        """Serialize check metadata for structured validation evidence."""
        details: dict[str, object] = {
            "paths": list(self.paths),
            "untracked_paths": list(self.untracked_paths),
            "ignored_paths": list(self.ignored_paths),
        }
        if self.reason_code is not None:
            details["reason_code"] = self.reason_code
        if self.command_stderr:
            details["command_stderr"] = self.command_stderr
        return details


@dataclass(frozen=True)
class ValidationWorktreeCleanup:
    """Result payload describing a validation-worktree cleanup attempt."""

    cleaned: bool
    check: ValidationWorktreeCheck
    restore_ref: str | None = None
    reason_code: str | None = None
    message: str = ""
    cleanup_command: str | None = None
    cleanup_stderr: str = ""
    verify_check: ValidationWorktreeCheck | None = None
    cleaned_paths: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """Return whether cleanup completed successfully."""
        return self.reason_code is None

    @property
    def side_effect_paths(self) -> tuple[str, ...]:
        """Return paths that prove validation left worktree side effects."""
        if self.cleaned_paths:
            return self.cleaned_paths
        if self.check.clean:
            return ()
        return tuple(dict.fromkeys((*self.check.paths, *self.check.untracked_paths)))

    def details(self) -> dict[str, object]:
        """Serialize cleanup metadata for failure reporting and evidence."""
        details = self.check.details()
        details["restore_ref"] = self.restore_ref
        if self.cleaned_paths:
            details["cleaned_paths"] = list(self.cleaned_paths)
        if self.reason_code is not None:
            details["reason_code"] = self.reason_code
        if self.cleanup_command is not None:
            details["cleanup_command"] = self.cleanup_command
        if self.cleanup_stderr:
            details["cleanup_stderr"] = self.cleanup_stderr
        if self.verify_check is not None:
            if self.verify_check.reason_code is not None:
                details["verify_reason_code"] = self.verify_check.reason_code
            if self.verify_check.command_stderr:
                details["verify_command_stderr"] = self.verify_check.command_stderr
            if self.verify_check.paths:
                details["remaining_paths"] = list(self.verify_check.paths)
                details["remaining_untracked_paths"] = list(self.verify_check.untracked_paths)
        return details
