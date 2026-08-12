"""Checkout-isolation helpers for NEEDS_HUMAN clarification re-asks."""

from __future__ import annotations

import contextlib
import os
import re
import stat
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

from awf.common.commands import CommandResult
from awf.node.git_manager import git_env_without_object_lookup_overrides
from awf.runtime.pr_monitor_runner.comments_source_git import _reask_source_mirror_command
from awf.runtime.pr_monitor_runner.types import _MonitorPolicyBlockedError
from awf.runtime.validation_worktree_constants import VALIDATION_WORKTREE_CLEANUP_FAILED

if TYPE_CHECKING:
    from awf.runtime.pr_monitor_runner import PullRequestMonitorRunner

_ISOLATED_REASK_WORKTREE_CREATION_TIMEOUT_SECONDS = 30.0
_FILTER_DRIVER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_FILTER_ATTRIBUTE_DRIVER_RE = re.compile(r"(?<!\S)filter=([^\s]+)")
_MAX_CHECKOUT_INFO_ATTRIBUTES_BYTES = 1024 * 1024
_MAX_CHECKOUT_FILTER_TREE_OUTPUT_BYTES = 1024 * 1024
_MAX_CHECKOUT_FILTER_ATTRIBUTE_FILES = 128
_SAFE_INFO_ATTRIBUTES_DIRECTORY_OPEN_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_SAFE_INFO_ATTRIBUTES_OPEN_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK


async def _checkout_filter_overrides(
    runner: PullRequestMonitorRunner,
    *,
    worktree_path: Path,
    restore_ref: str,
    source_mirror: Path | None,
) -> tuple[str, ...]:
    """Return overrides for every driver named by pinned tracked attributes."""
    deadline = time.monotonic() + _ISOLATED_REASK_WORKTREE_CREATION_TIMEOUT_SECONDS

    async def _run_filter_discovery_command(command: list[str]) -> CommandResult:
        """Run one discovery command within the shared setup deadline."""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _MonitorPolicyBlockedError(
                "Could not read tracked checkout filters before the NEEDS_HUMAN reason re-ask.",
                reason_code=VALIDATION_WORKTREE_CLEANUP_FAILED,
            )
        result = await runner._deps.runner.run(
            command,
            timeout_seconds=remaining,
            env=git_env_without_object_lookup_overrides(),
        )
        if not result.ok or time.monotonic() >= deadline:
            raise _MonitorPolicyBlockedError(
                "Could not read tracked checkout filters before the NEEDS_HUMAN reason re-ask.",
                reason_code=VALIDATION_WORKTREE_CLEANUP_FAILED,
            )
        return result

    attribute_paths = await _run_filter_discovery_command(
        _reask_source_mirror_command(
            worktree_path,
            source_mirror,
            "ls-tree",
            "--full-tree",
            "-r",
            "-z",
            "--name-only",
            restore_ref,
        )
    )
    if len(attribute_paths.stdout.encode("utf-8")) > _MAX_CHECKOUT_FILTER_TREE_OUTPUT_BYTES:
        raise _MonitorPolicyBlockedError(
            "Could not read tracked checkout filters before the NEEDS_HUMAN reason re-ask.",
            reason_code=VALIDATION_WORKTREE_CLEANUP_FAILED,
        )

    tracked_attribute_paths = tuple(
        attribute_path
        for attribute_path in attribute_paths.stdout.split("\0")
        if attribute_path == ".gitattributes" or attribute_path.endswith("/.gitattributes")
    )
    if len(tracked_attribute_paths) > _MAX_CHECKOUT_FILTER_ATTRIBUTE_FILES:
        raise _MonitorPolicyBlockedError(
            "Could not read tracked checkout filters before the NEEDS_HUMAN reason re-ask.",
            reason_code=VALIDATION_WORKTREE_CLEANUP_FAILED,
        )

    driver_names: set[str] = set()
    for attribute_path in tracked_attribute_paths:
        attributes = await _run_filter_discovery_command(
            _reask_source_mirror_command(
                worktree_path,
                source_mirror,
                "show",
                f"{restore_ref}:{attribute_path}",
            )
        )
        driver_names.update(_checkout_filter_driver_names(attributes.stdout))

    return _checkout_filter_driver_overrides(driver_names)


def _checkout_info_attributes_filter_overrides(
    *,
    source_mirror: Path | None,
    source_worktree_path: Path,
) -> tuple[str, ...]:
    """Return overrides for drivers named by the mutable Git info attributes file."""
    git_metadata_path = source_mirror or source_worktree_path / ".git"
    attributes = _read_checkout_info_attributes(git_metadata_path)
    return _checkout_filter_driver_overrides(_checkout_filter_driver_names(attributes))


