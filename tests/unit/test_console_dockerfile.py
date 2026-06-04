from __future__ import annotations

from pathlib import Path

import pytest


def _console_dockerfile() -> str:
    return Path("apps/console/Dockerfile").read_text(encoding="utf-8")


@pytest.mark.unit
def test_console_docker_context_ignores_generated_host_artifacts() -> None:
    dockerignore = Path(".dockerignore").read_text(encoding="utf-8")

    assert "apps/console/node_modules/" in dockerignore
    assert "apps/console/.next/" in dockerignore
    assert "apps/console/.env.local" in dockerignore
    assert "apps/console/tsconfig.tsbuildinfo" in dockerignore


@pytest.mark.unit
def test_console_runtime_stage_copies_next_config() -> None:
    dockerfile = _console_dockerfile()

    assert "COPY --from=build /app/next.config.ts ./next.config.ts" in dockerfile


@pytest.mark.unit
def test_console_runtime_stage_execs_next_directly_for_signal_handling() -> None:
    dockerfile = _console_dockerfile()

    assert (
        'CMD ["./node_modules/.bin/next", "start", "--hostname", "0.0.0.0", "--port", "3000"]'
        in dockerfile
    )
    assert 'CMD ["npm", "run", "start"' not in dockerfile


@pytest.mark.unit
def test_console_build_stage_accepts_public_poll_interval_arg() -> None:
    dockerfile = _console_dockerfile()

    assert "ARG NEXT_PUBLIC_AWF_CONSOLE_POLL_MS=5000" in dockerfile
    assert "ENV NEXT_PUBLIC_AWF_CONSOLE_POLL_MS=${NEXT_PUBLIC_AWF_CONSOLE_POLL_MS}" in dockerfile
    assert dockerfile.index("ARG NEXT_PUBLIC_AWF_CONSOLE_POLL_MS=5000") < dockerfile.index(
        "RUN npm run build"
    )
