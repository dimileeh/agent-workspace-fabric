"""Pre-commit output parsing for PR monitor autofix retries."""

from __future__ import annotations

import re

from awf.common.commands import CommandResult

_PRE_COMMIT_AUTOFIX_MARKER = "files were modified by this hook"
_AWF_RUFF_FORMAT_CHECK_HOOK_ID = "awf-ruff-format-check"
_PRE_COMMIT_DETERMINISTIC_REPAIR_HOOK_IDS = frozenset(
    {
        "trailing-whitespace",
        "end-of-file-fixer",
        _AWF_RUFF_FORMAT_CHECK_HOOK_ID,
    }
)
_PRE_COMMIT_HOOK_ID_PATTERN = re.compile(r"^-\s*hook id:\s*(?P<hook_id>\S+)", re.MULTILINE)
_PRE_COMMIT_WOULD_REFORMAT_PATTERN = re.compile(r"^Would reformat:\s*(?P<path>\S.*)$", re.MULTILINE)
_PRE_COMMIT_FIXING_PATH_PATTERN = re.compile(r"^Fixing\s+(?P<path>\S.*)$", re.MULTILINE)


def monitor_precommit_autofix_repair_paths(commit_result: CommandResult) -> tuple[str, ...]:
    """Return deterministic hook repair paths eligible for a monitor commit retry."""
    output = f"{commit_result.stdout or ''}\n{commit_result.stderr or ''}"
    if _PRE_COMMIT_AUTOFIX_MARKER not in output:
        return ()

    failed_hooks = tuple(
        dict.fromkeys(
            match.group("hook_id") for match in _PRE_COMMIT_HOOK_ID_PATTERN.finditer(output)
        )
    )
    deterministic_hooks = tuple(
        hook for hook in failed_hooks if hook in _PRE_COMMIT_DETERMINISTIC_REPAIR_HOOK_IDS
    )
    semantic_hooks = tuple(
        hook for hook in failed_hooks if hook not in _PRE_COMMIT_DETERMINISTIC_REPAIR_HOOK_IDS
    )
    if semantic_hooks or not deterministic_hooks:
        return ()

    normalizer_repair_files = tuple(
        dict.fromkeys(
            match.group("path").strip()
            for match in _PRE_COMMIT_FIXING_PATH_PATTERN.finditer(output)
        )
    )
    format_repair_files = tuple(
        match.group("path").strip() for match in _PRE_COMMIT_WOULD_REFORMAT_PATTERN.finditer(output)
    )
    return tuple(dict.fromkeys((*normalizer_repair_files, *format_repair_files)))
