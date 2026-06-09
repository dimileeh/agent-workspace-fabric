"""Local workspace secret lease orchestration.

This module records control-plane metadata for profile-declared secrets. It
does not fetch, store, mount, or broker secret values.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from awf.api.schemas import WorkspaceSecretLeaseResponse
from awf.db.models import Workspace, WorkspaceSecretLease
from awf.db.repositories import SecretLeaseIssue, SecretLeaseRepository
from awf.profiles.models import ProfileSecret, WorkspaceProfile

DEFAULT_SECRET_LEASE_TTL_SECONDS = 12 * 60 * 60
TERMINAL_CLEANUP_REVOKE_REASON = "TERMINAL_CLEANUP"
TERMINAL_GC_REVOKE_REASON = "TERMINAL_GC"
PROVISIONING_FAILED_REVOKE_REASON = "PROVISIONING_FAILED"


class SecretLeaseService:
    """Service layer for non-secret local lease records and projections."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repo = SecretLeaseRepository(session)

    async def issue_profile_secret_leases(
        self,
        workspace: Workspace,
        profile: WorkspaceProfile,
        *,
        now: datetime | None = None,
        ttl_seconds: int = DEFAULT_SECRET_LEASE_TTL_SECONDS,
    ) -> list[WorkspaceSecretLease]:
        issued_at = _coerce_now(now)
        expires_at = issued_at + timedelta(seconds=ttl_seconds)
        issues = [
            _lease_issue_from_profile_secret(
                secret,
                profile=profile,
                declaration_index=index,
                expires_at=expires_at,
            )
            for index, secret in enumerate(profile.secrets)
        ]
        if not issues:
            return []
        return await self._repo.issue_declared_leases(workspace, leases=issues, now=issued_at)

    async def record_secret_lease_mounts(
        self,
        workspace: Workspace,
        *,
        now: datetime | None = None,
        mount_metadata: dict[str, Any] | None = None,
    ) -> list[WorkspaceSecretLease]:
        return await self._repo.mark_issued_mounted(
            workspace,
            now=_coerce_now(now),
            mount_metadata=mount_metadata,
        )

    async def expire_due_secret_leases(
        self,
        *,
        now: datetime | None = None,
    ) -> list[WorkspaceSecretLease]:
        return await self._repo.expire_due_leases(now=_coerce_now(now))

    async def revoke_workspace_secret_leases(
        self,
        workspace: Workspace,
        *,
        now: datetime | None = None,
        reason_code: str,
    ) -> list[WorkspaceSecretLease]:
        return await self._repo.revoke_workspace_leases(
            workspace,
            now=_coerce_now(now),
            reason_code=reason_code,
        )

    async def workspace_secret_lease_status(
        self,
        workspace_id: str,
    ) -> list[WorkspaceSecretLeaseResponse]:
        leases = await self._repo.list_for_workspace(workspace_id)
        return [
            workspace_secret_lease_response(lease)
            for lease in sorted(leases, key=_lease_projection_sort_key)
        ]


def workspace_secret_lease_response(
    lease: WorkspaceSecretLease,
) -> WorkspaceSecretLeaseResponse:
    return WorkspaceSecretLeaseResponse(
        lease_id=lease.id,
        secret_name=lease.secret_name,
        kind=lease.kind,
        target=lease.target,
        status=lease.status,
        provider=lease.provider,
        ref_digest=lease.ref_digest,
        issued_at=_ensure_utc(lease.issued_at),
        mounted_at=_ensure_utc_or_none(lease.mounted_at),
        expires_at=_ensure_utc_or_none(lease.expires_at),
        revoked_at=_ensure_utc_or_none(lease.revoked_at),
    )


def secret_lease_revocation_summary(
    leases: list[WorkspaceSecretLease],
    *,
    reason_code: str,
) -> dict[str, Any]:
    return {
        "revoked_count": len(leases),
        "reason_code": reason_code,
    }


def _lease_issue_from_profile_secret(
    secret: ProfileSecret,
    *,
    profile: WorkspaceProfile,
    declaration_index: int,
    expires_at: datetime,
) -> SecretLeaseIssue:
    return SecretLeaseIssue(
        secret_name=secret.name,
        kind=secret.kind,
        target=secret.target,
        mode=secret.mode,
        required=secret.required,
        provider=secret.provider,
        ref_digest=_profile_secret_ref_digest(secret),
        expires_at=expires_at,
        issue_metadata={
            "schema": "secret_lease_issue_metadata.v1",
            "profile_name": profile.name,
            "profile_source": profile.source,
            "declaration_index": declaration_index,
        },
    )


def _profile_secret_ref_digest(secret: ProfileSecret) -> str | None:
    if secret.ref is None:
        return None
    provider = secret.provider or "unspecified"
    payload = f"{provider}\0{secret.ref}".encode()
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _lease_projection_sort_key(lease: WorkspaceSecretLease) -> tuple[datetime, int, str]:
    declaration_index = lease.issue_metadata.get("declaration_index")
    if not isinstance(declaration_index, int):
        declaration_index = 0
    return (lease.issued_at, declaration_index, lease.id)


def _coerce_now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(UTC)
    if now.tzinfo is None:
        return now.replace(tzinfo=UTC)
    return now


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _ensure_utc_or_none(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return _ensure_utc(value)
