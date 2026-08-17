"""Disabling-context, control-flow, and git blob helpers for salvage presence."""

from __future__ import annotations

import re

from awf.runtime.pr_monitor_runner.helpers_verdict import (
    _ascii_double_quote_is_delimiter,
    _ascii_single_quote_is_delimiter,
)

# C/JS statement headers and Python suite headers that make the following line
# their body. Used so suffix retention rejects ``if (false)\n`` / ``if False:\n``
# prefixes that keep an added salvage call as an exact suffix while disabling it
# (PRRT_kwDOSJAM6s6ZtJG5).
_CONTROL_FLOW_PAREN_HEADER_START_RE = re.compile(
    r"^[ \t]*(?:else\s+if|if|while|for|switch|catch)\b"
)
_CONTROL_FLOW_BARE_HEADER_RE = re.compile(
    r"^[ \t]*(?:else|do|try|finally)[ \t]*(?:\{[ \t]*)?(?://.*)?$"
)
# Idiomatic ``} else`` / ``} catch`` / ``} finally`` / ``} else if`` place the
# header after a closing brace rather than at column zero
# (PRRT_kwDOSJAM6s6ZtYk1). Same-line ``/* … */`` between ``}`` and the keyword is
# still a continuation (PRRT_kwDOSJAM6s6Zt56f). ``} while`` is excluded —
# do-while terminators do not open a body for the following line. Group 1 is the
# keyword so callers can slice the header without re-skipping comments.
_CONTROL_FLOW_AFTER_BRACE_RE = re.compile(
    r"\}(?:[ \t]|/\*.*?\*/)*((?:else\s+if|else|catch|finally)\b)"
)
_CONTROL_FLOW_PAREN_KEYWORD_RE = re.compile(r"^(?:else\s+if|if|while|for|switch|catch)\b")
_CONTROL_FLOW_BARE_KEYWORD_RE = re.compile(r"^(?:else|do|try|finally)[ \t]*(?:\{[ \t]*)?(?://.*)?$")
_PYTHON_SUITE_HEADER_RE = re.compile(
    r"^[ \t]*(?:async[ \t]+)?(?:def|class)\b[^:\n]*:[ \t]*(#.*)?$"
    r"|^[ \t]*(?:if|elif|else|while|for|try|except|finally|with|match|case)\b"
    r"[^:\n]*:[ \t]*(#.*)?$"
)


def _parse_ls_tree_meta(entry: str) -> tuple[str, str, str] | None:
    """Parse ``mode type oid`` from an ``ls-tree`` metadata token."""
    mode, sep, rest = entry.partition(" ")
    if not sep:
        return None
    obj_type, sep, oid = rest.partition(" ")
    if not sep or not mode or not obj_type or not oid or " " in oid:
        return None
    return mode, obj_type, oid


def _git_mode_file_kind(mode: str) -> str:
    """Return the Git tree-entry kind encoded in ``mode``.

    Regular files share kind ``file`` whether or not the executable bit is set
    (``100644`` / ``100755``). Symlinks (``120000``) and gitlinks (``160000``)
    are distinct kinds. Unknown modes compare as themselves so mismatched
    unknowns fail closed.
    """
    if mode.startswith("100"):
        return "file"
    if mode == "120000":
        return "symlink"
    if mode == "160000":
        return "gitlink"
    return mode


def _bytes_unsafe_for_text_merge(raw: bytes) -> bool:
    """Return True when merge-file / string containment cannot be trusted.

    NUL breaks merge-file. Invalid UTF-8 collapses under ``decode(replace)`` to
    U+FFFD, so distinct invalid blobs can falsely look retained. Intentional
    U+FFFD in valid UTF-8 is fine — detect lossy decode via strict UTF-8 on
    raw bytes, not via the decoded character (PRRT_kwDOSJAM6s6ZnK_D).
    """
    if b"\0" in raw:
        return True
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


