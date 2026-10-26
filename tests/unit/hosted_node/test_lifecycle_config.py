import pytest
from pydantic import ValidationError

from awf.hosted_node.config import HostedSettings


def config(**kwargs):
    return HostedSettings(
        owner="f28674ae-72bc-49df-8a5c-a758e1099f4b/nodea",
        recovery_generation=1,
        controller_token="synthetic-controller-only",
        material_root="/unused",
        **kwargs,
    )


def test_local_hosted_defaults_unchanged():
    assert config(incarnation="legacy-nonnumeric").lifecycle is None


@pytest.mark.parametrize("physical", ["0", "-1", "01", "1.0", "vm-name", str(2**64)])
def test_lifecycle_requires_canonical_numeric_physical_identity(physical):
    with pytest.raises(ValidationError):
        config(incarnation=physical, lifecycle={"disk_ref": "disk", "database_ref": "db"})
