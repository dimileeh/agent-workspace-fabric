"""Codebase-wide invariant: never ``git push origin HEAD`` without an
explicit refspec.

The 2026-04-23 aira-web incident happened because
``pr_monitor_runner.remote_ops._git_push`` issued ``git push origin HEAD``.
That command's destination depends on ``push.default`` and
``branch.<current>.merge`` — both of which had been polluted on the
shared bare mirror by prior sync workspaces. The polluted config
redirected HEAD to ``development``, putting four feature-branch
commits on the shared base branch and bypassing review.

**The invariant**: every ``git push origin HEAD`` the codebase
issues MUST be followed immediately by a ``:refs/heads/...``
refspec suffix (either ``HEAD:refs/heads/<branch>`` as a single
argument, OR paired inside an args list). No bare ``"HEAD"`` arg
after ``"origin"``.

This test is a guardrail against future refactors that "simplify"
back to the ambiguous form. If you need to push the current
branch, pass the explicit refspec — nothing else is safe in the
multi-worktree, shared-mirror layout AWF uses.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SRC = Path(__file__).parents[3] / "src"
_SCRIPTS = Path(__file__).parents[3] / "scripts"


def _iter_py_files() -> list[Path]:
    return [p for p in _SRC.rglob("*.py") if "__pycache__" not in p.parts] + [
        p for p in _SCRIPTS.rglob("*.py") if "__pycache__" not in p.parts
    ]


def _find_push_list_literals(tree: ast.AST) -> list[tuple[int, list[ast.expr]]]:
    """Return (lineno, elts) for every list literal that starts with
    ``"git"``-ish and contains the sequence ``"push", "origin", ...``.
    We look at list literals because every git command in this codebase
    is built as a list passed to ``runner.run([...])``.
    """
    found: list[tuple[int, list[ast.expr]]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.List):
            continue
        elts = node.elts
        if len(elts) < 4:
            continue
        # Collect only string-constant literals; dynamic f-strings don't
        # concern this test because they can't produce bare "HEAD" via
        # a constant "origin" neighbour without making it ambiguous.
        first = elts[0]
        if not (isinstance(first, ast.Constant) and first.value == "git"):
            continue
        string_vals = [
            e.value for e in elts if isinstance(e, ast.Constant) and isinstance(e.value, str)
        ]
        if "push" in string_vals and "origin" in string_vals:
            found.append((node.lineno, elts))
    return found


def _literal_strings(elts: list[ast.expr]) -> list[str | None]:
    """Map each element to its string value, or None if not a constant."""
    out: list[str | None] = []
    for e in elts:
        if isinstance(e, ast.Constant) and isinstance(e.value, str):
            out.append(e.value)
        else:
            out.append(None)
    return out


def test_no_bare_push_origin_head_in_codebase() -> None:  # noqa: N802 — historical name retained
    """No ``git push origin HEAD`` without an explicit refspec anywhere.

    Flags list literals where an element is exactly ``"HEAD"`` and the
    previous string element is ``"origin"``. The safe form is
    ``HEAD:refs/heads/<branch>`` as a single string, which this check
    allows (since the literal element would be
    ``"HEAD:refs/heads/..."`` — equal to the prefix ``"HEAD:"`` but not
    equal to just ``"HEAD"``).
    """
    offenders: list[str] = []
    for path in _iter_py_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover
            continue
        for lineno, elts in _find_push_list_literals(tree):
            vals = _literal_strings(elts)
            # Find positions of "origin" followed by bare "HEAD".
            for i in range(len(vals) - 1):
                if vals[i] == "origin" and vals[i + 1] == "HEAD":
                    offenders.append(
                        f"{path.relative_to(path.parents[3])}:{lineno} — bare"
                        " 'HEAD' after 'origin' (use 'HEAD:refs/heads/<branch>')"
                    )
    assert not offenders, (
        "Found bare ``git push origin HEAD`` without refspec — that's"
        " the 2026-04-23 aira-web regression. Offenders:\n  " + "\n  ".join(offenders)
    )


def test_monitor_git_push_arguments_carry_refspec() -> None:
    """Narrower assertion on the exact function that caused the
    incident: ``pr_monitor_runner.remote_ops._git_push``. The push command it
    issues must include a ``refs/heads/`` refspec.

    We parse the function's AST and scan only the string constants in
    its body — not its docstring. Otherwise a future regression could
    leave ``HEAD:refs/heads/`` in the docstring while the actual code
    reverted to bare ``git push origin HEAD`` and this test would
    silently pass."""
    from awf.runtime.pr_monitor_runner import remote_ops

    source = Path(remote_ops.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    # Locate the implementation helper mixed into ``PullRequestMonitorRunner``.
    target: ast.AsyncFunctionDef | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_git_push":
            target = node
            break
    assert target is not None, "_git_push not found in pr_monitor_runner"

    # Strip docstring — first statement if it's a bare string expression.
    body = target.body
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]

    code_module = ast.Module(body=body, type_ignores=[])
    found_refspec = False
    for node in ast.walk(code_module):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and "HEAD:refs/heads/" in node.value
        ):
            found_refspec = True
            break
        # f-strings: the literal prefix sits inside JoinedStr/Constant.
        if isinstance(node, ast.JoinedStr):
            for part in node.values:
                if (
                    isinstance(part, ast.Constant)
                    and isinstance(part.value, str)
                    and "HEAD:refs/heads/" in part.value
                ):
                    found_refspec = True
                    break
        if found_refspec:
            break
    assert found_refspec, (
        "_git_push body must contain an explicit"
        " ``HEAD:refs/heads/<branch>`` refspec string — reverting to"
        " ``push origin HEAD`` reopens the 2026-04-23 bug."
    )
