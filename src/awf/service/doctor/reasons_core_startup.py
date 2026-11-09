"""Operator-facing doctor reason text for AWF Core startup failures.

Kept beside ``reasons`` so that module stays within the first-party file-line
guardrail; the entries are merged into ``_REASON_TEXT`` at import time.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from awf.db.schema_head import DATABASE_SCHEMA_AHEAD_OF_CODE_REASON_CODE
from awf.service.doctor.reasons_helpers import reason_catalog_link

if TYPE_CHECKING:
    from awf.service.doctor.reasons import _ReasonText


def get_core_startup_reasons(
    reason_text_cls: type[_ReasonText],
) -> dict[str, _ReasonText]:
    """Return the Core startup catalog reason text entries."""
    return {
        "START_COMPOSE_ASSETS_MISSING": reason_text_cls(
            "AWF start could not locate the Compose or runtime assets needed to start local Core.",
            (
                "Use a valid package install or pass a verified `--source-checkout` "
                "path that contains AWF Docker and Compose assets."
            ),
            "The selected package/source asset lane does not contain required bootstrap files.",
            "awf start --source-checkout .",
            reason_catalog_link("START_COMPOSE_ASSETS_MISSING"),
        ),
        "START_PORT_CONFLICT": reason_text_cls(
            "AWF start found a local port conflict.",
            (
                "Stop the process using the reported port or configure a different "
                "AWF API/console port before retrying start."
            ),
            "A local service is already bound to a port needed by AWF Core.",
            "awf start",
            reason_catalog_link("START_PORT_CONFLICT"),
        ),
        "START_MIGRATION_FAILED": reason_text_cls(
            "AWF start could not complete local Core database migrations.",
            (
                "Inspect migration and Postgres logs, fix the database state, then "
                "retry `awf start` or use expert service commands for recovery."
            ),
            "The local control-plane database rejected or failed a required schema migration.",
            "awf service logs --service api",
            reason_catalog_link("START_MIGRATION_FAILED"),
        ),
        "START_HEALTH_TIMEOUT": reason_text_cls(
            "AWF start timed out waiting for local Core health checks.",
            (
                "Inspect API, worker, and Postgres logs, fix the failing service, "
                "then rerun start with an appropriate timeout."
            ),
            "One or more local Core services did not become healthy before the start timeout.",
            "awf service status",
            reason_catalog_link("START_HEALTH_TIMEOUT"),
        ),
        DATABASE_SCHEMA_AHEAD_OF_CODE_REASON_CODE: reason_text_cls(
            "AWF Core refused to start because the database schema is newer than this Core build.",
            (
                "Run the Core build that matches or postdates the database revision, or "
                "restore a database backup that matches this build. Do not downgrade the "
                "database with this build's migrations."
            ),
            (
                "A stale or restored image, or a rollback, is running code whose migration "
                "graph does not contain the database's alembic_version revision."
            ),
            "awf service logs --service api",
            reason_catalog_link(DATABASE_SCHEMA_AHEAD_OF_CODE_REASON_CODE),
        ),
    }
