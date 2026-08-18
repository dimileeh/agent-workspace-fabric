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
    # JS logical ``&&=`` / ``||=`` / ``??=`` must too (PRRT_kwDOSJAM6s6ZyImG).
    for compound_line in (
        "FEATURE_ENABLED &= False\n",
        "FEATURE_ENABLED += 1\n",
        "FEATURE_ENABLED -= 1\n",
        "FEATURE_ENABLED |= True\n",
        "FEATURE_ENABLED ^= True\n",
        "FEATURE_ENABLED &&= False\n",
        "FEATURE_ENABLED ||= False\n",
        "FEATURE_ENABLED ??= False\n",
    ):
        assert _tip_extra_can_supersede_modified_salvage(
            parent_blob=parent,
            commit_blob=commit,
            head_blob="x = 1\nFEATURE_ENABLED = True\ny = 2\n" + compound_line,
        )
    parent_guard = "x = 1\nguard.enabled = false\ny = 2\n"
    commit_guard = "x = 1\nguard.enabled = true\ny = 2\n"
    for logical_line in (
        "guard.enabled &&= false\n",
        "guard.enabled ||= false\n",
        "guard.enabled ??= false\n",
    ):
        assert _tip_extra_can_supersede_modified_salvage(
            parent_blob=parent_guard,
            commit_blob=commit_guard,
            head_blob="x = 1\nguard.enabled = true\ny = 2\n" + logical_line,
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
    # Nonliteral subscript overrides cannot be related by exact-key intersection:
    # after salvage ``FLAGS["enabled"] = True``, tip ``FLAGS[key] = False`` emits
    # ``FLAGS[key]``, which does not overlap ``FLAGS["enabled"]``. Fail closed on
    # tip-extra nonliteral subscripts that share a salvaged receiver
    # (PRRT_kwDOSJAM6s6Zv4pe).
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_sub,
        commit_blob=commit_sub,
        head_blob=('FLAGS = {}\nFLAGS["enabled"] = True\nkey = "enabled"\nFLAGS[key] = False\n'),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_sub,
        commit_blob=commit_sub,
        head_blob=('FLAGS = {}\nFLAGS["enabled"] = True\nFLAGS[key] = False\n'),
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_sub,
        commit_blob=commit_sub,
        head_blob=('FLAGS = {}\nFLAGS["enabled"] = True\nkey = "enabled"\nOTHER[key] = False\n'),
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_sub,
        commit_blob=commit_sub,
        head_blob=('FLAGS = {}\nFLAGS["enabled"] = True\nFLAGS[0] = False\n'),
    )
    # Surplus identical candidate binding makes exact-key overlap non-empty and
    # last-span equality false; nonliteral ``FLAGS[key]`` on the salvaged
    # receiver must still fail closed (PRRT_kwDOSJAM6s6ZwnzM).
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_sub,
        commit_blob=commit_sub,
        head_blob=(
            'FLAGS = {}\nFLAGS["enabled"] = True\nFLAGS["enabled"] = True\nFLAGS[key] = False\n'
        ),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_sub,
        commit_blob=commit_sub,
        head_blob=(
            'FLAGS = {}\nFLAGS["enabled"] = True\n'
            'FLAGS["enabled"] = True\nkey = "enabled"\nFLAGS[key] = False\n'
        ),
    )
    # Collection mutation helpers leave no binding key; call scanner emits
    # ``FLAGS`` / ``FLAGS.__setitem__`` / ``FLAGS.update``, which do not match
    # changed ``FLAGS["enabled"]``. Recognize helpers or fail closed on mutator
    # calls that share a salvaged subscript receiver (PRRT_kwDOSJAM6s6ZwrnH).
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_sub,
        commit_blob=commit_sub,
        head_blob=('FLAGS = {}\nFLAGS["enabled"] = True\nFLAGS.__setitem__("enabled", False)\n'),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_sub,
        commit_blob=commit_sub,
        head_blob=(
            'FLAGS = {}\nFLAGS["enabled"] = True\ndict.__setitem__(FLAGS, "enabled", False)\n'
        ),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_sub,
        commit_blob=commit_sub,
        head_blob=('FLAGS = {}\nFLAGS["enabled"] = True\nFLAGS.__delitem__("enabled")\n'),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_sub,
        commit_blob=commit_sub,
        head_blob=('FLAGS = {}\nFLAGS["enabled"] = True\nFLAGS.update(enabled=False)\n'),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_sub,
        commit_blob=commit_sub,
        head_blob=('FLAGS = {}\nFLAGS["enabled"] = True\nFLAGS.update({"enabled": False})\n'),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_sub,
        commit_blob=commit_sub,
        head_blob=('FLAGS = {}\nFLAGS["enabled"] = True\nFLAGS.update(other_flags)\n'),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_sub,
        commit_blob=commit_sub,
        head_blob=('FLAGS = {}\nFLAGS["enabled"] = True\nFLAGS.clear()\n'),
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_sub,
        commit_blob=commit_sub,
        head_blob=('FLAGS = {}\nFLAGS["enabled"] = True\n# FLAGS.__setitem__("enabled", False)\n'),
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_sub,
        commit_blob=commit_sub,
        head_blob=('FLAGS = {}\nFLAGS["enabled"] = True\nOTHER.__setitem__("enabled", False)\n'),
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_sub,
        commit_blob=commit_sub,
        head_blob=('FLAGS = {}\nFLAGS["enabled"] = True\nFLAGS.__setitem__("other", False)\n'),
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_sub,
        commit_blob=commit_sub,
        head_blob=('FLAGS = {}\nFLAGS["enabled"] = True\nFLAGS.update(other=False)\n'),
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_sub,
        commit_blob=commit_sub,
        head_blob=('FLAGS = {}\nFLAGS["enabled"] = True\nFLAGS.update({"other": False})\n'),
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_sub,
        commit_blob=commit_sub,
        head_blob=('FLAGS = {}\nFLAGS["enabled"] = True\nFLAGS.copy()\n'),
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
    # setattr/delattr mutate a salvage attribute without a binding key or a call
    # name that intersects ``guard.enabled``; recognize the helper target or a
    # later no-change FIXED reuses stale evidence (PRRT_kwDOSJAM6s6Zu8Kn).
    parent_py_guard = "x = 1\nguard.enabled = False\ny = 2\n"
    commit_py_guard = "x = 1\nguard.enabled = True\ny = 2\n"
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_py_guard,
        commit_blob=commit_py_guard,
        head_blob=('x = 1\nguard.enabled = True\ny = 2\nsetattr(guard, "enabled", False)\n'),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_py_guard,
        commit_blob=commit_py_guard,
        head_blob=('x = 1\nguard.enabled = True\ny = 2\ndelattr(guard, "enabled")\n'),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_py_guard,
        commit_blob=commit_py_guard,
        head_blob=(
            'x = 1\nguard.enabled = True\ny = 2\nobject.__setattr__(guard, "enabled", False)\n'
        ),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_py_guard,
        commit_blob=commit_py_guard,
        head_blob=('x = 1\nguard.enabled = True\ny = 2\nguard.__setattr__("enabled", False)\n'),
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_py_guard,
        commit_blob=commit_py_guard,
        head_blob=('x = 1\nguard.enabled = True\ny = 2\n# setattr(guard, "enabled", False)\n'),
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_py_guard,
        commit_blob=commit_py_guard,
        head_blob=('x = 1\nguard.enabled = True\ny = 2\nsetattr(other, "enabled", False)\n'),
    )
    # Dynamic evaluators can undo salvaged bindings without a scannable rebind;
    # tip-extra exec/eval must fail closed (PRRT_kwDOSJAM6s6Z02Us).
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent,
        commit_blob=commit,
        head_blob=('x = 1\nFEATURE_ENABLED = True\ny = 2\nexec("FEATURE_ENABLED = False")\n'),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent,
        commit_blob=commit,
        head_blob=('x = 1\nFEATURE_ENABLED = True\ny = 2\neval("FEATURE_ENABLED = False")\n'),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent,
        commit_blob=commit,
        head_blob=(
            'x = 1\nFEATURE_ENABLED = True\ny = 2\nbuiltins.exec("FEATURE_ENABLED = False")\n'
        ),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent,
        commit_blob=commit,
        head_blob=(
            'x = 1\nFEATURE_ENABLED = True\ny = 2\nexec(\n    "FEATURE_ENABLED = False"\n)\n'
        ),
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent,
        commit_blob=commit,
        head_blob=('x = 1\nFEATURE_ENABLED = True\ny = 2\n# exec("FEATURE_ENABLED = False")\n'),
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent,
        commit_blob=commit,
        head_blob="x = 1\nFEATURE_ENABLED = True\ny = 3\n",
    )
    # Object.assign after salvage ``guard.enabled = true`` leaves no binding key
    # and only call names ``Object`` / ``Object.assign``; recognize literal-key
    # mutation or fail closed on opaque sources sharing the salvaged receiver
    # (PRRT_kwDOSJAM6s6Zxwhs).
    parent_js_guard = "x = 1\nguard.enabled = false\ny = 2\n"
    commit_js_guard = "x = 1\nguard.enabled = true\ny = 2\n"
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_js_guard,
        commit_blob=commit_js_guard,
        head_blob=("x = 1\nguard.enabled = true\ny = 2\nObject.assign(guard, {enabled: false})\n"),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_js_guard,
        commit_blob=commit_js_guard,
        head_blob=(
            'x = 1\nguard.enabled = true\ny = 2\nObject.assign(guard, {"enabled": false})\n'
        ),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_js_guard,
        commit_blob=commit_js_guard,
        head_blob=("x = 1\nguard.enabled = true\ny = 2\nObject.assign(guard, other)\n"),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_js_guard,
        commit_blob=commit_js_guard,
        head_blob=(
            "x = 1\nguard.enabled = true\ny = 2\nObject.assign(guard, {other: false}, extra)\n"
        ),
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_js_guard,
        commit_blob=commit_js_guard,
        head_blob=(
            "x = 1\nguard.enabled = true\ny = 2\n// Object.assign(guard, {enabled: false})\n"
        ),
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_js_guard,
        commit_blob=commit_js_guard,
        head_blob=("x = 1\nguard.enabled = true\ny = 2\nObject.assign(other, {enabled: false})\n"),
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_js_guard,
        commit_blob=commit_js_guard,
        head_blob=("x = 1\nguard.enabled = true\ny = 2\nObject.assign(guard, {other: false})\n"),
    )
    # Multiline Object.assign after salvage (PRRT_kwDOSJAM6s6Zyo4_).
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_js_guard,
        commit_blob=commit_js_guard,
        head_blob=(
            "x = 1\nguard.enabled = true\ny = 2\nObject.assign(\n  guard,\n  {enabled: false}\n);\n"
        ),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_js_guard,
        commit_blob=commit_js_guard,
        head_blob=("x = 1\nguard.enabled = true\ny = 2\nObject.assign(\n  guard,\n  other\n);\n"),
    )
    # Shared Object.assign opener: tip only edits argument lines, so the opener is
    # not tip-extra. Look-back join must still synthesize target.key / fail closed
    # on opaque sources (PRRT_kwDOSJAM6s6Zy5DN).
    parent_shared_assign = "Object.assign(\n  guard,\n  {enabled: false}\n);\n"
    commit_shared_assign = "Object.assign(\n  guard,\n  {enabled: true}\n);\n"
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_shared_assign,
        commit_blob=commit_shared_assign,
        head_blob="Object.assign(\n  guard,\n  {enabled: false}\n);\n",
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_shared_assign,
        commit_blob=commit_shared_assign,
        head_blob="Object.assign(\n  guard,\n  other\n);\n",
    )
    parent_shared_retarget = "guard.enabled = false\nObject.assign(\n  other,\n  {x: 1}\n);\n"
    commit_shared_retarget = "guard.enabled = true\nObject.assign(\n  other,\n  {x: 1}\n);\n"
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_shared_retarget,
        commit_blob=commit_shared_retarget,
        head_blob=("guard.enabled = true\nObject.assign(\n  guard,\n  {enabled: false}\n);\n"),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_shared_retarget,
        commit_blob=commit_shared_retarget,
        head_blob="guard.enabled = true\nObject.assign(\n  guard,\n  other\n);\n",
    )
    # Object.defineProperty after salvage ``guard.enabled = true`` (PRRT_kwDOSJAM6s6Zy4pR).
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_js_guard,
        commit_blob=commit_js_guard,
        head_blob=(
            "x = 1\nguard.enabled = true\ny = 2\n"
            'Object.defineProperty(guard, "enabled", {value: false})\n'
        ),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_js_guard,
        commit_blob=commit_js_guard,
        head_blob=(
            "x = 1\nguard.enabled = true\ny = 2\n"
            "Object.defineProperty(guard, key, {value: false})\n"
        ),
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_js_guard,
        commit_blob=commit_js_guard,
        head_blob=(
            "x = 1\nguard.enabled = true\ny = 2\n"
            '// Object.defineProperty(guard, "enabled", {value: false})\n'
        ),
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_js_guard,
        commit_blob=commit_js_guard,
        head_blob=(
            "x = 1\nguard.enabled = true\ny = 2\n"
            'Object.defineProperty(other, "enabled", {value: false})\n'
        ),
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_js_guard,
        commit_blob=commit_js_guard,
        head_blob=(
            "x = 1\nguard.enabled = true\ny = 2\n"
            'Object.defineProperty(guard, "other", {value: false})\n'
        ),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_js_guard,
        commit_blob=commit_js_guard,
        head_blob=(
            "x = 1\nguard.enabled = true\ny = 2\n"
            'Object.defineProperty(\n  guard,\n  "enabled",\n  {value: false}\n);\n'
        ),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_js_guard,
        commit_blob=commit_js_guard,
        head_blob=(
            "x = 1\nguard.enabled = true\ny = 2\n"
            "Object.defineProperty(\n  guard,\n  key,\n  {value: false}\n);\n"
        ),
    )
    # Shared defineProperty opener: tip only edits the value / property lines
    # (PRRT_kwDOSJAM6s6Zy5DN).
    parent_shared_define = 'Object.defineProperty(\n  guard,\n  "enabled",\n  {value: false}\n);\n'
    commit_shared_define = 'Object.defineProperty(\n  guard,\n  "enabled",\n  {value: true}\n);\n'
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_shared_define,
        commit_blob=commit_shared_define,
        head_blob=('Object.defineProperty(\n  guard,\n  "enabled",\n  {value: false}\n);\n'),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_shared_define,
        commit_blob=commit_shared_define,
        head_blob=("Object.defineProperty(\n  guard,\n  key,\n  {value: false}\n);\n"),
    )
    # Reflect.set after salvage ``guard.enabled = true`` (PRRT_kwDOSJAM6s6ZzN-l).
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_js_guard,
        commit_blob=commit_js_guard,
        head_blob=('x = 1\nguard.enabled = true\ny = 2\nReflect.set(guard, "enabled", false)\n'),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_js_guard,
        commit_blob=commit_js_guard,
        head_blob=("x = 1\nguard.enabled = true\ny = 2\nReflect.set(guard, key, false)\n"),
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_js_guard,
        commit_blob=commit_js_guard,
        head_blob=('x = 1\nguard.enabled = true\ny = 2\n// Reflect.set(guard, "enabled", false)\n'),
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_js_guard,
        commit_blob=commit_js_guard,
        head_blob=('x = 1\nguard.enabled = true\ny = 2\nReflect.set(other, "enabled", false)\n'),
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_js_guard,
        commit_blob=commit_js_guard,
        head_blob=('x = 1\nguard.enabled = true\ny = 2\nReflect.set(guard, "other", false)\n'),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_js_guard,
        commit_blob=commit_js_guard,
        head_blob=(
            "x = 1\nguard.enabled = true\ny = 2\n"
            'Reflect.set(\n  guard,\n  "enabled",\n  false\n);\n'
        ),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_js_guard,
        commit_blob=commit_js_guard,
        head_blob=(
            "x = 1\nguard.enabled = true\ny = 2\nReflect.set(\n  guard,\n  key,\n  false\n);\n"
        ),
    )
    parent_shared_reflect = 'Reflect.set(\n  guard,\n  "enabled",\n  false\n);\n'
    commit_shared_reflect = 'Reflect.set(\n  guard,\n  "enabled",\n  true\n);\n'
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_shared_reflect,
        commit_blob=commit_shared_reflect,
        head_blob='Reflect.set(\n  guard,\n  "enabled",\n  false\n);\n',
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_shared_reflect,
        commit_blob=commit_shared_reflect,
        head_blob="Reflect.set(\n  guard,\n  key,\n  false\n);\n",
    )
    # Object.defineProperties after salvage ``guard.enabled = true``
    # (PRRT_kwDOSJAM6s6ZzifG).
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_js_guard,
        commit_blob=commit_js_guard,
        head_blob=(
            "x = 1\nguard.enabled = true\ny = 2\n"
            "Object.defineProperties(guard, {enabled: {value: false}})\n"
        ),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_js_guard,
        commit_blob=commit_js_guard,
        head_blob=("x = 1\nguard.enabled = true\ny = 2\nObject.defineProperties(guard, props)\n"),
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_js_guard,
        commit_blob=commit_js_guard,
        head_blob=(
            "x = 1\nguard.enabled = true\ny = 2\n"
            "// Object.defineProperties(guard, {enabled: {value: false}})\n"
        ),
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_js_guard,
        commit_blob=commit_js_guard,
        head_blob=(
            "x = 1\nguard.enabled = true\ny = 2\n"
            "Object.defineProperties(other, {enabled: {value: false}})\n"
        ),
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_js_guard,
        commit_blob=commit_js_guard,
        head_blob=(
            "x = 1\nguard.enabled = true\ny = 2\n"
            "Object.defineProperties(guard, {other: {value: false}})\n"
        ),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_js_guard,
        commit_blob=commit_js_guard,
        head_blob=(
            "x = 1\nguard.enabled = true\ny = 2\n"
            "Object.defineProperties(\n  guard,\n  {enabled: {value: false}}\n);\n"
        ),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_js_guard,
        commit_blob=commit_js_guard,
        head_blob=(
            "x = 1\nguard.enabled = true\ny = 2\nObject.defineProperties(\n  guard,\n  props\n);\n"
        ),
    )
    parent_shared_defines = "Object.defineProperties(\n  guard,\n  {enabled: {value: false}}\n);\n"
    commit_shared_defines = "Object.defineProperties(\n  guard,\n  {enabled: {value: true}}\n);\n"
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_shared_defines,
        commit_blob=commit_shared_defines,
        head_blob="Object.defineProperties(\n  guard,\n  {enabled: {value: false}}\n);\n",
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_shared_defines,
        commit_blob=commit_shared_defines,
        head_blob="Object.defineProperties(\n  guard,\n  props\n);\n",
    )


