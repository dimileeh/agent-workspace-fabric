"""Structured types for AWF service doctor diagnostics."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

from awf.service.config import ServiceSettings

DiagnosticStatus = Literal["ok", "warn", "fail", "skipped"]
ReportStatus = Literal["ok", "warn", "fail"]
PathPredicate = Callable[[Path], bool]


class CompletedProcessLike(Protocol):
    @property
    def returncode(self) -> int: ...  # pragma: no cover - Protocol property declaration only.

    @property
    def stdout(self) -> str | None: ...  # pragma: no cover - Protocol property declaration only.

    @property
    def stderr(self) -> str | None: ...  # pragma: no cover - Protocol property declaration only.


class SubprocessRun(Protocol):
    def __call__(  # pragma: no cover - Protocol method declaration only.
        self,
        args: list[str],
        *,
        check: bool,
        capture_output: bool,
        text: Literal[True],
        timeout: float,
        env: Mapping[str, str],
    ) -> CompletedProcessLike: ...


class StatusCollector(Protocol):
    def __call__(  # pragma: no cover - Protocol method declaration only.
        self,
        settings: ServiceSettings,
        *,
        strict_providers: Iterable[str] | None = None,
        provider_environ: Mapping[str, str] | None = None,
    ) -> Awaitable[dict[str, object]]: ...


class SocketLike(Protocol):
    def close(self) -> object: ...  # pragma: no cover - Protocol method declaration only.


class SocketConnector(Protocol):
    def __call__(  # pragma: no cover - Protocol method declaration only.
        self,
        address: tuple[str, int],
        timeout: float,
    ) -> SocketLike: ...


@dataclass(frozen=True, kw_only=True)
class DoctorDiagnostic:
    """One terminal-readable diagnostic with stable structured fields."""

    id: str
    label: str
    status: DiagnosticStatus
    reason: str
    message: str
    action: str
    source: str
    detail: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "id": self.id,
            "label": self.label,
            "status": self.status,
            "reason": self.reason,
            "message": self.message,
            "action": self.action,
            "source": self.source,
            "metadata": dict(self.metadata),
        }
        if self.detail is not None:
            payload["detail"] = self.detail
        return payload


@dataclass(frozen=True, kw_only=True)
class DoctorReport:
    """Structured AWF doctor report."""

    service: str
    status: ReportStatus
    diagnostics: tuple[DoctorDiagnostic, ...]

    @property
    def summary(self) -> dict[str, int]:
        return {
            "ok": sum(1 for diagnostic in self.diagnostics if diagnostic.status == "ok"),
            "warn": sum(1 for diagnostic in self.diagnostics if diagnostic.status == "warn"),
            "fail": sum(1 for diagnostic in self.diagnostics if diagnostic.status == "fail"),
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "service": self.service,
            "status": self.status,
            "summary": self.summary,
            "diagnostics": [diagnostic.to_dict() for diagnostic in self.diagnostics],
        }
