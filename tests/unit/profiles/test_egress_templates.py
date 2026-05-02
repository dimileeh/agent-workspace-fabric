"""Egress allowlist template model and policy tests."""

from __future__ import annotations

import pytest

from awf.node.egress_policy import local_egress_plan
from awf.profiles.models import (
    EgressAllowlistTemplate,
    EgressMode,
    ProfileEgress,
    WorkspaceProfile,
)


@pytest.mark.unit
def test_egress_allowlist_template_enum_roundtrips() -> None:
    for template in EgressAllowlistTemplate:
        assert EgressAllowlistTemplate(template.value) == template
        dumped = ProfileEgress(
            mode=EgressMode.restricted,
            allowlist_templates=[template],
        ).model_dump(mode="json")
        assert dumped["allowlist_templates"] == [template.value]


@pytest.mark.unit
def test_profile_egress_parses_allowlist_templates_and_open_explanation() -> None:
    egress = ProfileEgress(
        mode=EgressMode.open,
        allowlist_templates=[
            EgressAllowlistTemplate.github,
            EgressAllowlistTemplate.documentation,
        ],
        open_explanation="Self-dogfood workspace needs broad access.",
    )
    assert egress.allowlist_templates == [
        EgressAllowlistTemplate.github,
        EgressAllowlistTemplate.documentation,
    ]
    assert egress.open_explanation == "Self-dogfood workspace needs broad access."

    dumped = egress.model_dump(mode="json")
    assert dumped["allowlist_templates"] == ["github", "documentation"]
    assert dumped["open_explanation"] == "Self-dogfood workspace needs broad access."


@pytest.mark.unit
def test_profile_egress_rejects_invalid_template() -> None:
    with pytest.raises(ValueError):
        ProfileEgress(mode=EgressMode.restricted, allowlist_templates=["invalid"])  # type: ignore[list-item]


@pytest.mark.unit
def test_local_egress_plan_restricted_includes_templates_in_details() -> None:
    egress = ProfileEgress(
        mode=EgressMode.restricted,
        allowlist_templates=[
            EgressAllowlistTemplate.github,
            EgressAllowlistTemplate.package_registries,
        ],
    )
    plan = local_egress_plan(egress)
    assert plan.details["allowlist_templates"] == ["github", "package_registries"]
    assert plan.details["destination_filtering"] == "deferred"


@pytest.mark.unit
def test_local_egress_plan_open_omits_templates() -> None:
    egress = ProfileEgress(
        mode=EgressMode.open,
        allowlist_templates=[EgressAllowlistTemplate.github],
    )
    plan = local_egress_plan(egress)
    assert "allowlist_templates" not in plan.details
    assert plan.details["internet_access"] == "unrestricted"


@pytest.mark.unit
def test_workspace_profile_defaults_empty_allowlist_templates() -> None:
    profile = WorkspaceProfile(name="test")
    assert profile.security.egress.allowlist_templates == []
    assert profile.security.egress.open_explanation is None
