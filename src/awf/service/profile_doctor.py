"""Real profile-readiness preflight for ``awf profile doctor`` (issue #527).

Operators onboarding a new project run ``awf smoke`` (and ``--mocked-local``),
see green, then hit real failures the first time a workspace actually provisions
— the most painful being ``SECRET_LEASE_SOURCE_MISSING`` (a ``provider:
local-file`` secret whose source path is not visible to the worker context). The
mocked smoke never resolves the *real* profile, the real secret leases, the real
egress posture, or the real service images, so a green smoke is mistaken for real
readiness.

``collect_profile_doctor_report`` is a **read-only, host-context preflight**: it
resolves the RESOLVED profile and runs the *same* probes provisioning uses
(resolver + lint, secret-lease mount resolver, egress policy, image inspect).
There is NO agent run, NO workspace creation, and NO PR. The report mirrors the
smoke phase shape (``name``/``status``/``reason_code``/``message``/``evidence``/
``action``) plus a top-level ``status``/``repo``/``phases``/``next_actions``, so
it stays visually consistent with ``awf smoke`` which operators already read.
"""

from __future__ import annotations

import subprocess
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from awf.node.egress_policy import LocalEgressPolicyError, local_egress_plan
from awf.node.secret_mounts import (
    LocalSecretLeaseMountResolver,
    SecretLeaseResolutionError,
)
from awf.profiles.models import (
    DockerMode,
    EgressMode,
    ProfileLintFinding,
    ProfileLintSeverity,
    ProfileResolution,
    WorkspaceProfile,
)
from awf.profiles.resolver import ProfileResolutionError, resolve_workspace_profile
from awf.service.report_shape import collect_next_actions, compute_overall_status

# Synthetic, path-safe workspace id handed to the secret-lease resolver. It never
# touches real workspace state — the resolver only uses it to name a throwaway
# askpass subdir under ``work_dir`` (a temp dir here) and validates its shape
# (non-empty, no ``/`` ``\`` ``.`` ``..``).
DOCTOR_WORKSPACE_ID = "ws_doctor_preflight"

# Profile-resolution phase reason codes.
PROFILE_DOCTOR_PROFILE_RESOLVED = "PROFILE_DOCTOR_PROFILE_RESOLVED"
PROFILE_RESOLUTION_FAILED = "PROFILE_RESOLUTION_FAILED"
PROFILE_DOCTOR_PROFILE_UNRESOLVED = "PROFILE_DOCTOR_PROFILE_UNRESOLVED"

# Lint phase reason codes.
PROFILE_DOCTOR_LINT_CLEAN = "PROFILE_DOCTOR_LINT_CLEAN"
PROFILE_DOCTOR_LINT_WARNINGS = "PROFILE_DOCTOR_LINT_WARNINGS"
PROFILE_DOCTOR_LINT_ERRORS = "PROFILE_DOCTOR_LINT_ERRORS"

# Secret-lease phase reason codes (success/optional; structured failures reuse the
# resolver's own ``reason_code`` — e.g. ``SECRET_LEASE_SOURCE_MISSING``).
PROFILE_DOCTOR_SECRET_LEASES_OK = "PROFILE_DOCTOR_SECRET_LEASES_OK"
PROFILE_DOCTOR_SECRET_LEASES_OPTIONAL_MISSING = "PROFILE_DOCTOR_SECRET_LEASES_OPTIONAL_MISSING"
# An *unexpected* (non-``SecretLeaseResolutionError``) failure while probing — e.g.
# an ``OSError`` from the bitbucket askpass materialization (mkdir/write/chmod).
PROFILE_DOCTOR_SECRET_LEASES_PROBE_ERROR = "PROFILE_DOCTOR_SECRET_LEASES_PROBE_ERROR"

# Docker-image phase reason codes.
DOCKER_MODE_NOT_DIND = "DOCKER_MODE_NOT_DIND"
PROFILE_DOCTOR_IMAGES_PRESENT = "PROFILE_DOCTOR_IMAGES_PRESENT"
PROFILE_DOCTOR_IMAGES_PULLABLE = "PROFILE_DOCTOR_IMAGES_PULLABLE"
PROFILE_DOCTOR_IMAGE_UNREACHABLE = "PROFILE_DOCTOR_IMAGE_UNREACHABLE"
DOCKER_UNAVAILABLE = "DOCKER_UNAVAILABLE"

