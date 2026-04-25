"""Built-in workspace profiles and lightweight detectors."""

from __future__ import annotations

import json
from pathlib import Path

from awf.profiles.models import (
    DockerMode,
    ProfileCommand,
    ProfileDocker,
    ProfileRuntime,
    ProfileService,
    ProfileValidation,
    WorkspaceProfile,
)


def generic_profile(*, source: str = "builtin:generic") -> WorkspaceProfile:
    return WorkspaceProfile(
        name="generic",
        source=source,
        confidence="low",
        description="Agent-only workspace. Caller or repo profile supplies validation.",
    )


def python_profile(*, source: str = "builtin:python") -> WorkspaceProfile:
    return WorkspaceProfile(
        name="python",
        source=source,
        confidence="medium",
        description="Generic Python project profile.",
        phases={
            "setup": [ProfileCommand(command='uv pip install -e ".[dev]"')],
            "validate": [ProfileCommand(command="pytest -q")],
        },
    )


def node_profile(*, package_manager: str = "npm", source: str = "builtin:node") -> WorkspaceProfile:
    install = {
        "pnpm": "pnpm install --frozen-lockfile",
        "yarn": "yarn install --frozen-lockfile",
        "bun": "bun install --frozen-lockfile",
        "npm": "npm ci",
    }.get(package_manager, "npm ci")
    test = {
        "pnpm": "pnpm test",
        "yarn": "yarn test",
        "bun": "bun test",
        "npm": "npm test",
    }.get(package_manager, "npm test")
    return WorkspaceProfile(
        name="node",
        source=source,
        confidence="medium",
        description=f"Generic Node.js project profile using {package_manager}.",
        phases={"setup": [install], "validate": [test]},
    )


def nextjs_profile(
    *, package_manager: str = "npm", source: str = "builtin:nextjs"
) -> WorkspaceProfile:
    base = node_profile(package_manager=package_manager, source=source)
    lint = {
        "pnpm": "pnpm lint",
        "yarn": "yarn lint",
        "bun": "bun run lint",
        "npm": "npm run lint",
    }.get(package_manager, "npm run lint")
    return base.model_copy(
        update={
            "name": "nextjs",
            "description": f"Next.js profile using {package_manager}.",
            "phases": base.phases.model_copy(
                update={
                    "validate_commands": [
                        ProfileCommand(command=lint),
                        *base.phases.validate_commands,
                    ]
                }
            ),
        }
    )


def docker_compose_profile(*, source: str = "builtin:docker-compose") -> WorkspaceProfile:
    return WorkspaceProfile(
        name="docker-compose",
        source=source,
        confidence="medium",
        description="Project-owned Docker Compose stack running inside workspace DinD.",
        docker=ProfileDocker(mode=DockerMode.dind),
        runtime=ProfileRuntime(environment={"DOCKER_HOST": "tcp://docker:2375"}),
        phases={
            "setup": ["docker compose up -d --wait"],
            "cleanup": ["docker compose down -v --remove-orphans"],
        },
    )


def aira_profile(*, source: str = "builtin:aira") -> WorkspaceProfile:
    postgres_url = "postgresql+asyncpg://awf:${AWF_POSTGRES_PASSWORD}@postgres:5432/awf"
    return WorkspaceProfile(
        name="aira",
        source=source,
        confidence="high",
        description="Aira compatibility profile: pgvector Postgres plus Python/Alembic defaults.",
        runtime=ProfileRuntime(
            environment={
                "DATABASE_URL": "postgresql+psycopg://awf:${AWF_POSTGRES_PASSWORD}@postgres:5432/awf",
                "AIRA_DATABASE_URL": postgres_url,
            }
        ),
        services=[
            ProfileService(
                name="postgres",
                image="pgvector/pgvector:pg18",
                environment={
                    "POSTGRES_USER": "awf",
                    "POSTGRES_PASSWORD": "${AWF_POSTGRES_PASSWORD}",
                    "POSTGRES_DB": "awf",
                },
                healthcheck_cmd="pg_isready -U awf -d awf",
                volumes=[("postgres_data", "/var/lib/postgresql")],
            )
        ],
        phases={
            "setup": ['uv pip install -e ".[dev]"', "alembic upgrade head"],
            "validate": ["pytest -q"],
        },
        validation=ProfileValidation(requested_tier=1),
    )


BUILTIN_PROFILES: dict[str, WorkspaceProfile] = {
    "generic": generic_profile(),
    "python": python_profile(),
    "node": node_profile(),
    "nextjs": nextjs_profile(),
    "docker-compose": docker_compose_profile(),
    "aira": aira_profile(),
}


def get_builtin_profile(name: str) -> WorkspaceProfile | None:
    profile = BUILTIN_PROFILES.get(name)
    return profile.model_copy(deep=True) if profile is not None else None


def detect_profile(worktree_path: Path) -> WorkspaceProfile | None:
    """Return the best built-in profile for a checked-out repo."""
    if _looks_like_aira(worktree_path):
        return aira_profile(source="detector:aira")
    if _has_compose_file(worktree_path):
        return docker_compose_profile(source="detector:docker-compose")
    package_json = worktree_path / "package.json"
    if package_json.is_file():
        package_manager = _detect_package_manager(worktree_path)
        if _looks_like_nextjs(package_json):
            return nextjs_profile(package_manager=package_manager, source="detector:nextjs")
        return node_profile(package_manager=package_manager, source="detector:node")
    if (worktree_path / "pyproject.toml").is_file() or (
        worktree_path / "requirements.txt"
    ).is_file():
        return python_profile(source="detector:python")
    return None


def _has_compose_file(worktree_path: Path) -> bool:
    return any(
        (worktree_path / name).is_file()
        for name in (
            "compose.yml",
            "compose.yaml",
            "docker-compose.yml",
            "docker-compose.yaml",
        )
    )


def _detect_package_manager(worktree_path: Path) -> str:
    if (worktree_path / "pnpm-lock.yaml").is_file():
        return "pnpm"
    if (worktree_path / "yarn.lock").is_file():
        return "yarn"
    if (worktree_path / "bun.lockb").is_file() or (worktree_path / "bun.lock").is_file():
        return "bun"
    return "npm"


def _looks_like_nextjs(package_json: Path) -> bool:
    try:
        data = json.loads(package_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    deps = {}
    for key in ("dependencies", "devDependencies"):
        value = data.get(key)
        if isinstance(value, dict):
            deps.update(value)
    return "next" in deps


def _looks_like_aira(worktree_path: Path) -> bool:
    # Keep this intentionally narrow: the compatibility profile should only
    # auto-select for repos that clearly belong to the Aira stack.
    pyproject = worktree_path / "pyproject.toml"
    if not pyproject.is_file():
        return False
    try:
        text = pyproject.read_text(encoding="utf-8")
    except OSError:
        return False
    return "aira-agent" in text or "aira_agent" in text