@pytest.mark.unit
def test_added_salvage_rejects_nonliteral_subscript_on_salvaged_receiver() -> None:
    """Tip ``FLAGS[key] =`` after added ``FLAGS["enabled"]`` salvage must not retain.

    Exact-key intersection misses computed indices; fail closed when the
    tip-extra nonliteral subscript shares the salvaged receiver
    (PRRT_kwDOSJAM6s6Zv4pe).
    """
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass import (
        _added_salvage_blob_retained,
    )

    salvage = 'FLAGS = {}\nFLAGS["enabled"] = True\n'
    assert not _added_salvage_blob_retained(
        commit_blob=salvage,
        head_blob=salvage + 'key = "enabled"\nFLAGS[key] = False\n',
    )
    assert not _added_salvage_blob_retained(
        commit_blob=salvage,
        head_blob=salvage + "FLAGS[key] = False\n",
    )
    # Surplus identical salvage binding + tip-extra ``FLAGS[key]`` must still
    # reject retention (PRRT_kwDOSJAM6s6ZwnzM).
    assert not _added_salvage_blob_retained(
        commit_blob=salvage,
        head_blob=salvage + 'FLAGS["enabled"] = True\nFLAGS[key] = False\n',
    )
    assert _added_salvage_blob_retained(
        commit_blob=salvage,
        head_blob=salvage + 'key = "enabled"\nOTHER[key] = False\n',
    )
    assert _added_salvage_blob_retained(
        commit_blob=salvage,
        head_blob=salvage + "FLAGS[0] = False\n",
    )
