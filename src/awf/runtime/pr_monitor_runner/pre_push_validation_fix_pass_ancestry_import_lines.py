"""Physical-line readers the import scanners in the cross-file probe share.

An ``import`` statement is a *logical* line: it can carry a trailing comment,
wrap inside parentheses, or continue after a backslash, and it is only matchable
once those physical lines are reduced to the statement they spell. These readers
do that reduction. They live in their own module because
``pre_push_validation_fix_pass_ancestry_cross_file`` sits at the first-party line
budget, and because physical→logical line reading is one unit.
"""

from __future__ import annotations


def _import_line_without_comment(line: str) -> str:
    """``line`` up to its first ``#``.

    A comment ends at its own physical line, but a parenthesized target list is
    joined line by line, so it has to be dropped *before* the join — otherwise
    one ``# note`` swallows every name listed below it, those names never bind,
    and the gate silently falls back to the name-only rule that accepts an
    unrelated same-named definition (PRRT_kwDOSJAM6s6q8BmK). Both import readers
    split on commas, so both strip the comment first. An import statement cannot
    hold a string literal, so scanning for a bare ``#`` is exact.
    """
    head, _hash, _comment = line.partition("#")
    return head


def _import_line_without_continuation(line: str) -> str:
    """``line`` without its trailing backslash line-continuation marker.

    ``from pkg.mod import \\`` + ``record`` is one logical statement in Python, so
    the marker has to be dropped and the next physical line joined on before the
    import heads are matched. Left split, the head's target list is the backslash
    itself, ``record`` binds to no module and keeps the name-only rule that accepts
    an unrelated same-named definition in another package (PRRT_kwDOSJAM6s6rAf0X).
    """
    head = line.rstrip()
    return head[:-1] if head.endswith("\\") else head


def _import_head_bracket_depths(lines: list[str]) -> list[int]:
    """Open-bracket depth each masked line begins at.

    An ``import`` head starts a logical line, so it can never sit inside an open
    bracket. The masked scan deliberately keeps f-string / template ``{...}``
    bodies scannable so interpolated calls stay visible to callee discovery,
    which leaves an import head quoted inside a *multi-line* interpolation
    readable as code. It is still inside the interpolation's brace, so holding
    heads to depth 0 drops that last decoy binding (PRRT_kwDOSJAM6s6q8MXB); a
    real import's parenthesized target list is unaffected because its head is
    itself at depth 0.
    """
    depths: list[int] = []
    depth = 0
    for line in lines:
        depths.append(depth)
        for char in line:
            if char in "([{":
                depth += 1
            elif char in ")]}":
                depth = max(depth - 1, 0)
    return depths
