"""Local Docker egress policy tests."""

from __future__ import annotations

import pytest

from awf.node.egress_policy import LocalEgressPlan, local_egress_plan
from awf.profiles.models import EgressMode, ProfileEgress


@pytest.mark.unit
def test_local_egress_plan_is_explicitly_unhashable() -> None:
    plan = local_egress_plan(ProfileEgress())

    assert LocalEgressPlan.__hash__ is None
    with pytest.raises(TypeError, match="unhashable type: 'LocalEgressPlan'"):
        hash(plan)


@pytest.mark.unit
def test_local_egress_plan_treats_missing_allowlist_as_empty() -> None:
    egress = ProfileEgress.model_construct(mode=EgressMode.open, allowlist=None)

    plan = local_egress_plan(egress)

    assert plan.details["allowlist_count"] == 0