def _raw_blob_from_cat_file_result(
    *, ok: bool, stdout: str, stdout_bytes: bytes | None
) -> bytes | None:
    """Resolve cat-file blob bytes, preferring raw capture over decoded stdout.

    Without ``stdout_bytes``, intentional U+FFFD cannot be distinguished from
    ``decode(replace)`` artifacts — fail closed (``None``) when the decoded
    text contains NUL or U+FFFD.
    """
    if not ok:
        return None
    if stdout_bytes is not None:
        return stdout_bytes
    if "\0" in stdout or "\ufffd" in stdout:
        return None
    return stdout.encode("utf-8")


def _merge_file_result_matches_head(
    *, head_raw: bytes, stdout: str, stdout_bytes: bytes | None
) -> bool:
    """Return True when merge-file stdout equals the HEAD blob bytes."""
    if stdout_bytes is not None:
        return stdout_bytes == head_raw
    return stdout == head_raw.decode("utf-8")


def _prefix_leaves_open_disabling_context(prefix: str) -> bool:
    """Return True when ``prefix`` ends inside an open comment/string/#if region.

    Suffix (prepend) salvage retention must reject tips that place the salvage
    under an unterminated ``/*``, triple-quoted string, or ``#if`` / ``#ifdef`` /
    ``#ifndef`` — those keep a line-aligned suffix while disabling the fix
    (PRRT_kwDOSJAM6s6ZpaIn). Ordinary ``"..."`` / ``'...'`` strings (with ``\\``
    escapes) and ``//`` line comments are opaque so a quoted or commented
    ``/*`` token cannot open block-comment state and reject a still-valid
    salvage suffix (PRRT_kwDOSJAM6s6Zq2m_). Possessives, contractions, and inch
    marks are not string openers — otherwise they mask a later real ``/*`` /
    ``#if`` and retain salvage under a disabling region (PRRT_kwDOSJAM6s6Zq7kr).
    Hash-line bodies are still scanned for trailing ``/*`` / triple-quotes
    (``#endif /*``, ``#define X /*``), and closing a multi-line ``*/`` keeps
    line-start so a same-line ``#if`` is not missed (PRRT_kwDOSJAM6s6ZpdMC).
    Closed wrappers and plain header lines return False.
    """
    in_block_comment = False
    in_triple_double = False
    in_triple_single = False
    in_double_string = False
    in_single_string = False
    if_depth = 0
    at_line_start = True
    i = 0
    n = len(prefix)
    while i < n:
        ch = prefix[i]
        if in_block_comment:
            if ch == "*" and i + 1 < n and prefix[i + 1] == "/":
                in_block_comment = False
                i += 2
                # Keep ``at_line_start`` from a prior newline inside the comment
                # so a same-line ``#if`` after ``*/`` is still seen
                # (PRRT_kwDOSJAM6s6ZpdMC).
                continue
            if ch == "\n":
                at_line_start = True
            i += 1
            continue
        if in_triple_double:
            if prefix.startswith('"""', i):
                in_triple_double = False
                i += 3
                continue
            if ch == "\n":
                at_line_start = True
            i += 1
            continue
        if in_triple_single:
            if prefix.startswith("'''", i):
                in_triple_single = False
                i += 3
                continue
            if ch == "\n":
                at_line_start = True
            i += 1
            continue
        if in_double_string:
            if ch == "\\" and i + 1 < n:
                i += 2
                at_line_start = False
                continue
            if ch == '"' and _ascii_double_quote_is_delimiter(prefix, i, True):
                in_double_string = False
            elif ch == "\n":
                at_line_start = True
                i += 1
                continue
            else:
                at_line_start = False
            i += 1
            continue
        if in_single_string:
            if ch == "\\" and i + 1 < n:
                i += 2
                at_line_start = False
                continue
            if ch == "'" and _ascii_single_quote_is_delimiter(prefix, i, True):
                in_single_string = False
            elif ch == "\n":
                at_line_start = True
                i += 1
                continue
            else:
                at_line_start = False
            i += 1
            continue
        # ``//`` line comments are opaque: do not treat ``/*`` inside them as
        # openers (PRRT_kwDOSJAM6s6Zq2m_).
        if prefix.startswith("//", i):
            while i < n and prefix[i] != "\n":
                i += 1
            at_line_start = False
            continue
        if prefix.startswith("/*", i):
            in_block_comment = True
            i += 2
            at_line_start = False
            continue
        if prefix.startswith('"""', i):
            in_triple_double = True
            i += 3
            at_line_start = False
            continue
        if prefix.startswith("'''", i):
            in_triple_single = True
            i += 3
            at_line_start = False
            continue
        if ch == '"' and _ascii_double_quote_is_delimiter(prefix, i, False):
            in_double_string = True
            i += 1
            at_line_start = False
            continue
        if ch == "'" and _ascii_single_quote_is_delimiter(prefix, i, False):
            in_single_string = True
            i += 1
            at_line_start = False
            continue
        if at_line_start and ch == "#":
            # Skip whitespace between ``#`` and the directive keyword.
            j = i + 1
            while j < n and prefix[j] in " \t":
                j += 1
            rest = prefix[j:]
            matched_directive = False
            # Check longer ``ifdef`` / ``ifndef`` / ``endif`` before bare ``if``.
            for keyword, depth_delta in (
                ("ifdef", 1),
                ("ifndef", 1),
                ("endif", -1),
                ("if", 1),
            ):
                if not rest.startswith(keyword):
                    continue
                after = j + len(keyword)
                if after < n and (prefix[after].isalnum() or prefix[after] == "_"):
                    continue
                if depth_delta < 0:
                    if_depth = max(0, if_depth + depth_delta)
                else:
                    if_depth += depth_delta
                # Advance past the keyword; scan the rest of the line normally
                # so trailing ``/*`` / triple-quotes still open disabling context
                # (PRRT_kwDOSJAM6s6ZpdMC).
                i = after
                matched_directive = True
                break
            if not matched_directive:
                # Non-directive hash line (e.g. ``#define X /*``): skip ``#``
                # and keep scanning the body for openers.
                i += 1
            at_line_start = False
            continue
        if ch == "\n":
            at_line_start = True
            i += 1
            continue
        if not ch.isspace():
            at_line_start = False
        i += 1
    return in_block_comment or in_triple_double or in_triple_single or if_depth > 0