# Image-probe result vocabulary (the injected ``image_probe`` returns one of
# these). ``present`` = inspectable locally; ``pullable`` = absent locally but the
# registry manifest is reachable; ``unreachable`` = not found / auth-gated;
# ``unavailable`` = the docker CLI/daemon could not be reached at all.
IMAGE_PRESENT = "present"
IMAGE_PULLABLE = "pullable"
IMAGE_UNREACHABLE = "unreachable"
IMAGE_UNAVAILABLE = "unavailable"

ImageProbe = Callable[[str], str]
ProfileResolveFn = Callable[..., ProfileResolution]

_NO_ACTION = "No action required."
_PROBE_TIMEOUT_SECONDS = 30.0


def collect_profile_doctor_report(
    repo: Path,
    *,
    repo_url: str | None = None,
    host_home: Path | None = None,
    host_env: Mapping[str, str] | None = None,
    image_probe: ImageProbe | None = None,
    resolve: ProfileResolveFn | None = None,
) -> dict[str, Any]:
    """Run the real profile-readiness preflight and return a phase-by-phase report.

    Args:
        repo: Path to a checked-out repository to resolve a profile from.
        repo_url: Optional origin URL used for ``forge: auto`` resolution. The CLI
            detects it from the checkout; the service stays subprocess-free.
        host_home: Host home directory the worker context will see (defaults to
            ``Path.home()``); the secret-lease probe resolves against it.
        host_env: Host environment the worker context will see (defaults to
            ``os.environ``); env/github/bitbucket leases resolve against it.
        image_probe: Injectable docker image probe (defaults to a subprocess
            ``docker image inspect``/``docker manifest inspect`` probe). Tests
            always inject a fake so no test touches the daemon.
        resolve: Injectable profile resolver (defaults to
            ``resolve_workspace_profile``).

    Returns:
        Report dict with top-level ``status``/``repo``/``phases``/``next_actions``
        and smoke-style phase dicts. ``status`` is the worst phase status
        (``fail`` > ``warn`` > ``ok``; ``skipped`` is neutral).

    """
    resolve_fn = resolve or resolve_workspace_profile
    resolved_home = host_home if host_home is not None else Path.home()
    probe = image_probe or _default_image_probe

    phases: list[dict[str, Any]] = []
    resolution, resolution_phase = _resolution_phase(repo, resolve=resolve_fn, repo_url=repo_url)
    phases.append(resolution_phase)

    if resolution is None:
        # The profile did not resolve: every downstream probe needs the profile,
        # so emit them as ``skipped`` rather than crashing or fabricating
        # readiness.
        for name in ("profile_lint", "secret_leases", "egress", "docker_images"):
            phases.append(_skipped_phase(name))
    else:
        profile = resolution.profile
        phases.append(_lint_phase(resolution.lint_findings))
        phases.append(_secret_leases_phase(profile, host_home=resolved_home, host_env=host_env))
        phases.append(_egress_phase(profile))
        phases.append(_docker_images_phase(profile, image_probe=probe))

    status = compute_overall_status([phase["status"] for phase in phases])
    next_actions = collect_next_actions(phases)
    return {
        "status": status,
        "repo": str(repo),
        "phases": phases,
        "next_actions": next_actions,
    }


def _resolution_phase(
    repo: Path,
    *,
    resolve: ProfileResolveFn,
    repo_url: str | None,
) -> tuple[ProfileResolution | None, dict[str, Any]]:
    """Resolve the RESOLVED profile from the same sources provisioning uses."""
    try:
        resolution = resolve(worktree_path=repo, repo_url=repo_url)
    except ProfileResolutionError as exc:
        return None, {
            "name": "profile_resolution",
            "status": "fail",
            "reason_code": exc.reason_code or PROFILE_RESOLUTION_FAILED,
            "message": f"Could not resolve a workspace profile: {exc}",
            "evidence": {
                "repo": str(repo),
                "reason_code": exc.reason_code,
                "findings": [
                    {
                        "reason_code": finding.reason_code,
                        "severity": finding.severity.value,
                        "message": finding.message,
                        "path": finding.path,
                    }
                    for finding in exc.findings
                ],
            },
            "action": ("Fix .awf/workspace.yml so it parses and passes schema/lint validation."),
        }

    profile = resolution.profile
    return resolution, {
        "name": "profile_resolution",
        "status": "ok",
        "reason_code": PROFILE_DOCTOR_PROFILE_RESOLVED,
        "message": (
            f"Resolved profile '{profile.name}' from {profile.source} "
            f"({profile.confidence} confidence)."
        ),
        "evidence": {
            "profile": profile.name,
            "source": profile.source,
            "confidence": profile.confidence,
            "network_posture": resolution.network_posture,
            "candidates_considered": list(resolution.candidates_considered),
        },
        "action": _NO_ACTION,
    }


