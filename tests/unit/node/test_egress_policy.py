"""Local Docker egress policy tests."""

from __future__ import annotations

import pytest

from awf.node.egress_policy import (
    LocalEgressPlan,
    LocalEgressPolicyError,
    local_egress_plan,
)
from awf.profiles.models import EgressMode, ProfileEgress


@pytest.mark.unit
def test_local_egress_plan_is_explicitly_unhashable() -> None:
    plan = local_egress_plan(ProfileEgress())

    assert LocalEgressPlan.__hash__ is None
    with pytest.raises(TypeError, match="unhashable type: 'LocalEgressPlan'"):
        hash(plan)


@pytest.mark.unit
def test_local_egress_plan_open_is_unrestricted_public_network() -> None:
    egress = ProfileEgress(mode=EgressMode.open)

    plan = local_egress_plan(egress)

    assert plan.network_internal is False
    assert plan.host_gateway_enabled is True
    assert plan.reason_code == "LOCAL_EGRESS_OPEN_UNRESTRICTED"
    assert plan.details["internet_access"] == "unrestricted"


@pytest.mark.unit
def test_local_egress_plan_offline_is_internal_without_host_gateway() -> None:
    plan = local_egress_plan(ProfileEgress(mode=EgressMode.offline))

    assert plan.network_internal is True
    assert plan.host_gateway_enabled is False
    assert plan.reason_code == "LOCAL_EGRESS_OFFLINE_NETWORK"


@pytest.mark.unit
def test_local_egress_plan_restricted_is_conservative_local_internal_network() -> None:
    plan = local_egress_plan(ProfileEgress(mode=EgressMode.restricted))

    assert plan.network_internal is True
    assert plan.host_gateway_enabled is False
    assert plan.reason_code == "LOCAL_EGRESS_RESTRICTED_LOCAL_ONLY"
    assert plan.details["destination_filtering"] == "deferred"


@pytest.mark.unit
def test_local_egress_policy_error_carries_operator_context() -> None:
    error = LocalEgressPolicyError(
        reason_code="LOCAL_EGRESS_MODE_UNSUPPORTED",
        mode=EgressMode.restricted,
        message="restricted egress is unsupported by this backend",
        details={"network_posture": "restricted"},
    )

    assert error.reason_code == "LOCAL_EGRESS_MODE_UNSUPPORTED"
    assert error.mode == "restricted"
    assert error.details == {"network_posture": "restricted"}
    assert str(error) == (
        "LOCAL_EGRESS_MODE_UNSUPPORTED: restricted egress is unsupported by this backend"
    )
