"""Pre-commit output parsing for PR monitor autofix retries."""

from __future__ import annotations

import re

from awf.common.commands import CommandResult

_PRE_COMMIT_AUTOFIX_MARKER = "files were modified by this hook"
# Exact hook IDs only: custom deterministic wrappers around these normalizers
# must add their own IDs here to opt in without widening to semantic fixers.
_PRE_COMMIT_DETERMINISTIC_REPAIR_HOOK_IDS = frozenset(
    {
        "trailing-whitespace",
        "end-of-file-fixer",
    }
)
_PRE_COMMIT_HOOK_ID_PATTERN = re.compile(r"^-\s*hook id:\s*(?P<hook_id>\S+)", re.MULTILINE)
_PRE_COMMIT_FIXING_PATH_PATTERN = re.compile(r"^Fixing\s+(?P<path>\S.*)$", re.MULTILINE)


def monitor_precommit_autofix_repair_paths(commit_result: CommandResult) -> tuple[str, ...]:
    """Return deterministic hook repair paths eligible for a monitor commit retry."""
    output = f"{commit_result.stdout or ''}\n{commit_result.stderr or ''}"
    if _PRE_COMMIT_AUTOFIX_MARKER not in output:
        return ()

    hook_matches = tuple(_PRE_COMMIT_HOOK_ID_PATTERN.finditer(output))
    repair_paths: list[str] = []
    for index, hook_match in enumerate(hook_matches):
        hook_id = hook_match.group("hook_id")
        if hook_id not in _PRE_COMMIT_DETERMINISTIC_REPAIR_HOOK_IDS:
            continue

        next_hook_start = (
            hook_matches[index + 1].start() if index + 1 < len(hook_matches) else len(output)
        )
        hook_output = output[hook_match.start() : next_hook_start]
        if _PRE_COMMIT_AUTOFIX_MARKER not in hook_output:
            continue

        repair_paths.extend(
            match.group("path").strip()
            for match in _PRE_COMMIT_FIXING_PATH_PATTERN.finditer(hook_output)
        )

    return tuple(dict.fromkeys(repair_paths))
