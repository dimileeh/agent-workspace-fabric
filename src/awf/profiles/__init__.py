"""Workspace profile primitives.

Profiles are the project-specific layer of AWF. The control plane remains
generic; profiles describe runtime services, Docker requirements, setup and
validation phases, and project-specific environment.
"""

from awf.profiles.lint import LintIssue, LintSeverity, lint_profile
from awf.profiles.models import (
    DockerMode,
    ProfileCommand,
    ProfileDocker,
    ProfilePhaseSet,
    ProfileResolution,
    ProfileRuntime,
    ProfileService,
    WorkspaceProfile,
)
from awf.profiles.resolver import ProfileResolver, resolve_workspace_profile

__all__ = [
    "DockerMode",
    "LintIssue",
    "LintSeverity",
    "ProfileCommand",
    "ProfileDocker",
    "ProfilePhaseSet",
    "ProfileResolution",
    "ProfileRuntime",
    "ProfileResolver",
    "ProfileService",
    "WorkspaceProfile",
    "lint_profile",
    "resolve_workspace_profile",
]
