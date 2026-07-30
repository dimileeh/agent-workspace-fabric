"""Shared auto-merge intent resolution.

``auto_merge`` is one uniform, opt-in setting that behaves identically whether a
workspace is created (``awf workspace create``) or an existing PR is adopted
(``awf workspace adopt-pr``). The persisted ``workspace.auto_merge`` column is the
single authority for monitor selection; ``task_kind`` never affects it.

The create/adopt request carries a tri-state *intent* (``True``/``False``/``None``
where ``None`` means "unset — fall through to the profile/default"). The intent is
persisted durably in ``task_policy[AUTO_MERGE_INTENT_POLICY_KEY]`` so idempotent
replays compare a stable snapshot and so the resolver can re-run at provision time
(when the resolved ``workspace.yml`` profile is finally materialized).

Precedence (highest wins):

1. per-task intent (``--auto-merge``/``--no-auto-merge``; ``None`` = unset)
2. ``monitor.auto_merge.by_base_branch[<PR base branch>]`` (exact match)
3. ``monitor.auto_merge.default`` (repo global)
4. ``DEFAULT_AUTO_MERGE`` (``False``)

This module is pure (no I/O) so both the create/adopt idempotency comparisons and
the provisioner call the same code.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from awf.db.enums import WorkspaceStatus

if TYPE_CHECKING:
    from awf.profiles.models import WorkspaceProfile

#: The single source of truth for the uniform, opt-in default. Import this rather
#: than re-typing the literal ``False`` in schemas/CLI/MCP/repo defaults.
DEFAULT_AUTO_MERGE = False

#: Statuses in which the provisioner has not yet resolved the final ``auto_merge``
#: flag from the materialized profile, so the persisted column is still the
#: provisional :func:`seed_auto_merge` value rather than the authoritative policy.
AUTO_MERGE_UNRESOLVED_STATUSES = frozenset(
    {WorkspaceStatus.requested.value, WorkspaceStatus.provisioning.value}
)

#: The ``task_policy`` key under which the raw tri-state intent is persisted.
#: Import this rather than re-typing the string literal.
AUTO_MERGE_INTENT_POLICY_KEY = "auto_merge_intent"


def auto_merge_intent_from_policy(task_policy: Mapping[str, Any] | None) -> bool | None:
    """Read the persisted tri-state auto-merge intent from a task policy.

    Returns ``True``/``False`` when an explicit intent was persisted, or ``None``
    when the intent is unset. Rows written before this key existed (legacy) and
    any non-bool stored value normalize to ``None`` (unset) so the resolver falls
    through to the profile/default; legacy idempotency mapping is handled by the
    caller, not here.
    """
    if not isinstance(task_policy, Mapping):
        return None
    value = task_policy.get(AUTO_MERGE_INTENT_POLICY_KEY)
    if isinstance(value, bool):
        return value
    return None


def task_policy_has_auto_merge_intent(task_policy: Mapping[str, Any] | None) -> bool:
    """Whether a task policy carries an explicit auto-merge intent key.

    New-world rows always persist ``AUTO_MERGE_INTENT_POLICY_KEY`` (even for an
    unset ``None`` intent). Legacy rows written before the key existed lack it
    entirely; callers use this to preserve a grandfathered persisted
    ``workspace.auto_merge`` column instead of resolving such a row as a fresh
    unset intent (which would clobber a grandfathered ``True`` with the profile's
    new default). This is a strict *presence* check, distinct from
    :func:`auto_merge_intent_from_policy`, which collapses both an absent key and a
    present ``None`` to ``None``.
    """
    return isinstance(task_policy, Mapping) and AUTO_MERGE_INTENT_POLICY_KEY in task_policy


def seed_auto_merge(intent: bool | None) -> bool:
    """Seed the provisional ``workspace.auto_merge`` column from a tri-state intent.

    Create/adopt persist the workspace row before the profile is resolved, so the
    column starts at the explicit intent when one was given and otherwise at the
    conservative ``DEFAULT_AUTO_MERGE`` seed. The provisioner later re-runs
    :func:`resolve_auto_merge` against the materialized profile and overwrites an
    unset seed, so this rule must stay identical on both entry paths.
    """
    return DEFAULT_AUTO_MERGE if intent is None else intent


def auto_merge_is_resolved(status: str, task_policy: Mapping[str, Any] | None) -> bool:
    """Whether the persisted ``workspace.auto_merge`` column is authoritative.

    Create and adopt persist a provisional :func:`seed_auto_merge` value before the
    profile exists, so a row with an unset intent that is still
    ``requested``/``provisioning`` carries ``DEFAULT_AUTO_MERGE`` even when the
    profile's ``monitor.auto_merge`` will resolve it on. The column becomes
    authoritative once any of the following holds:

    * an explicit intent was given — :func:`resolve_auto_merge` already returns it;
    * the row has left the pre-provisioning statuses — the resolver has run;
    * the row is legacy (no persisted intent key) — the provisioner preserves the
      grandfathered column instead of re-resolving it, so it is already final.
    """
    return (
        auto_merge_intent_from_policy(task_policy) is not None
        or status not in AUTO_MERGE_UNRESOLVED_STATUSES
        or not task_policy_has_auto_merge_intent(task_policy)
    )


def reported_auto_merge(
    status: str,
    task_policy: Mapping[str, Any] | None,
    auto_merge: bool,
) -> bool | None:
    """Project the persisted column for API responses, or ``None`` when unresolved.

    Reporting the provisional seed would advertise a ``manual`` merge policy for a
    workspace whose profile resolves auto-merge on, so surface the value only once
    :func:`auto_merge_is_resolved` says it is settled.
    """
    return auto_merge if auto_merge_is_resolved(status, task_policy) else None


def resolve_auto_merge(
    intent: bool | None,
    resolved_profile: WorkspaceProfile,
    base_branch: str,
) -> bool:
    """Resolve the final auto-merge flag from intent + resolved profile config.

    ``intent`` is the per-task tri-state (``True``/``False`` explicit, ``None``
    unset). ``resolved_profile`` is the reconstructed ``WorkspaceProfile`` (never a
    raw dict). ``base_branch`` is the PR base/target branch matched exactly against
    ``monitor.auto_merge.by_base_branch``. See the module docstring for the
    precedence chain.
    """
    if intent is not None:
        return intent
    config = resolved_profile.monitor.auto_merge
    by_base = config.by_base_branch.get(base_branch)
    if by_base is not None:
        return by_base
    return config.default
