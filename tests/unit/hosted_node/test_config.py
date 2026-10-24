import secrets

import pytest
from pydantic import ValidationError

from awf.common.config import Settings


def test_invalid_hosted_configuration_does_not_print_controller_token():
    token = secrets.token_urlsafe(6)
    with pytest.raises(ValidationError) as caught:
        Settings(
            _env_file=None,
            hosted_node={
                "owner": "owner",
                "incarnation": "vm",
                "recovery_generation": 1,
                "controller_token": token,
                "material_root": "/dev/shm/synthetic",
            },
        )
    leaked = token in str(caught.value)
    assert not leaked, "Configuration validation disclosed its secret input"


def test_defaults_do_not_enable_receiver():
    assert Settings(_env_file=None).hosted_node is None