def _delta_brackets_outside_strings(fragment: str, *, opens: str, closes: str) -> int:
    """Net bracket delta in ``fragment``, ignoring quotes and escapes."""
    delta = 0
    in_double = False
    in_single = False
    i = 0
    n = len(fragment)
    while i < n:
        ch = fragment[i]
        if in_double or in_single:
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if (in_double and ch == '"') or (in_single and ch == "'"):
                in_double = in_single = False
            i += 1
            continue
        if ch == '"':
            in_double = True
        elif ch == "'":
            in_single = True
        elif ch == opens:
            delta += 1
        elif ch == closes:
            delta -= 1
        i += 1
    return delta


def _line_code_without_line_comment(line: str) -> str:
    """Strip ``//`` / ``#`` line comments outside simple quoted spans."""
    in_double = False
    in_single = False
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        if in_double or in_single:
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if (in_double and ch == '"') or (in_single and ch == "'"):
                in_double = in_single = False
            i += 1
            continue
        if ch == '"':
            in_double = True
        elif ch == "'":
            in_single = True
        elif line.startswith("//", i) or ch == "#":
            return line[:i]
        i += 1
    return line


def _keyword_start_after_brace_match(match: re.Match[str]) -> int:
    """Return the index of the control-flow keyword inside an after-brace match."""
    return match.start(1)


def _last_brace_control_flow_continuation(code: str) -> re.Match[str] | None:
    """Return the last ``} else`` / ``} catch`` / ``} finally`` / ``} else if`` match.

    Allows optional same-line ``/* … */`` between ``}`` and the keyword
    (PRRT_kwDOSJAM6s6Zt56f). Scans outside simple quoted spans so braces inside
    strings do not fake a continuation (PRRT_kwDOSJAM6s6ZtYk1).
    """
    last: re.Match[str] | None = None
    in_double = False
    in_single = False
    i = 0
    n = len(code)
    while i < n:
        ch = code[i]
        if in_double or in_single:
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if (in_double and ch == '"') or (in_single and ch == "'"):
                in_double = in_single = False
            i += 1
            continue
        if ch == '"':
            in_double = True
            i += 1
            continue
        if ch == "'":
            in_single = True
            i += 1
            continue
        if ch == "}":
            match = _CONTROL_FLOW_AFTER_BRACE_RE.match(code, i)
            if match is not None:
                last = match
                i = match.end()
                continue
        i += 1
    return last


