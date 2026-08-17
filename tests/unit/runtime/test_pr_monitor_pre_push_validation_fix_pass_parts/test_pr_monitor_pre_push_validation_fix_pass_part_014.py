"""Tip-extra supersession rebinding coverage (part 014).

Moved out of part_010 to stay under the first-party line limit.
"""

from __future__ import annotations

import pytest


@pytest.mark.unit
def test_tip_extra_can_supersede_modified_salvage_rebinding() -> None:
    """Baseline tips that append a rebinding of a salvage-changed name fail closed.

    ``git merge-file`` can cleanly reproduce a descendant that keeps
    ``FEATURE_ENABLED = True`` and appends ``FEATURE_ENABLED = False`` when
    surrounding context exists; equality with HEAD would then falsely retain
    salvage. Only names whose last binding line changed vs parent count, so
    unrelated appends / later hunks stay retained (PRRT_kwDOSJAM6s6Zp_3j).
    """
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass import (
        _salvage_changed_binding_names,
        _tip_extra_can_supersede_modified_salvage,
    )

    parent = "x = 1\nFEATURE_ENABLED = False\ny = 2\n"
    commit = "x = 1\nFEATURE_ENABLED = True\ny = 2\n"
    assert _salvage_changed_binding_names(parent_blob=parent, commit_blob=commit) == {
        "FEATURE_ENABLED"
    }
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent,
        commit_blob=commit,
        head_blob="x = 1\nFEATURE_ENABLED = True\ny = 2\nFEATURE_ENABLED = False\n",
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent,
        commit_blob=commit,
        head_blob=("x = 1\nFEATURE_ENABLED = True\ny = 3\nFEATURE_ENABLED = False\n"),
    )
    # Compound assigns supersede salvage like plain ``=`` (PRRT_kwDOSJAM6s6ZsNCC).
    for compound_line in (
        "FEATURE_ENABLED &= False\n",
        "FEATURE_ENABLED += 1\n",
        "FEATURE_ENABLED -= 1\n",
        "FEATURE_ENABLED |= True\n",
        "FEATURE_ENABLED ^= True\n",
    ):
        assert _tip_extra_can_supersede_modified_salvage(
            parent_blob=parent,
            commit_blob=commit,
            head_blob="x = 1\nFEATURE_ENABLED = True\ny = 2\n" + compound_line,
        )
    # Subscript assign overrides must supersede: bare/dotted-only binding
    # patterns miss ``FLAGS["enabled"] =`` so clean merge-file equality would
    # retain stale FIXED evidence (PRRT_kwDOSJAM6s6ZsQFs).
    parent_sub = 'FLAGS = {}\nFLAGS["enabled"] = False\n'
    commit_sub = 'FLAGS = {}\nFLAGS["enabled"] = True\n'
    assert _salvage_changed_binding_names(parent_blob=parent_sub, commit_blob=commit_sub) == {
        'FLAGS["enabled"]'
    }
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_sub,
        commit_blob=commit_sub,
        head_blob='FLAGS = {}\nFLAGS["enabled"] = True\nFLAGS["enabled"] = False\n',
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_sub,
        commit_blob=commit_sub,
        head_blob=("FLAGS = {}\nFLAGS[\"enabled\"] = True\nFLAGS['enabled'] = False\n"),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_sub,
        commit_blob=commit_sub,
        head_blob=('FLAGS = {}\nFLAGS["enabled"] = True\nif ready: FLAGS["enabled"] = False\n'),
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_sub,
        commit_blob=commit_sub,
        head_blob='FLAGS = {}\nFLAGS["enabled"] = True\nother = 1\n',
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_sub,
        commit_blob=commit_sub,
        head_blob=('FLAGS = {}\nFLAGS["enabled"] = True\nOTHER["enabled"] = False\n'),
    )
    # Nested / mid-statement rebinds must supersede: line-start assign matching
    # misses ``if ready: FEATURE_ENABLED = False`` so merge-file equality would
    # retain stale FIXED evidence (PRRT_kwDOSJAM6s6ZsD5y).
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent,
        commit_blob=commit,
        head_blob=("x = 1\nFEATURE_ENABLED = True\ny = 2\nif ready: FEATURE_ENABLED = False\n"),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent,
        commit_blob=commit,
        head_blob=("x = 1\nFEATURE_ENABLED = True\ny = 2\nif ready: FEATURE_ENABLED &= False\n"),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent,
        commit_blob=commit,
        head_blob=("x = 1\nFEATURE_ENABLED = True\ny = 2\nx = 9; FEATURE_ENABLED = False\n"),
    )
    # Nested typed ``name: T =`` must supersede too; recording the type token
    # ``bool`` (or skipping it without recovering ``FEATURE_ENABLED``) left
    # merge-file equality retaining stale FIXED evidence (PRRT_kwDOSJAM6s6Zs0s8).
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent,
        commit_blob=commit,
        head_blob=(
            "x = 1\nFEATURE_ENABLED = True\ny = 2\nif ready: FEATURE_ENABLED: bool = False\n"
        ),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent,
        commit_blob=commit,
        head_blob=("x = 1\nFEATURE_ENABLED = True\ny = 2\nx = 9; FEATURE_ENABLED: bool = False\n"),
    )
    # One-line ``class`` / ``def … -> T`` suite headers must supersede too;
    # treating them as typed assigns bound ``C`` / ``T`` and kept FIXED
    # evidence (PRRT_kwDOSJAM6s6Zs-so).
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent,
        commit_blob=commit,
        head_blob=("x = 1\nFEATURE_ENABLED = True\ny = 2\nclass C: FEATURE_ENABLED = False\n"),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent,
        commit_blob=commit,
        head_blob=("x = 1\nFEATURE_ENABLED = True\ny = 2\ndef f() -> T: FEATURE_ENABLED = False\n"),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent,
        commit_blob=commit,
        head_blob=(
            "x = 1\nFEATURE_ENABLED = True\ny = 2\nasync def f() -> T: FEATURE_ENABLED = False\n"
        ),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent,
        commit_blob=commit,
        head_blob=(
            "x = 1\nFEATURE_ENABLED = True\ny = 2\nclass C: FEATURE_ENABLED: bool = False\n"
        ),
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent,
        commit_blob=commit,
        head_blob=("x = 1\nFEATURE_ENABLED = True\ny = 2\n# if ready: FEATURE_ENABLED = False\n"),
    )
    # Kwargs / defaults sharing the salvage name are not rebinds
    # (PRRT_kwDOSJAM6s6ZsJyZ).
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent,
        commit_blob=commit,
        head_blob=("x = 1\nFEATURE_ENABLED = True\ny = 2\nconfigure(FEATURE_ENABLED=False)\n"),
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent,
        commit_blob=commit,
        head_blob=(
            "x = 1\nFEATURE_ENABLED = True\ny = 2\ndef helper(FEATURE_ENABLED=False):\n    pass\n"
        ),
    )
    # Unpacking LHS / parenthesized walrus after ``,`` or ``(`` still rebind
    # (PRRT_kwDOSJAM6s6ZsOT0); the kwarg filter must not keep FIXED evidence.
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent,
        commit_blob=commit,
        head_blob=("x = 1\nFEATURE_ENABLED = True\ny = 2\na, FEATURE_ENABLED = get_flags()\n"),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent,
        commit_blob=commit,
        head_blob=("x = 1\nFEATURE_ENABLED = True\ny = 2\nFEATURE_ENABLED, other = get_flags()\n"),
    )
    # Parenthesized / list unpacking must supersede too — no ident sits before
    # ``=``, so bare unpacking recovery alone keeps FIXED evidence
    # (PRRT_kwDOSJAM6s6ZsZ5d).
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent,
        commit_blob=commit,
        head_blob=("x = 1\nFEATURE_ENABLED = True\ny = 2\n(FEATURE_ENABLED, other) = (False, 1)\n"),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent,
        commit_blob=commit,
        head_blob=("x = 1\nFEATURE_ENABLED = True\ny = 2\n[FEATURE_ENABLED, other] = [False, 1]\n"),
    )
    # Starred / trailing-comma paren-list unpack must supersede too
    # (PRRT_kwDOSJAM6s6ZsfLc).
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent,
        commit_blob=commit,
        head_blob=(
            "x = 1\nFEATURE_ENABLED = True\ny = 2\n(FEATURE_ENABLED, *rest) = (False, 1, 2)\n"
        ),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent,
        commit_blob=commit,
        head_blob=(
            "x = 1\nFEATURE_ENABLED = True\ny = 2\n(*rest, FEATURE_ENABLED) = (1, 2, False)\n"
        ),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent,
        commit_blob=commit,
        head_blob=(
            "x = 1\nFEATURE_ENABLED = True\ny = 2\n(FEATURE_ENABLED, other,) = (False, 1)\n"
        ),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent,
        commit_blob=commit,
        head_blob=("x = 1\nFEATURE_ENABLED = True\ny = 2\n(FEATURE_ENABLED,) = (False,)\n"),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent,
        commit_blob=commit,
        head_blob=(
            "x = 1\nFEATURE_ENABLED = True\ny = 2\n[FEATURE_ENABLED, *rest] = [False, 1, 2]\n"
        ),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent,
        commit_blob=commit,
        head_blob=("x = 1\nFEATURE_ENABLED = True\ny = 2\nFEATURE_ENABLED, *rest = get_flags()\n"),
    )
    # Nested paren/list unpacking must supersede too; flat bodies miss nested
    # targets and keep FIXED salvage (PRRT_kwDOSJAM6s6ZsnYi).
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent,
        commit_blob=commit,
        head_blob=(
            "x = 1\nFEATURE_ENABLED = True\ny = 2\n"
            "(other, (FEATURE_ENABLED, rest)) = (1, (False, 2))\n"
        ),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent,
        commit_blob=commit,
        head_blob=(
            "x = 1\nFEATURE_ENABLED = True\ny = 2\n"
            "((FEATURE_ENABLED, rest), other) = ((False, 2), 1)\n"
        ),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent,
        commit_blob=commit,
        head_blob=(
            "x = 1\nFEATURE_ENABLED = True\ny = 2\n"
            "[other, [FEATURE_ENABLED, rest]] = [1, [False, 2]]\n"
        ),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent,
        commit_blob=commit,
        head_blob=(
            "x = 1\nFEATURE_ENABLED = True\ny = 2\n"
            "(other, [FEATURE_ENABLED, rest]) = (1, [False, 2])\n"
        ),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_sub,
        commit_blob=commit_sub,
        head_blob=(
            'FLAGS = {}\nFLAGS["enabled"] = True\n(FLAGS["enabled"], other) = get_flags()\n'
        ),
    )
    # Subscript unpacking priors must supersede too (PRRT_kwDOSJAM6s6ZsYZx).
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_sub,
        commit_blob=commit_sub,
        head_blob=('FLAGS = {}\nFLAGS["enabled"] = True\nFLAGS["enabled"], other = get_flags()\n'),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_sub,
        commit_blob=commit_sub,
        head_blob=(
            "FLAGS = {}\nFLAGS[\"enabled\"] = True\nFLAGS['enabled'], other = get_flags()\n"
        ),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_sub,
        commit_blob=commit_sub,
        head_blob=('FLAGS = {}\nFLAGS["enabled"] = True\nother, FLAGS["enabled"] = get_flags()\n'),
    )
    # ``++`` / ``--`` update expressions supersede modified salvage the same
    # way compound assigns do (PRRT_kwDOSJAM6s6Zs-Rb).
    parent_budget = "x = 1\nretryBudget = 0\ny = 2\n"
    commit_budget = "x = 1\nretryBudget = 2\ny = 2\n"
    for update_line in (
        "retryBudget++\n",
        "retryBudget--\n",
        "++retryBudget\n",
        "--retryBudget\n",
    ):
        assert _tip_extra_can_supersede_modified_salvage(
            parent_blob=parent_budget,
            commit_blob=commit_budget,
            head_blob=commit_budget + update_line,
        )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_budget,
        commit_blob=commit_budget,
        head_blob=commit_budget + "if (ready) retryBudget--\n",
    )
    parent_count = "obj.count = 0\n"
    commit_count = "obj.count = 1\n"
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_count,
        commit_blob=commit_count,
        head_blob=commit_count + "obj.count++\n",
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_sub,
        commit_blob='FLAGS = {}\nFLAGS["enabled"] = 1\n',
        head_blob=('FLAGS = {}\nFLAGS["enabled"] = 1\nFLAGS["enabled"]++\n'),
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_budget,
        commit_blob=commit_budget,
        head_blob=commit_budget + "# retryBudget--\n",
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_budget,
        commit_blob=commit_budget,
        head_blob=commit_budget + "other--\n",
    )
    # ``del`` supersedes modified salvage the same way (PRRT_kwDOSJAM6s6Zse8m).
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent,
        commit_blob=commit,
        head_blob=("x = 1\nFEATURE_ENABLED = True\ny = 2\ndel FEATURE_ENABLED\n"),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent,
        commit_blob=commit,
        head_blob=("x = 1\nFEATURE_ENABLED = True\ny = 2\nif ready: del FEATURE_ENABLED\n"),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent,
        commit_blob=commit,
        head_blob=("x = 1\nFEATURE_ENABLED = True\ny = 2\ndel FEATURE_ENABLED, other\n"),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_sub,
        commit_blob=commit_sub,
        head_blob=('FLAGS = {}\nFLAGS["enabled"] = True\ndel FLAGS["enabled"]\n'),
    )
    # Parenthesized ``del(NAME)`` / ``del (NAME)`` supersede modified salvage
    # the same way (PRRT_kwDOSJAM6s6ZsmNH).
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent,
        commit_blob=commit,
        head_blob=("x = 1\nFEATURE_ENABLED = True\ny = 2\ndel(FEATURE_ENABLED)\n"),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent,
        commit_blob=commit,
        head_blob=("x = 1\nFEATURE_ENABLED = True\ny = 2\ndel (FEATURE_ENABLED)\n"),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent,
        commit_blob=commit,
        head_blob=("x = 1\nFEATURE_ENABLED = True\ny = 2\nif ready: del(FEATURE_ENABLED)\n"),
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent,
        commit_blob=commit,
        head_blob=("x = 1\nFEATURE_ENABLED = True\ny = 2\n# del FEATURE_ENABLED\n"),
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent,
        commit_blob=commit,
        head_blob=("x = 1\nFEATURE_ENABLED = True\ny = 2\ndel other\n"),
    )
    # JS ``delete`` supersedes modified salvage the same way (PRRT_kwDOSJAM6s6ZtiIE).
    parent_guard = "x = 1\nguard.enabled = false\ny = 2\n"
    commit_guard = "x = 1\nguard.enabled = true\ny = 2\n"
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_guard,
        commit_blob=commit_guard,
        head_blob=("x = 1\nguard.enabled = true\ny = 2\ndelete guard.enabled\n"),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_guard,
        commit_blob=commit_guard,
        head_blob=("x = 1\nguard.enabled = true\ny = 2\nif (ready) delete guard.enabled\n"),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_sub,
        commit_blob=commit_sub,
        head_blob=('FLAGS = {}\nFLAGS["enabled"] = True\ndelete FLAGS["enabled"]\n'),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_guard,
        commit_blob=commit_guard,
        head_blob=("x = 1\nguard.enabled = true\ny = 2\ndelete(guard.enabled)\n"),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_guard,
        commit_blob=commit_guard,
        head_blob=("x = 1\nguard.enabled = true\ny = 2\ndelete (guard.enabled)\n"),
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_guard,
        commit_blob=commit_guard,
        head_blob=("x = 1\nguard.enabled = true\ny = 2\n// delete guard.enabled\n"),
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_guard,
        commit_blob=commit_guard,
        head_blob=("x = 1\nguard.enabled = true\ny = 2\ndelete other\n"),
    )
    # Shell ``unset`` supersedes modified salvage the same way (PRRT_kwDOSJAM6s6ZuRSm).
    parent_shell = "x=1\nFEATURE_ENABLED=false\ny=2\n"
    commit_shell = "x=1\nFEATURE_ENABLED=true\ny=2\n"
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_shell,
        commit_blob=commit_shell,
        head_blob=("x=1\nFEATURE_ENABLED=true\ny=2\nunset FEATURE_ENABLED\n"),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_shell,
        commit_blob=commit_shell,
        head_blob=("x=1\nFEATURE_ENABLED=true\ny=2\nunset -v FEATURE_ENABLED\n"),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_shell,
        commit_blob=commit_shell,
        head_blob=("x=1\nFEATURE_ENABLED=true\ny=2\nunset -- FEATURE_ENABLED\n"),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_shell,
        commit_blob=commit_shell,
        head_blob=("x=1\nFEATURE_ENABLED=true\ny=2\nunset FEATURE_ENABLED OTHER\n"),
    )
    parent_export_unset = "x=1\nexport FEATURE_ENABLED=false\ny=2\n"
    commit_export_unset = "x=1\nexport FEATURE_ENABLED=true\ny=2\n"
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_export_unset,
        commit_blob=commit_export_unset,
        head_blob=(
            "x=1\nexport FEATURE_ENABLED=true\ny=2\nif true; then unset -v FEATURE_ENABLED; fi\n"
        ),
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_shell,
        commit_blob=commit_shell,
        head_blob=("x=1\nFEATURE_ENABLED=true\ny=2\n# unset FEATURE_ENABLED\n"),
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_shell,
        commit_blob=commit_shell,
        head_blob=("x=1\nFEATURE_ENABLED=true\ny=2\nunset other\n"),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent,
        commit_blob=commit,
        head_blob=("x = 1\nFEATURE_ENABLED = True\ny = 2\n(FEATURE_ENABLED := False)\n"),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent,
        commit_blob=commit,
        head_blob=("x = 1\nFEATURE_ENABLED = True\ny = 2\nz = (FEATURE_ENABLED := False)\n"),
    )
    # Unrelated tip kwargs must not collide with salvage-changed kwarg phantoms.
    parent_kw = "configure(timeout=10)\nFEATURE_ENABLED = False\n"
    commit_kw = "configure(timeout=30)\nFEATURE_ENABLED = True\n"
    assert _salvage_changed_binding_names(parent_blob=parent_kw, commit_blob=commit_kw) == {
        "FEATURE_ENABLED"
    }
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_kw,
        commit_blob=commit_kw,
        head_blob=commit_kw + "other_fn(timeout=99)\n",
    )
    # Shell ``export NAME=value`` rebinds must supersede like bare assignments
    # (PRRT_kwDOSJAM6s6ZqseO).
    parent_export = "x=1\nexport FEATURE_ENABLED=false\ny=2\n"
    commit_export = "x=1\nexport FEATURE_ENABLED=true\ny=2\n"
    assert _salvage_changed_binding_names(parent_blob=parent_export, commit_blob=commit_export) == {
        "FEATURE_ENABLED"
    }
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_export,
        commit_blob=commit_export,
        head_blob=("x=1\nexport FEATURE_ENABLED=true\ny=2\nexport FEATURE_ENABLED=false\n"),
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_export,
        commit_blob=commit_export,
        head_blob="x=1\nexport FEATURE_ENABLED=true\ny=2\nother=1\n",
    )
    # ``declare -x`` / ``typeset`` rebinds must supersede like ``export``
    # (PRRT_kwDOSJAM6s6ZqxX4).
    parent_declare = "x=1\ndeclare -x FEATURE_ENABLED=false\ny=2\n"
    commit_declare = "x=1\ndeclare -x FEATURE_ENABLED=true\ny=2\n"
    assert _salvage_changed_binding_names(
        parent_blob=parent_declare, commit_blob=commit_declare
    ) == {"FEATURE_ENABLED"}
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_declare,
        commit_blob=commit_declare,
        head_blob=("x=1\ndeclare -x FEATURE_ENABLED=true\ny=2\ndeclare -x FEATURE_ENABLED=false\n"),
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_declare,
        commit_blob=commit_declare,
        head_blob="x=1\ndeclare -x FEATURE_ENABLED=true\ny=2\nother=1\n",
    )
    parent_typeset = "x=1\ntypeset -x FEATURE_ENABLED=false\ny=2\n"
    commit_typeset = "x=1\ntypeset -x FEATURE_ENABLED=true\ny=2\n"
    assert _salvage_changed_binding_names(
        parent_blob=parent_typeset, commit_blob=commit_typeset
    ) == {"FEATURE_ENABLED"}
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_typeset,
        commit_blob=commit_typeset,
        head_blob=("x=1\ntypeset -x FEATURE_ENABLED=true\ny=2\ntypeset -x FEATURE_ENABLED=false\n"),
    )
    # Mixed declare/export spellings of the same name still intersect.
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_declare,
        commit_blob=commit_declare,
        head_blob=("x=1\ndeclare -x FEATURE_ENABLED=true\ny=2\nexport FEATURE_ENABLED=false\n"),
    )
    # ``readonly`` rebinds must supersede like declare/export
    # (PRRT_kwDOSJAM6s6ZrBJF).
    parent_readonly = "x=1\nFEATURE_ENABLED=false\ny=2\n"
    commit_readonly = "x=1\nFEATURE_ENABLED=true\ny=2\n"
    assert _salvage_changed_binding_names(
        parent_blob=parent_readonly, commit_blob=commit_readonly
    ) == {"FEATURE_ENABLED"}
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_readonly,
        commit_blob=commit_readonly,
        head_blob=("x=1\nFEATURE_ENABLED=true\ny=2\nreadonly FEATURE_ENABLED=false\n"),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_declare,
        commit_blob=commit_declare,
        head_blob=("x=1\ndeclare -x FEATURE_ENABLED=true\ny=2\nreadonly FEATURE_ENABLED=false\n"),
    )
    parent_readonly_decl = "x=1\nreadonly FEATURE_ENABLED=false\ny=2\n"
    commit_readonly_decl = "x=1\nreadonly FEATURE_ENABLED=true\ny=2\n"
    assert _salvage_changed_binding_names(
        parent_blob=parent_readonly_decl, commit_blob=commit_readonly_decl
    ) == {"FEATURE_ENABLED"}
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_readonly_decl,
        commit_blob=commit_readonly_decl,
        head_blob=("x=1\nreadonly FEATURE_ENABLED=true\ny=2\nreadonly FEATURE_ENABLED=false\n"),
    )
    # YAML-style key rebinds must supersede like equals-style assignments
    # (PRRT_kwDOSJAM6s6ZqNAk).
    parent_yaml = "x: 1\nfeature_enabled: false\ny: 2\n"
    commit_yaml = "x: 1\nfeature_enabled: true\ny: 2\n"
    assert _salvage_changed_binding_names(parent_blob=parent_yaml, commit_blob=commit_yaml) == {
        "feature_enabled"
    }
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_yaml,
        commit_blob=commit_yaml,
        head_blob="x: 1\nfeature_enabled: true\ny: 2\nfeature_enabled: false\n",
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_yaml,
        commit_blob=commit_yaml,
        head_blob="x: 1\nfeature_enabled: true\ny: 2\nother_key: 1\n",
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_yaml,
        commit_blob=commit_yaml,
        head_blob="x: 1\nfeature_enabled: true\ny: 2\n# feature_enabled: false\n",
    )
    # Nested YAML leaves under different parents must not collide as bare
    # ``enabled``. Salvage of ``feature.enabled`` plus a tip that adds
    # ``logging.enabled`` still merge-file-matches HEAD; unqualified keys would
    # discard salvage and leave a later FIXED retry as fixed_without_head_advance
    # (PRRT_kwDOSJAM6s6ZqZo2).
    parent_nested_yaml = "feature:\n  enabled: false\nlogging:\n  level: info\n"
    commit_nested_yaml = "feature:\n  enabled: true\nlogging:\n  level: info\n"
    nested_yaml_changed = _salvage_changed_binding_names(
        parent_blob=parent_nested_yaml, commit_blob=commit_nested_yaml
    )
    assert "feature.enabled" in nested_yaml_changed
    assert "enabled" not in nested_yaml_changed
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_nested_yaml,
        commit_blob=commit_nested_yaml,
        head_blob=("feature:\n  enabled: true\nlogging:\n  level: info\n  enabled: false\n"),
    )
    # Same-parent tip rebind of the salvaged nested leaf still supersedes.
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_nested_yaml,
        commit_blob=commit_nested_yaml,
        head_blob=("feature:\n  enabled: true\n  enabled: false\nlogging:\n  level: info\n"),
    )
    # Quoted mapping openers with empty values nest the same way.
    parent_quoted_nested = '"feature":\n  enabled: false\n"logging":\n  level: info\n'
    commit_quoted_nested = '"feature":\n  enabled: true\n"logging":\n  level: info\n'
    quoted_nested_changed = _salvage_changed_binding_names(
        parent_blob=parent_quoted_nested, commit_blob=commit_quoted_nested
    )
    assert "feature.enabled" in quoted_nested_changed
    assert "enabled" not in quoted_nested_changed
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_quoted_nested,
        commit_blob=commit_quoted_nested,
        head_blob=('"feature":\n  enabled: true\n"logging":\n  level: info\n  enabled: false\n'),
    )
    # Block-sequence mapping entries (``- enabled:``) must bind like nested
    # leaves. Without recognizing the sequence-item key, salvage only records
    # the enclosing ``feature`` span while a tip that appends ``enabled: false``
    # on the same item is keyed ``feature.enabled`` — empty intersection would
    # retain stale FIXED evidence (PRRT_kwDOSJAM6s6ZqeWt). Scalar identity
    # qualifies the inline leaf as ``feature.enabled.<value>`` so same-item
    # rebinds still intersect while sibling items stay distinct
    # (PRRT_kwDOSJAM6s6ZqxYE).
    parent_seq_yaml = "feature:\n  - enabled: false\n"
    commit_seq_yaml = "feature:\n  - enabled: true\n"
    seq_yaml_changed = _salvage_changed_binding_names(
        parent_blob=parent_seq_yaml, commit_blob=commit_seq_yaml
    )
    assert "feature.enabled.true" in seq_yaml_changed
    assert "feature.enabled.false" in seq_yaml_changed
    assert "enabled" not in seq_yaml_changed
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_seq_yaml,
        commit_blob=commit_seq_yaml,
        head_blob="feature:\n  - enabled: true\n    enabled: false\n",
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_seq_yaml,
        commit_blob=commit_seq_yaml,
        head_blob="feature:\n  - enabled: true\n    other: 1\n",
    )
    # Quoted keys after a sequence marker nest/rebind the same way.
    parent_seq_quoted = 'feature:\n  - "enabled": false\n'
    commit_seq_quoted = 'feature:\n  - "enabled": true\n'
    seq_quoted_changed = _salvage_changed_binding_names(
        parent_blob=parent_seq_quoted, commit_blob=commit_seq_quoted
    )
    assert "feature.enabled.true" in seq_quoted_changed
    assert "feature.enabled.false" in seq_quoted_changed
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_seq_quoted,
        commit_blob=commit_seq_quoted,
        head_blob='feature:\n  - "enabled": true\n    enabled: false\n',
    )
    # Sequence-item mapping openers (no same-line scalar) still qualify nested
    # leaves under the item key.
    parent_seq_nested = "feature:\n  - nested:\n      enabled: false\n"
    commit_seq_nested = "feature:\n  - nested:\n      enabled: true\n"
    seq_nested_changed = _salvage_changed_binding_names(
        parent_blob=parent_seq_nested, commit_blob=commit_seq_nested
    )
    assert "feature.nested.enabled" in seq_nested_changed
    assert "enabled" not in seq_nested_changed
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_seq_nested,
        commit_blob=commit_seq_nested,
        head_blob=("feature:\n  - nested:\n      enabled: true\n      enabled: false\n"),
    )
    # Scalar sequence items (``- name: a``) must open an identity scope so a tip
    # sibling ``- name: b`` with its own ``enabled`` does not collide as
    # ``features.enabled`` and clear retained salvage (PRRT_kwDOSJAM6s6ZqxYE).
    parent_seq_sibling = "features:\n  - name: a\n    enabled: false\n"
    commit_seq_sibling = "features:\n  - name: a\n    enabled: true\n"
    sibling_changed = _salvage_changed_binding_names(
        parent_blob=parent_seq_sibling, commit_blob=commit_seq_sibling
    )
    assert "features.name.a.enabled" in sibling_changed
    assert "features.name.a" in sibling_changed
    assert "features.enabled" not in sibling_changed
    assert "features.name" not in sibling_changed
    assert "enabled" not in sibling_changed
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_seq_sibling,
        commit_blob=commit_seq_sibling,
        head_blob=("features:\n  - name: a\n    enabled: true\n  - name: b\n    enabled: false\n"),
    )
    # Same-identity tip rebind under the salvaged item still supersedes.
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_seq_sibling,
        commit_blob=commit_seq_sibling,
        head_blob=("features:\n  - name: a\n    enabled: true\n    enabled: false\n"),
    )
    # Bare hyphenated sequence-item keys (``- feature-name: a``) must open the
    # same identity scope as underscore keys; omitting ``-`` from the bare
    # class left siblings collapsing to ``features.enabled``
    # (PRRT_kwDOSJAM6s6Zq13_).
    parent_hyphen_seq = "features:\n  - feature-name: a\n    enabled: false\n"
    commit_hyphen_seq = "features:\n  - feature-name: a\n    enabled: true\n"
    hyphen_seq_changed = _salvage_changed_binding_names(
        parent_blob=parent_hyphen_seq, commit_blob=commit_hyphen_seq
    )
    assert "features.feature-name.a.enabled" in hyphen_seq_changed
    assert "features.feature-name.a" in hyphen_seq_changed
    assert "features.enabled" not in hyphen_seq_changed
    assert "enabled" not in hyphen_seq_changed
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_hyphen_seq,
        commit_blob=commit_hyphen_seq,
        head_blob=(
            "features:\n  - feature-name: a\n    enabled: true\n"
            "  - feature-name: b\n    enabled: false\n"
        ),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_hyphen_seq,
        commit_blob=commit_hyphen_seq,
        head_blob=("features:\n  - feature-name: a\n    enabled: true\n    enabled: false\n"),
    )
    # Quoted scalars may contain ``#``; truncating at ``#`` would collapse
    # ``"a#1"`` / ``"a#2"`` to the same identity and falsely supersede salvage
    # when a sibling tip rebinds (PRRT_kwDOSJAM6s6Zq135).
    parent_hash_quoted = 'features:\n  - name: "a#1"\n    enabled: false\n'
    commit_hash_quoted = 'features:\n  - name: "a#1"\n    enabled: true\n'
    hash_quoted_changed = _salvage_changed_binding_names(
        parent_blob=parent_hash_quoted, commit_blob=commit_hash_quoted
    )
    assert "features.name.a#1.enabled" in hash_quoted_changed
    assert "features.name.a#1" in hash_quoted_changed
    assert 'features.name."a' not in hash_quoted_changed
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_hash_quoted,
        commit_blob=commit_hash_quoted,
        head_blob=(
            'features:\n  - name: "a#1"\n    enabled: true\n  - name: "a#2"\n    enabled: false\n'
        ),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_hash_quoted,
        commit_blob=commit_hash_quoted,
        head_blob=('features:\n  - name: "a#1"\n    enabled: true\n    enabled: false\n'),
    )
    parent_hash_single = "features:\n  - name: 'a#1'\n    enabled: false\n"
    commit_hash_single = "features:\n  - name: 'a#1'\n    enabled: true\n"
    assert "features.name.a#1.enabled" in _salvage_changed_binding_names(
        parent_blob=parent_hash_single, commit_blob=commit_hash_single
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_hash_single,
        commit_blob=commit_hash_single,
        head_blob=(
            "features:\n  - name: 'a#1'\n    enabled: true\n  - name: 'a#2'\n    enabled: false\n"
        ),
    )
    # Bare Python control-flow headers (``else:`` / ``try:`` / ``except:`` /
    # ``finally:``) must not open YAML mapping scopes. Treating them as parents
    # qualifies tip rebinds as ``else.FEATURE_ENABLED`` so they miss the
    # salvage-changed bare key and keep stale FIXED evidence
    # (PRRT_kwDOSJAM6s6Zqeen). Quoted ``"else":`` remains a real YAML opener.
    parent_cf = "FEATURE_ENABLED = False\n"
    commit_cf = "FEATURE_ENABLED = True\n"
    for header in ("else:", "try:", "except:", "finally:"):
        assert _tip_extra_can_supersede_modified_salvage(
            parent_blob=parent_cf,
            commit_blob=commit_cf,
            head_blob=(
                f"FEATURE_ENABLED = True\nif cond:\n    pass\n{header}\n"
                "    FEATURE_ENABLED = False\n"
            ),
        )
    parent_quoted_else = '"else":\n  enabled: false\nfeature:\n  level: info\n'
    commit_quoted_else = '"else":\n  enabled: true\nfeature:\n  level: info\n'
    quoted_else_changed = _salvage_changed_binding_names(
        parent_blob=parent_quoted_else, commit_blob=commit_quoted_else
    )
    assert "else.enabled" in quoted_else_changed
    assert "enabled" not in quoted_else_changed
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_quoted_else,
        commit_blob=commit_quoted_else,
        head_blob=('"else":\n  enabled: true\nfeature:\n  level: info\n  enabled: false\n'),
    )
    # Quoted JSON mapping keys must supersede the same way; otherwise a tip that
    # keeps salvage `"feature-enabled": true` and appends a later duplicate
    # false cleanly merge-file-matches HEAD while consumers take the final
    # false (PRRT_kwDOSJAM6s6ZqQfh). Keep the salvage key line byte-identical so
    # only the appended duplicate is tip-extra (trailing commas would retarget
    # the salvage line itself).
    parent_json = '{\n  "feature-enabled": false\n}\n'
    commit_json = '{\n  "feature-enabled": true\n}\n'
    assert _salvage_changed_binding_names(parent_blob=parent_json, commit_blob=commit_json) == {
        "feature-enabled"
    }
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_json,
        commit_blob=commit_json,
        head_blob=('{\n  "feature-enabled": true\n  "other": 1\n  "feature-enabled": false\n}\n'),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_json,
        commit_blob=commit_json,
        head_blob=('{\n  "feature-enabled": true\n}\n"feature-enabled": false\n'),
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_json,
        commit_blob=commit_json,
        head_blob=('{\n  "feature-enabled": true\n  "other": 1\n}\n'),
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_json,
        commit_blob=commit_json,
        head_blob=('{\n  "feature-enabled": true\n}\n# "feature-enabled": false\n'),
    )
    # TOML table / array-table headers must qualify leaf keys so ``[feature]``
    # ``enabled`` and ``[logging]`` ``enabled`` do not collide as bare
    # ``enabled``. Salvage of ``feature.enabled`` plus a tip that adds
    # ``logging.enabled`` still merge-file-matches HEAD; unqualified keys would
    # discard salvage and leave a later FIXED retry as fixed_without_head_advance
    # (PRRT_kwDOSJAM6s6ZqpBC).
    parent_toml_table = '[feature]\nenabled = false\n[logging]\nlevel = "info"\n'
    commit_toml_table = '[feature]\nenabled = true\n[logging]\nlevel = "info"\n'
    toml_table_changed = _salvage_changed_binding_names(
        parent_blob=parent_toml_table, commit_blob=commit_toml_table
    )
    assert "feature.enabled" in toml_table_changed
    assert "enabled" not in toml_table_changed
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_toml_table,
        commit_blob=commit_toml_table,
        head_blob=('[feature]\nenabled = true\n[logging]\nlevel = "info"\nenabled = false\n'),
    )
    # Same-table tip rebind of the salvaged leaf still supersedes.
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_toml_table,
        commit_blob=commit_toml_table,
        head_blob=('[feature]\nenabled = true\nenabled = false\n[logging]\nlevel = "info"\n'),
    )
    # Array tables and dotted / quoted table paths qualify the same way.
    parent_arr = '[[feature]]\nenabled = false\n[[logging]]\nlevel = "info"\n'
    commit_arr = '[[feature]]\nenabled = true\n[[logging]]\nlevel = "info"\n'
    arr_changed = _salvage_changed_binding_names(parent_blob=parent_arr, commit_blob=commit_arr)
    assert "feature.enabled" in arr_changed
    assert "enabled" not in arr_changed
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_arr,
        commit_blob=commit_arr,
        head_blob=('[[feature]]\nenabled = true\n[[logging]]\nlevel = "info"\nenabled = false\n'),
    )
    parent_dotted_table = '[feature.sub]\nenabled = false\n[logging]\nlevel = "info"\n'
    commit_dotted_table = '[feature.sub]\nenabled = true\n[logging]\nlevel = "info"\n'
    dotted_table_changed = _salvage_changed_binding_names(
        parent_blob=parent_dotted_table, commit_blob=commit_dotted_table
    )
    assert "feature.sub.enabled" in dotted_table_changed
    assert "enabled" not in dotted_table_changed
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_dotted_table,
        commit_blob=commit_dotted_table,
        head_blob=('[feature.sub]\nenabled = true\n[logging]\nlevel = "info"\nenabled = false\n'),
    )
    parent_quoted_table = '["feature"]\nenabled = false\n["logging"]\nlevel = "info"\n'
    commit_quoted_table = '["feature"]\nenabled = true\n["logging"]\nlevel = "info"\n'
    quoted_table_changed = _salvage_changed_binding_names(
        parent_blob=parent_quoted_table, commit_blob=commit_quoted_table
    )
    assert "feature.enabled" in quoted_table_changed
    assert "enabled" not in quoted_table_changed
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_quoted_table,
        commit_blob=commit_quoted_table,
        head_blob=('["feature"]\nenabled = true\n["logging"]\nlevel = "info"\nenabled = false\n'),
    )
    # TOML bare / quoted keys with ``=`` and hyphens must supersede like JSON
    # quoted ``:`` keys (PRRT_kwDOSJAM6s6Zqip3).
    parent_toml = "feature-enabled = false\n"
    commit_toml = "feature-enabled = true\n"
    assert _salvage_changed_binding_names(parent_blob=parent_toml, commit_blob=commit_toml) == {
        "feature-enabled"
    }
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_toml,
        commit_blob=commit_toml,
        head_blob="feature-enabled = true\nfeature-enabled = false\n",
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_toml,
        commit_blob=commit_toml,
        head_blob="feature-enabled = true\nother = 1\nfeature-enabled = false\n",
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_toml,
        commit_blob=commit_toml,
        head_blob="feature-enabled = true\nother-key = 1\n",
    )
    parent_toml_q = '"feature-enabled" = false\n'
    commit_toml_q = '"feature-enabled" = true\n'
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_toml_q,
        commit_blob=commit_toml_q,
        head_blob='"feature-enabled" = true\n"feature-enabled" = false\n',
    )
    # TOML dotted keys (incl. quoted segments) must supersede on the modified
    # salvage path the same way as added salvage (PRRT_kwDOSJAM6s6Zql88).
    parent_toml_dot = "feature.enabled = false\n"
    commit_toml_dot = "feature.enabled = true\n"
    assert _salvage_changed_binding_names(
        parent_blob=parent_toml_dot, commit_blob=commit_toml_dot
    ) == {"feature.enabled"}
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_toml_dot,
        commit_blob=commit_toml_dot,
        head_blob="feature.enabled = true\nfeature.enabled = false\n",
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_toml_dot,
        commit_blob=commit_toml_dot,
        head_blob='feature.enabled = true\nfeature."enabled" = false\n',
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_toml_dot,
        commit_blob=commit_toml_dot,
        head_blob="feature.enabled = true\nother.key = 1\n",
    )
    # Root tip call ``feature()`` / ``feature[key]()`` must not supersede via
    # ``name.*`` against binding key ``feature.enabled`` (PRRT_kwDOSJAM6s6ZrsE0).
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_toml_dot,
        commit_blob=commit_toml_dot,
        head_blob="feature.enabled = true\nfeature()\n",
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_toml_dot,
        commit_blob=commit_toml_dot,
        head_blob="feature.enabled = true\nfeature[key]()\n",
    )
    # Full dotted tip call of the salvaged key still supersedes.
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_toml_dot,
        commit_blob=commit_toml_dot,
        head_blob="feature.enabled = true\nfeature.enabled()\n",
    )
    # Distinct dotted / quoted-dot keys must not cross-supersede
    # (PRRT_kwDOSJAM6s6ZqoYV).
    parent_host = 'site."google.com" = false\n'
    commit_host = 'site."google.com" = true\n'
    assert _salvage_changed_binding_names(parent_blob=parent_host, commit_blob=commit_host) == {
        'site."google.com"'
    }
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_host,
        commit_blob=commit_host,
        head_blob='site."google.com" = true\nsite.google.com = false\n',
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_host,
        commit_blob=commit_host,
        head_blob='site."google.com" = true\nsite."google.com" = false\n',
    )
    parent_ab = '"a.b" = false\n'
    commit_ab = '"a.b" = true\n'
    assert _salvage_changed_binding_names(parent_blob=parent_ab, commit_blob=commit_ab) == {'"a.b"'}
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_ab,
        commit_blob=commit_ab,
        head_blob='"a.b" = true\na.b = false\n',
    )
    # YAML ``:`` quote-only rebinds of dotted keys must supersede (unlike TOML
    # ``=`` where ``"a.b"`` ≠ ``a.b``; PRRT_kwDOSJAM6s6ZqtHj).
    parent_yaml_ab = '"a.b": false\n'
    commit_yaml_ab = '"a.b": true\n'
    assert _salvage_changed_binding_names(
        parent_blob=parent_yaml_ab, commit_blob=commit_yaml_ab
    ) == {"a.b"}
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_yaml_ab,
        commit_blob=commit_yaml_ab,
        head_blob='"a.b": true\na.b: false\n',
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob="a.b: false\n",
        commit_blob="a.b: true\n",
        head_blob='a.b: true\n"a.b": false\n',
    )
    parent_json_sq = "{\n  'feature-enabled': false\n}\n"
    commit_json_sq = "{\n  'feature-enabled': true\n}\n"
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_json_sq,
        commit_blob=commit_json_sq,
        head_blob=("{\n  'feature-enabled': true\n  'other': 1\n  'feature-enabled': false\n}\n"),
    )
    # Tip-extra Google-style docstring prose must not supersede a real salvage
    # assignment rebind (PRRT_kwDOSJAM6s6ZqPO9).
    parent_timeout = "x = 1\ntimeout = 10\ny = 2\n"
    commit_timeout = "x = 1\ntimeout = 30\ny = 2\n"
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_timeout,
        commit_blob=commit_timeout,
        head_blob=(
            "x = 1\ntimeout = 30\ny = 2\n"
            '"""\n'
            "Args:\n"
            "    timeout: Seconds until the request fails.\n"
            '"""\n'
        ),
    )
    # Tip-extra rebind after a URL/glob/`#`/`//` line that embeds ``/*`` must
    # still supersede; false openers used to skip the rebind (PRRT_kwDOSJAM6s6ZqSbO).
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent,
        commit_blob=commit,
        head_blob=(
            "x = 1\nFEATURE_ENABLED = True\ny = 2\n"
            'url = "https://example.com/*/path"\n'
            "FEATURE_ENABLED = False\n"
        ),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent,
        commit_blob=commit,
        head_blob=(
            "x = 1\nFEATURE_ENABLED = True\ny = 2\n"
            "# see https://example.com/*/docs\n"
            "FEATURE_ENABLED = False\n"
        ),
    )
    # Unrelated append / later hunk must not look like supersession.
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent,
        commit_blob=commit,
        head_blob="x = 1\nFEATURE_ENABLED = True\ny = 2\nother = 1\n",
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent,
        commit_blob=commit,
        head_blob="x = 1\nFEATURE_ENABLED = True\ny = 3\n",
    )
    # Comment-only append cannot supersede.
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent,
        commit_blob=commit,
        head_blob="x = 1\nFEATURE_ENABLED = True\ny = 2\n# FEATURE_ENABLED = False\n",
    )
    # Rebinding an unchanged name (x) must not reject.
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent,
        commit_blob=commit,
        head_blob="x = 1\nFEATURE_ENABLED = True\ny = 2\nx = 9\n",
    )
    # Surplus copies of salvage assignment text in an unrelated later hunk must
    # not look like supersession. Full-line multiset marks the duplicate
    # ``FEATURE_ENABLED = True`` as tip-only, but last-binding equality keeps
    # FIXED evidence (PRRT_kwDOSJAM6s6ZqGeU).
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent,
        commit_blob=commit,
        head_blob="x = 1\nFEATURE_ENABLED = True\ny = 2\nFEATURE_ENABLED = True\n",
    )
    # When salvage flips only the last of two identical ``False`` assignments
    # to ``True``, an appended third ``False`` must still supersede: set
    # membership would hide it behind the unchanged earlier copy
    # (PRRT_kwDOSJAM6s6ZrFdv).
    parent_dup = "FEATURE_ENABLED = False\nFEATURE_ENABLED = False\n"
    commit_dup = "FEATURE_ENABLED = False\nFEATURE_ENABLED = True\n"
    assert _salvage_changed_binding_names(parent_blob=parent_dup, commit_blob=commit_dup) == {
        "FEATURE_ENABLED"
    }
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_dup,
        commit_blob=commit_dup,
        head_blob=("FEATURE_ENABLED = False\nFEATURE_ENABLED = True\nFEATURE_ENABLED = False\n"),
    )
    parent_indented = "class C:\n    FEATURE_ENABLED = False\n"
    commit_indented = "class C:\n    FEATURE_ENABLED = True\n"
    assert _salvage_changed_binding_names(
        parent_blob=parent_indented, commit_blob=commit_indented
    ) == {"C", "C.FEATURE_ENABLED"}
    # Same indented assignment text reused in a later local hunk — identical line
    # text makes a tip-extra multiset hit, but scoped tip keys bind ``helper`` /
    # last ``C.FEATURE_ENABLED`` stays equal so FIXED evidence retains
    # (PRRT_kwDOSJAM6s6ZqGeU).
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_indented,
        commit_blob=commit_indented,
        head_blob=(
            "class C:\n    FEATURE_ENABLED = True\ndef helper():\n    FEATURE_ENABLED = True\n"
        ),
    )
    # Same-signature redefinition reuses the salvage opener line text. Tip-extra
    # multiset counting keeps the duplicate opener tip-only; last-binding span
    # then differs so the append supersedes (PRRT_kwDOSJAM6s6ZqDij).
    parent_def = "x = 1\n"
    commit_def = "x = 1\ndef guard():\n    return True\n"
    head_redef = "x = 1\ndef guard():\n    return True\ndef guard():\n    return False\n"
    assert _salvage_changed_binding_names(parent_blob=parent_def, commit_blob=commit_def) == {
        "guard"
    }
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_def,
        commit_blob=commit_def,
        head_blob=head_redef,
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob="x = 1\n",
        commit_blob="x = 1\nclass Guard:\n    ok = True\n",
        head_blob=("x = 1\nclass Guard:\n    ok = True\nclass Guard:\n    ok = False\n"),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob="x = 1\n",
        commit_blob="x = 1\nfunction guard() {\n  return true;\n}\n",
        head_blob=(
            "x = 1\nfunction guard() {\n  return true;\n}\nfunction guard() {\n  return false;\n}\n"
        ),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob="x = 1\n",
        commit_blob="x = 1\nconst guard = true;\n",
        head_blob="x = 1\nconst guard = true;\nconst guard = false;\n",
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob="x = 1\n",
        commit_blob="x = 1\n#define GUARD 1\n",
        head_blob="x = 1\n#define GUARD 1\n#define GUARD 0\n",
    )
    # Body-only salvage of an existing declaration keeps the same opener line.
    # Comparing opener text alone would omit the name from ``changed``, so a tip
    # that appends a same-signature redefinition would retain stale FIXED
    # evidence after a clean merge-file match (PRRT_kwDOSJAM6s6ZqHvh).
    parent_body = "x = 1\ndef guard():\n    return False\n"
    commit_body = "x = 1\ndef guard():\n    return True\n"
    head_body_redef = "x = 1\ndef guard():\n    return True\ndef guard():\n    return False\n"
    assert _salvage_changed_binding_names(parent_blob=parent_body, commit_blob=commit_body) == {
        "guard"
    }
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_body,
        commit_blob=commit_body,
        head_blob=head_body_redef,
    )
    parent_class = "x = 1\nclass Guard:\n    def ok(self):\n        return False\n"
    commit_class = "x = 1\nclass Guard:\n    def ok(self):\n        return True\n"
    class_changed = _salvage_changed_binding_names(
        parent_blob=parent_class, commit_blob=commit_class
    )
    assert "Guard" in class_changed
    assert "Guard.ok" in class_changed
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_class,
        commit_blob=commit_class,
        head_blob=(
            "x = 1\nclass Guard:\n    def ok(self):\n        return True\n"
            "class Guard:\n    def ok(self):\n        return False\n"
        ),
    )
    # Body-only salvage of ``A.ok`` must not treat an unrelated later ``C.ok``
    # opener as a tip-extra rebind. Flat file-global names + opener multiset
    # wrongly put bare ``ok`` in ``changed`` and counted the surplus method as
    # supersession, dropping still-valid FIXED evidence (PRRT_kwDOSJAM6s6ZqKN3).
    parent_scoped = (
        "class A:\n"
        "    def ok(self):\n"
        "        return False\n"
        "class B:\n"
        "    def other(self):\n"
        "        return 1\n"
    )
    commit_scoped = (
        "class A:\n"
        "    def ok(self):\n"
        "        return True\n"
        "class B:\n"
        "    def other(self):\n"
        "        return 1\n"
    )
    scoped_changed = _salvage_changed_binding_names(
        parent_blob=parent_scoped, commit_blob=commit_scoped
    )
    assert "A.ok" in scoped_changed
    assert "ok" not in scoped_changed
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_scoped,
        commit_blob=commit_scoped,
        head_blob=(commit_scoped + "class C:\n    def ok(self):\n        return False\n"),
    )
    # Same-class tip-extra redefinition of the salvaged method still supersedes.
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_scoped,
        commit_blob=commit_scoped,
        head_blob=(
            "class A:\n"
            "    def ok(self):\n"
            "        return True\n"
            "    def ok(self):\n"
            "        return False\n"
            "class B:\n"
            "    def other(self):\n"
            "        return 1\n"
        ),
    )
    assert _salvage_changed_binding_names(
        parent_blob="x = 1\nfunction guard() {\n  return false;\n}\n",
        commit_blob="x = 1\nfunction guard() {\n  return true;\n}\n",
    ) == {"guard"}
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob="x = 1\nfunction guard() {\n  return false;\n}\n",
        commit_blob="x = 1\nfunction guard() {\n  return true;\n}\n",
        head_blob=(
            "x = 1\nfunction guard() {\n  return true;\n}\nfunction guard() {\n  return false;\n}\n"
        ),
    )
    # Unchanged body must not mark the binding changed.
    assert _salvage_changed_binding_names(parent_blob=commit_body, commit_blob=commit_body) == set()
    # Comment / non-directive hash lines are ordinary tip-extra text; they must
    # not look like binding supersession (PRRT_kwDOSJAM6s6ZqGeU).
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent,
        commit_blob=commit,
        head_blob=("x = 1\nFEATURE_ENABLED = True\ny = 2\n// def guard():\n# not-a-define\n"),
    )
    # No salvage binding change / exact tip match → no supersession.
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=commit,
        commit_blob=commit,
        head_blob=commit + "other = 1\n",
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent,
        commit_blob=commit,
        head_blob=commit,
    )
    # Salvage that deletes a parent binding must still mark the name changed.
    # Iterating only commit spans would omit it; a tip that reintroduces the
    # binding after unrelated content can then cleanly merge-file-match HEAD
    # while falsely retaining FIXED evidence (PRRT_kwDOSJAM6s6ZqKGY).
    parent_deleted = "x = 1\nFEATURE_ENABLED = False\ny = 2\n"
    commit_deleted = "x = 1\ny = 2\n"
    assert _salvage_changed_binding_names(
        parent_blob=parent_deleted, commit_blob=commit_deleted
    ) == {"FEATURE_ENABLED"}
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_deleted,
        commit_blob=commit_deleted,
        head_blob="x = 1\ny = 2\nFEATURE_ENABLED = False\n",
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_deleted,
        commit_blob=commit_deleted,
        head_blob="x = 1\ny = 2\nother = 1\n",
    )
