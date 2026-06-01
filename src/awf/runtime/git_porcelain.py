"""Shared parsers for Git porcelain path output."""

from __future__ import annotations

_PORCELAIN_C_ESCAPES = {
    "a": 0x07,
    "b": 0x08,
    "t": 0x09,
    "n": 0x0A,
    "v": 0x0B,
    "f": 0x0C,
    "r": 0x0D,
    '"': 0x22,
    "\\": 0x5C,
}


def unquote_porcelain_path(path: str) -> str:
    """Decode Git's C-quoted porcelain path form when present."""
    if len(path) < 2 or path[0] != '"' or path[-1] != '"':
        return path

    raw = bytearray()
    end = len(path) - 1
    i = 1
    while i < end:
        char = path[i]
        if char != "\\":
            raw.extend(char.encode("utf-8", "surrogateescape"))
            i += 1
            continue

        i += 1
        if i >= end:
            raw.append(ord("\\"))
            break

        escaped = path[i]
        if escaped in _PORCELAIN_C_ESCAPES:
            raw.append(_PORCELAIN_C_ESCAPES[escaped])
            i += 1
            continue

        if "0" <= escaped <= "7":
            j = i + 1
            while j < end and j < i + 3 and "0" <= path[j] <= "7":
                j += 1
            raw.append(int(path[i:j], 8))
            i = j
            continue

        raw.extend(escaped.encode("utf-8", "surrogateescape"))
        i += 1

    return bytes(raw).decode("utf-8", "surrogateescape")


def split_porcelain_rename_paths(path: str) -> tuple[str, str] | None:
    """Split porcelain rename paths on the separator outside C-quoted paths."""
    in_quote = False
    escaped = False
    for index, char in enumerate(path):
        if in_quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_quote = False
            continue

        if char == '"':
            in_quote = True
            continue

        if path.startswith(" -> ", index):
            return path[:index], path[index + 4 :]

    return None


def changed_paths_from_porcelain(status_stdout: str) -> tuple[str, ...]:
    """Extract changed paths from ``git status --porcelain`` output."""
    paths: list[str] = []
    for line in status_stdout.splitlines():
        if not line:
            continue
        if line.startswith("?? ") or (len(line) >= 4 and line[2] == " "):
            status = line[:2]
            path = line[3:]
        else:
            continue

        rename_paths = (
            split_porcelain_rename_paths(path)
            if status[:1] in {"R", "C"} or status[1:2] in {"R", "C"}
            else None
        )
        if rename_paths:
            old_path, new_path = rename_paths
            paths.extend(
                [
                    unquote_porcelain_path(old_path),
                    unquote_porcelain_path(new_path),
                ]
            )
        else:
            paths.append(unquote_porcelain_path(path))
    return tuple(dict.fromkeys(paths))


def untracked_paths_from_porcelain(status_stdout: str) -> tuple[str, ...]:
    """Extract untracked or ignored paths from ``git status --porcelain`` output."""
    paths: list[str] = []
    for line in status_stdout.splitlines():
        if not (line.startswith("?? ") or line.startswith("!! ")):
            continue
        paths.append(unquote_porcelain_path(line[3:]))
    return tuple(dict.fromkeys(paths))