def _control_flow_header_effect(header: str) -> tuple[bool, int | None]:
    """Return ``(awaiting_body, header_paren_depth)`` for a header starting at a keyword."""
    stripped = header.lstrip(" \t")
    if _CONTROL_FLOW_BARE_KEYWORD_RE.match(stripped):
        return ("{" not in stripped, None)
    if _CONTROL_FLOW_PAREN_KEYWORD_RE.match(stripped):
        depth = _delta_brackets_outside_strings(stripped, opens="(", closes=")")
        if depth > 0:
            return (False, depth)
        if "{" not in stripped.split(")", 1)[-1]:
            return (True, None)
        return (False, None)
    return (False, None)


def _prefix_opens_control_flow_over_suffix(prefix: str) -> bool:
    """Return True when ``prefix`` leaves the next line under control-flow.

    ``if (false)`` / open ``{`` / Python ``if False:`` prefixes keep an added
    salvage call as a line-aligned suffix while preventing execution; suffix
    retention must fail closed (PRRT_kwDOSJAM6s6ZtJG5). Brace-continuation
    headers (``} else`` / ``} catch`` / same-line ``if (...) {} else``) likewise
    attach the following line as their body (PRRT_kwDOSJAM6s6ZtYk1).
    """
    brace_depth = 0
    awaiting_body = False
    header_paren_depth: int | None = None

    for raw_line in prefix.splitlines():
        code = _line_code_without_line_comment(raw_line)
        stripped = code.strip()
        if awaiting_body and stripped:
            awaiting_body = False

        brace_delta = _delta_brackets_outside_strings(code, opens="{", closes="}")
        if header_paren_depth is not None:
            header_paren_depth += _delta_brackets_outside_strings(code, opens="(", closes=")")
            brace_depth = max(0, brace_depth + brace_delta)
            if header_paren_depth > 0:
                continue
            cont = _last_brace_control_flow_continuation(code)
            if cont is not None:
                awaiting_body, header_paren_depth = _control_flow_header_effect(
                    code[_keyword_start_after_brace_match(cont) :]
                )
            elif "{" not in code.split(")", 1)[-1]:
                awaiting_body = True
                header_paren_depth = None
            else:
                header_paren_depth = None
            continue

        if not stripped:
            brace_depth = max(0, brace_depth + brace_delta)
            continue

        if _PYTHON_SUITE_HEADER_RE.match(code):
            awaiting_body = True
            brace_depth = max(0, brace_depth + brace_delta)
            continue

        cont = _last_brace_control_flow_continuation(code)
        if cont is not None:
            brace_depth = max(0, brace_depth + brace_delta)
            awaiting_body, header_paren_depth = _control_flow_header_effect(
                code[_keyword_start_after_brace_match(cont) :]
            )
            continue

        if _CONTROL_FLOW_BARE_HEADER_RE.match(code):
            brace_depth = max(0, brace_depth + brace_delta)
            if "{" not in stripped:
                awaiting_body = True
            continue

        paren_match = _CONTROL_FLOW_PAREN_HEADER_START_RE.match(code)
        if paren_match is not None:
            rest = code[paren_match.start() :]
            depth = _delta_brackets_outside_strings(rest, opens="(", closes=")")
            brace_depth = max(0, brace_depth + brace_delta)
            if depth > 0:
                header_paren_depth = depth
            elif "{" not in rest.split(")", 1)[-1]:
                awaiting_body = True
            continue

        brace_depth = max(0, brace_depth + brace_delta)

    return brace_depth > 0 or awaiting_body or header_paren_depth is not None