def _read_checkout_info_attributes(git_metadata_path: Path) -> str:
    """Read mutable ``info/attributes`` without following links or special files."""
    try:
        info_fd = os.open(
            git_metadata_path / "info",
            _SAFE_INFO_ATTRIBUTES_DIRECTORY_OPEN_FLAGS,
        )
    except FileNotFoundError:
        return ""
    except OSError as exc:
        raise _MonitorPolicyBlockedError(
            "Could not safely read checkout info attributes before the NEEDS_HUMAN reason re-ask.",
            reason_code=VALIDATION_WORKTREE_CLEANUP_FAILED,
        ) from exc

    try:
        try:
            attributes_fd = os.open(
                "attributes",
                _SAFE_INFO_ATTRIBUTES_OPEN_FLAGS,
                dir_fd=info_fd,
            )
        except FileNotFoundError:
            return ""
    except OSError as exc:
        raise _MonitorPolicyBlockedError(
            "Could not safely read checkout info attributes before the NEEDS_HUMAN reason re-ask.",
            reason_code=VALIDATION_WORKTREE_CLEANUP_FAILED,
        ) from exc
    finally:
        os.close(info_fd)

    try:
        attributes_stat = os.fstat(attributes_fd)
        if not stat.S_ISREG(attributes_stat.st_mode):
            raise OSError("Git info attributes is not a regular file")
        if attributes_stat.st_size > _MAX_CHECKOUT_INFO_ATTRIBUTES_BYTES:
            raise OSError("Git info attributes exceeds size limit")
        attributes_bytes = os.read(attributes_fd, _MAX_CHECKOUT_INFO_ATTRIBUTES_BYTES + 1)
        if len(attributes_bytes) > _MAX_CHECKOUT_INFO_ATTRIBUTES_BYTES:
            raise OSError("Git info attributes exceeds size limit")
        return attributes_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise _MonitorPolicyBlockedError(
            "Could not safely read checkout info attributes before the NEEDS_HUMAN reason re-ask.",
            reason_code=VALIDATION_WORKTREE_CLEANUP_FAILED,
        ) from exc
    finally:
        os.close(attributes_fd)


def _checkout_filter_driver_names(attributes: str) -> set[str]:
    """Extract and validate filter driver names from one attributes file."""
    driver_names: set[str] = set()
    for line in attributes.splitlines():
        stripped_line = line.lstrip()
        if stripped_line.startswith("#"):
            continue
        for driver_name in _FILTER_ATTRIBUTE_DRIVER_RE.findall(stripped_line):
            if _FILTER_DRIVER_NAME_RE.fullmatch(driver_name) is None:
                raise _MonitorPolicyBlockedError(
                    "Could not safely disable checkout filters before the NEEDS_HUMAN reason re-ask.",
                    reason_code=VALIDATION_WORKTREE_CLEANUP_FAILED,
                )
            driver_names.add(driver_name)
    return driver_names


def _checkout_filter_driver_overrides(driver_names: set[str]) -> tuple[str, ...]:
    """Render command-line overrides that keep filter drivers from executing."""
    return tuple(
        option
        for driver_name in sorted(driver_names)
        for option in (
            "-c",
            f"filter.{driver_name}.smudge=",
            "-c",
            f"filter.{driver_name}.process=",
            "-c",
            f"filter.{driver_name}.required=false",
        )
    )


@contextlib.contextmanager
def _isolated_reask_checkout_git_dir(
    *,
    source_mirror: Path | None,
    source_worktree_path: Path,
    restore_ref: str,
) -> Iterator[Path]:
    """Build an ephemeral Git directory that cannot inherit mutable checkout settings."""
    source_git_metadata_path = source_mirror or source_worktree_path / ".git"
    try:
        with tempfile.TemporaryDirectory(prefix="awf-isolated-reask-checkout-") as temporary_dir:
            git_dir = Path(temporary_dir)
            (git_dir / "refs").mkdir()
            (git_dir / "info").mkdir()
            (git_dir / "objects").symlink_to(source_git_metadata_path / "objects")
            (git_dir / "HEAD").write_text(f"{restore_ref}\n", encoding="utf-8")
            config = "[core]\nrepositoryformatversion = 0\nfilemode = true\nbare = false\n"
            if len(restore_ref) == 64:
                config = (
                    "[core]\nrepositoryformatversion = 1\nfilemode = true\nbare = false\n"
                    "[extensions]\nobjectformat = sha256\n"
                )
            (git_dir / "config").write_text(
                config,
                encoding="utf-8",
            )
            yield git_dir
    except OSError as exc:
        raise _MonitorPolicyBlockedError(
            "Could not isolate Git checkout settings before the NEEDS_HUMAN reason re-ask.",
            reason_code=VALIDATION_WORKTREE_CLEANUP_FAILED,
        ) from exc