def _lint_phase(findings: list[ProfileLintFinding]) -> dict[str, Any]:
    """Map resolved-profile lint findings to a smoke-style phase.

    The resolver raises on lint *errors*, so in practice this surfaces
    ``warning``-severity findings; the error mapping is kept for robustness (and
    when ``_lint_phase`` is called on a profile that bypassed the resolver gate).
    Findings carry references/paths only — ``lint_workspace_profile`` already
    omits secret values.
    """
    evidence = {
        "findings": [
            {
                "reason_code": finding.reason_code,
                "severity": finding.severity.value,
                "message": finding.message,
                "path": finding.path,
            }
            for finding in findings
        ]
    }
    errors = [f for f in findings if f.severity is ProfileLintSeverity.error]
    warnings = [f for f in findings if f.severity is ProfileLintSeverity.warning]
    if errors:
        return {
            "name": "profile_lint",
            "status": "fail",
            "reason_code": PROFILE_DOCTOR_LINT_ERRORS,
            "message": f"Profile lint reported {len(errors)} blocking error(s).",
            "evidence": evidence,
            "action": "Resolve the blocking profile lint findings before provisioning.",
        }
    if warnings:
        return {
            "name": "profile_lint",
            "status": "warn",
            "reason_code": PROFILE_DOCTOR_LINT_WARNINGS,
            "message": f"Profile lint reported {len(warnings)} warning(s).",
            "evidence": evidence,
            "action": "Review the profile lint warnings; provisioning is not blocked.",
        }
    return {
        "name": "profile_lint",
        "status": "ok",
        "reason_code": PROFILE_DOCTOR_LINT_CLEAN,
        "message": "Profile lint is clean.",
        "evidence": evidence,
        "action": _NO_ACTION,
    }


def _secret_leases_phase(
    profile: WorkspaceProfile,
    *,
    host_home: Path,
    host_env: Mapping[str, str] | None,
) -> dict[str, Any]:
    """Resolve profile secret leases against the worker's host context.

    THE key probe: the operator's painful case is a required ``provider:
    local-file`` (or ``env``) secret whose source is not visible to the worker
    context, which the mocked smoke never checks. A throwaway ``work_dir`` ensures
    any bitbucket askpass materialization writes to a temp dir, never real
    workspace state. The resolver never returns secret values — evidence carries
    only counts, providers, targets, and reason codes.
    """
    with tempfile.TemporaryDirectory(prefix="awf-doctor-secret-leases-") as work_dir:
        resolver = LocalSecretLeaseMountResolver(
            host_home=host_home,
            work_dir=Path(work_dir),
            host_env=host_env,
        )
        try:
            resolution = resolver.resolve(profile, workspace_id=DOCTOR_WORKSPACE_ID)
        except SecretLeaseResolutionError as exc:
            return {
                "name": "secret_leases",
                "status": "fail",
                "reason_code": exc.reason_code,
                "message": (
                    f"Secret lease '{exc.secret_name}' could not be resolved: its source "
                    "is not visible to the worker context (host_home / env)."
                ),
                "evidence": {
                    "secret_name": exc.secret_name,
                    "provider": exc.provider,
                    "target": exc.target,
                    "kind": exc.kind,
                },
                "action": (
                    "Make the secret source available to the worker (host_home / env) "
                    "or mark the lease optional."
                ),
            }
        except OSError as exc:
            # The resolver materializes a bitbucket askpass script (mkdir / write /
            # chmod) when a git-token lease resolves, so an unexpected filesystem
            # error (e.g. PermissionError) can escape the structured
            # SecretLeaseResolutionError path. Surface it as a phase failure rather
            # than an uncaught traceback so the doctor still reports a usable
            # report. The exception message can embed a source-adjacent path, so
            # evidence carries only the error class name, never ``str(exc)``.
            return {
                "name": "secret_leases",
                "status": "fail",
                "reason_code": PROFILE_DOCTOR_SECRET_LEASES_PROBE_ERROR,
                "message": (
                    "Secret lease resolution failed unexpectedly while probing the worker "
                    "context; the worker would hit the same error during provisioning."
                ),
                "evidence": {"error": type(exc).__name__},
                "action": (
                    "Inspect the worker host context (filesystem permissions / paths) "
                    "and re-run the preflight."
                ),
            }

    metadata = resolution.metadata
    evidence: dict[str, Any] = {
        "providers": list(metadata.get("providers", [])),
        "targets": list(metadata.get("targets", [])),
        "env_count": metadata.get("env_count", 0),
        "mount_count": metadata.get("mount_count", 0),
    }
    skipped_unresolved = metadata.get("skipped_unresolved_count", 0)
    if skipped_unresolved:
        evidence["skipped_unresolved_count"] = skipped_unresolved
    omitted_optional = list(metadata.get("omitted_optional", []))
    if omitted_optional:
        evidence["omitted_optional"] = omitted_optional
        return {
            "name": "secret_leases",
            "status": "warn",
            "reason_code": PROFILE_DOCTOR_SECRET_LEASES_OPTIONAL_MISSING,
            "message": (
                f"{len(omitted_optional)} optional secret lease(s) have no source "
                "visible to the worker context; they will be omitted."
            ),
            "evidence": evidence,
            "action": (
                "Provide the optional secret sources if those leases are needed, "
                "or leave them omitted."
            ),
        }
    return {
        "name": "secret_leases",
        "status": "ok",
        "reason_code": PROFILE_DOCTOR_SECRET_LEASES_OK,
        "message": "All required secret leases resolve against the worker context.",
        "evidence": evidence,
        "action": _NO_ACTION,
    }


