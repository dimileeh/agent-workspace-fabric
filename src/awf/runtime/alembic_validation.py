"""Profile-gated Alembic migration-chain validation policy helpers."""

from __future__ import annotations

from pathlib import Path

from awf.profiles.models import ProfileAlembicValidation
from awf.service.alembic_resolver import (
    AlembicGraphValidationResult,
    validate_alembic_migration_graph,
)

ALEMBIC_MIGRATION_POLICY_COMMAND = "awf validate alembic migration chain"
ALEMBIC_MIGRATION_POLICY_PHASE = "migration_policy"


def validate_alembic_migration_chain(
    repo_path: Path,
    policy: ProfileAlembicValidation,
) -> AlembicGraphValidationResult:
    """Run the Alembic migration-chain policy against ``repo_path``."""

    return validate_alembic_migration_graph(
        repo_path,
        config_path=policy.config_path,
        script_location=policy.script_location,
    )


def alembic_policy_metadata(
    result: AlembicGraphValidationResult,
    *,
    policy: ProfileAlembicValidation,
) -> dict[str, object]:
    """Return structured, secret-free policy diagnostics for validation records."""

    metadata = result.to_dict()
    metadata["policy"] = {
        "enabled": policy.enabled,
        "config_path": policy.config_path,
        "script_location": policy.script_location,
        "fail_on_unconfigured": policy.fail_on_unconfigured,
    }
    return metadata