def _egress_phase(profile: WorkspaceProfile) -> dict[str, Any]:
    """Report the declared network posture and warn when it may starve bootstrap."""
    try:
        plan = local_egress_plan(profile.security.egress)
    except LocalEgressPolicyError as exc:  # pragma: no cover - enum validation prevents this.
        return {
            "name": "egress",
            "status": "fail",
            "reason_code": exc.reason_code,
            "message": f"Local Docker cannot enforce the declared egress mode: {exc}",
            "evidence": {"mode": exc.mode, "details": exc.details},
            "action": "Choose an egress mode the local Docker backend supports.",
        }

    evidence = {"mode": plan.mode.value, "details": dict(plan.details)}
    if plan.mode == EgressMode.open:
        return {
            "name": "egress",
            "status": "ok",
            "reason_code": plan.reason_code,
            "message": "Egress is open (unrestricted internet access).",
            "evidence": evidence,
            "action": _NO_ACTION,
        }
    return {
        "name": "egress",
        "status": "warn",
        "reason_code": plan.reason_code,
        "message": (
            f"Egress mode '{plan.mode.value}' may be insufficient for bootstrap that "
            "needs the internet (pip/npm install, image pulls)."
        ),
        "evidence": evidence,
        "action": (
            "Widen egress to 'open' if bootstrap needs network access, or confirm the "
            "restricted/offline posture is intended."
        ),
    }


def _docker_images_phase(
    profile: WorkspaceProfile,
    *,
    image_probe: ImageProbe,
) -> dict[str, Any]:
    """Probe service-image + DinD-daemon pullability (skipping locally built services).

    Profile service images (postgres, redis, private app images, ...) are pulled
    by ``docker compose up`` regardless of ``docker.mode`` -- only the managed
    DinD daemon image is conditional on ``docker.mode == dind`` (see
    ``ComposeManager._services_for``). Both are probed here so the phase cannot
    report a green/skipped result while a missing or private service image would
    later break ``docker compose up``.
    """
    images: list[str] = []
    # The managed DinD daemon image is only pulled when docker.mode is dind.
    dind_image = profile.docker.dind_image if profile.docker.mode == DockerMode.dind else None
    # ProfileService enforces mutual exclusivity of image and build_context, so a
    # build-only service always has image=None; filtering on `s.image` therefore
    # correctly excludes locally built services (nothing to pull) without an explicit
    # build_context guard.
    for candidate in (dind_image, *(s.image for s in profile.services if s.image)):
        if candidate and candidate not in images:
            images.append(candidate)

    if not images:
        return {
            "name": "docker_images",
            "status": "skipped",
            "reason_code": DOCKER_MODE_NOT_DIND,
            "message": (
                "docker.mode is not 'dind' and no service images are declared, so "
                "there are no images to pull."
            ),
            "evidence": {"docker_mode": profile.docker.mode.value},
            "action": _NO_ACTION,
        }

    results = [{"image": image, "status": image_probe(image)} for image in images]
    statuses = {result["status"] for result in results}
    evidence = {"images": results}

    if IMAGE_UNREACHABLE in statuses:
        unreachable = [r["image"] for r in results if r["status"] == IMAGE_UNREACHABLE]
        return {
            "name": "docker_images",
            "status": "fail",
            "reason_code": PROFILE_DOCTOR_IMAGE_UNREACHABLE,
            "message": (
                f"{len(unreachable)} image(s) are unreachable or private-gated: "
                f"{', '.join(unreachable)}."
            ),
            "evidence": evidence,
            "action": (
                "Authenticate to the registry or fix the image reference so the worker can pull it."
            ),
        }
    if IMAGE_UNAVAILABLE in statuses:
        return {
            "name": "docker_images",
            "status": "warn",
            "reason_code": DOCKER_UNAVAILABLE,
            "message": (
                "The docker CLI/daemon was unavailable, so image pullability could not be verified."
            ),
            "evidence": evidence,
            "action": "Run this preflight where docker is available to verify image pulls.",
        }
    if IMAGE_PULLABLE in statuses:
        pullable = [r["image"] for r in results if r["status"] == IMAGE_PULLABLE]
        return {
            "name": "docker_images",
            "status": "warn",
            "reason_code": PROFILE_DOCTOR_IMAGES_PULLABLE,
            "message": (
                f"{len(pullable)} image(s) are absent locally but pullable: {', '.join(pullable)}."
            ),
            "evidence": evidence,
            "action": "Pre-pull the images to avoid a first-provision pull delay.",
        }
    return {
        "name": "docker_images",
        "status": "ok",
        "reason_code": PROFILE_DOCTOR_IMAGES_PRESENT,
        "message": f"All {len(results)} required image(s) are present.",
        "evidence": evidence,
        "action": _NO_ACTION,
    }


def _skipped_phase(name: str) -> dict[str, Any]:
    """Build a ``skipped`` phase for probes that need an unresolved profile."""
    return {
        "name": name,
        "status": "skipped",
        "reason_code": PROFILE_DOCTOR_PROFILE_UNRESOLVED,
        "message": "Profile did not resolve; this preflight probe was skipped.",
        "evidence": {},
        "action": _NO_ACTION,
    }


def _default_image_probe(image: str) -> str:
    """Probe a docker image locally, then test registry pullability.

    ``docker image inspect`` confirms a locally present image. When absent,
    ``docker manifest inspect`` tests pullability without a full pull. A missing
    docker CLI/daemon degrades to ``unavailable`` (never an exception): the doctor
    host may have no docker, which must be reported honestly rather than hard-
    failing the whole preflight.
    """
    inspect = _run_docker(["docker", "image", "inspect", image, "--format", "{{.Id}}"])
    if inspect is None:
        return IMAGE_UNAVAILABLE
    if inspect == 0:
        return IMAGE_PRESENT
    manifest = _run_docker(["docker", "manifest", "inspect", image])
    if manifest is None:
        return IMAGE_UNAVAILABLE
    return IMAGE_PULLABLE if manifest == 0 else IMAGE_UNREACHABLE


def _run_docker(args: list[str]) -> int | None:
    """Run a docker command and return its exit code, or ``None`` if unavailable.

    ``None`` means the docker CLI/daemon could not be reached at all (binary
    missing, daemon not running, or the call errored), which the caller maps to
    ``unavailable`` rather than treating as an image failure. When the daemon is
    down, ``docker`` still exits non-zero with a connection error on stderr; we
    detect that here so the preflight reports ``DOCKER_UNAVAILABLE`` rather than a
    hard ``IMAGE_UNREACHABLE`` failure.
    """
    try:
        result = subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    if result.returncode != 0 and result.stderr:
        stderr_lower = result.stderr.lower()
        if (
            "cannot connect to the docker daemon" in stderr_lower
            or "is the docker daemon running" in stderr_lower
            or "error during connect" in stderr_lower
        ):
            return None

    return result.returncode
