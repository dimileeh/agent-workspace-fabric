# Releasing Agent Workspace Fabric

Use this checklist before tagging an AWF Core alpha release.

## Preconditions

- Work from a clean `development` branch.
- Confirm GitHub CI is green for the commit being tagged.
- Keep the public package name `agent-workspace-fabric` and the import package
  name `awf`.
- Confirm the supported install commands are documented and working:
  `uv tool install agent-workspace-fabric`, `pipx install
  agent-workspace-fabric`, virtualenv-scoped `pip install
  agent-workspace-fabric`, and contributor `uv tool install . --force`.
- Confirm the public curl installer lane is advertised only for releases whose
  `install.sh`, `awf-install-manifest.json`, and checksum-backed distribution
  artifacts have been published and verified from GitHub Release URLs.
- The repository URL still points at
  `https://github.com/dimileeh/aira-agent-workspace-fabric` until the GitHub
  repository is renamed.

## Required Validation

```bash
git status --short --branch
uv run --python 3.12 --extra dev ruff check src/awf tests
uv run --python 3.12 --extra dev mypy src/awf
uv run --python 3.12 --extra dev pytest tests/unit -q
uv run --python 3.12 --extra dev pytest -n 8 --dist=loadscope --timeout=300 \
  --cov=awf --cov-report=term-missing --cov-report=xml --cov-fail-under=99
uv run --python 3.12 --extra dev python scripts/generate_openapi.py --check
npm --prefix apps/console ci
npm --prefix apps/console run lint
npm --prefix apps/console run typecheck
npm --prefix apps/console run build
npm --prefix apps/console run test:browser
mkdir -p artifacts/release
uv run --python 3.12 --with build python -m build
sha256sum dist/* | tee artifacts/release/python-distribution-sha256.txt
uv run --python 3.12 python scripts/generate_install_manifest.py \
  --dist-dir dist \
  --checksums-file artifacts/release/python-distribution-sha256.txt \
  --output artifacts/release/awf-install-manifest.json \
  --version 0.1.0 \
  --tag v0.1.0 \
  --repository-url https://github.com/dimileeh/aira-agent-workspace-fabric \
  --channel auto
docker build -t awf-control-plane:release-check -f docker/control-plane.Dockerfile .
docker build -t awf-agent-runtime:release-check -f docker/agent-runtime.Dockerfile .
```

Install the built wheel from outside the source checkout before tagging:

```bash
uv venv --python 3.12 /tmp/awf-release-install
cd /tmp
uv pip install --python /tmp/awf-release-install/bin/python \
  /path/to/aira-agent-workspace-fabric/dist/*.whl
/tmp/awf-release-install/bin/awf --help
/tmp/awf-release-install/bin/awf init --help
/tmp/awf-release-install/bin/awf service bootstrap --help
/tmp/awf-release-install/bin/python - <<'PY'
from pathlib import Path

from awf.service.bootstrap import get_bootstrap_asset_root

root = get_bootstrap_asset_root()
assert root is not None
for relative in (
    "docker/agent-runtime.Dockerfile",
    "docker/control-plane.Dockerfile",
    "docker/compose/local-service.yml",
    ".env.example",
    "openapi.json",
    "migrations/env.py",
):
    assert (Path(root) / relative).is_file(), relative
PY
```

## Dependency License Audit

Create release-local audit artifacts before tagging:

```bash
mkdir -p artifacts/release
uv run --python 3.12 --extra dev --with pip-licenses pip-licenses \
  --format=json \
  --output-file artifacts/release/python-licenses.json
npx --yes license-checker --production --json --start apps/console \
  > artifacts/release/node-licenses.json
```

Manually review any unknown, unlicensed, GPL, AGPL, LGPL, MPL, CDDL, EPL, or
custom licenses before tagging. Add a root `NOTICE` file only if the audit
finds a concrete attribution notice that must be preserved.

## Local Service Readiness

From a clean checkout with Docker running:

```bash
awf service bootstrap --timeout-seconds 300
awf service readiness --format json
awf service release-readiness --format pretty
```

Release readiness uses the lower-level service bootstrap command because it is
validating local service gates directly. Do not use `awf init` as service setup;
project onboarding is the separate `awf init <path>` flow after the local
service is available.

If `awf service readiness` fails only because historical SLO evidence reflects
known dogfood failures, document the exception in the release notes and rerun
the gate with an explicit allowlist:

```bash
awf service readiness --allow-slo-breach --format json
```

Do not ignore doctor, provider, Docker, database, or cleanup failures.

## Curl Installer Documentation Gate

The public README and Quickstart may present the curl installer lane only after
the release has all of the following:

- published `packaging/install.sh` or the approved hosted redirect for it,
- a published `awf-install-manifest.json`,
- distribution artifacts attached to the GitHub Release,
- checksum metadata for those artifacts, and
- a successful installer smoke proving the manifest-pinned `sha256` is verified
  before install.

If any of those pieces are missing, release notes must direct users to the
`uv tool` / `pipx` lane or a source checkout lane instead of curl.

## PyPI Trusted Publishing

AWF uses PyPI Trusted Publishing through GitHub OIDC; do not create or store a
long-lived PyPI API token for the release workflow. Before the first publish:

1. Create the `agent-workspace-fabric` project on TestPyPI/PyPI.
2. Configure Trusted Publishing for this repository and the `testpypi` and
   `pypi` GitHub environments.
3. Run the `Publish Python Package` workflow manually with
   `publish_target=testpypi`.
4. Install from TestPyPI in a disposable environment and run the local service
   bootstrap smoke above.
5. Only after TestPyPI is clean, run the workflow with `publish_target=pypi`
   for the tag being released.

The workflow also builds distributions on `v*` tags and uploads checksum
artifacts. Publishing remains manual until maintainers explicitly choose the
target environment.

## Install Manifest

GitHub Releases is the canonical artifact source for the v1 installer trust
chain. `aira.pro` may serve or redirect `install.sh`, but v1 installers must
consume `awf-install-manifest.json` and verify a manifest-pinned `sha256`
before installing a wheel.

The manifest is generated from the built `dist/*` files and the existing
`python-distribution-sha256.txt` checksum artifact:

```bash
uv run --python 3.12 python scripts/generate_install_manifest.py \
  --dist-dir dist \
  --checksums-file artifacts/release/python-distribution-sha256.txt \
  --output artifacts/release/awf-install-manifest.json \
  --version 0.1.0 \
  --tag v0.1.0 \
  --repository-url https://github.com/dimileeh/aira-agent-workspace-fabric \
  --channel auto
```

Inspect the manifest before publishing release notes:

```bash
jq . artifacts/release/awf-install-manifest.json
jq -r '.artifacts[] | [.kind, .name, .url, .sha256] | @tsv' \
  artifacts/release/awf-install-manifest.json
```

Every artifact URL must be pinned to a GitHub Release tag, using the shape
`https://github.com/<owner>/<repo>/releases/download/vX.Y.Z/<filename>`.
Do not use mutable latest URLs, branch URLs, raw GitHub URLs, or unpinned
package-index URLs in the manifest. The channel does not change artifact URLs:
version and tag pinning are the trust boundary.

Verify the manifest hashes against the checksum artifact and the local
distribution files:

```bash
sha256sum -c artifacts/release/python-distribution-sha256.txt
python - <<'PY'
import json
from pathlib import Path

manifest = json.loads(Path("artifacts/release/awf-install-manifest.json").read_text())
checksums = {}
for line in Path("artifacts/release/python-distribution-sha256.txt").read_text().splitlines():
    digest, artifact = line.split(maxsplit=1)
    checksums[Path(artifact).name] = digest

for artifact in manifest["artifacts"]:
    assert artifact["sha256"] == checksums[artifact["name"]], artifact["name"]
PY
```

### Verify release artifacts (drift + installer smoke)

The publish workflow runs these checks automatically, but you can reproduce both
locally to confirm the manifest and checksums match the built distributions and
that the installer verifies the artifact **before install**.

Drift gate — fails if the manifest or `python-distribution-sha256.txt` drift
from the built `dist/*` (recorded `sha256`, filenames, channel/version/tag, or
checksum bytes):

```bash
uv run --python 3.12 python scripts/check_release_artifacts.py \
  --dist-dir dist \
  --checksums-file artifacts/release/python-distribution-sha256.txt \
  --manifest artifacts/release/awf-install-manifest.json \
  --version 0.1.0 \
  --tag v0.1.0 \
  --repository-url https://github.com/dimileeh/aira-agent-workspace-fabric
```

Installer smoke — rewrites the manifest's artifact URLs to the local `dist/`
wheel and runs `packaging/install.sh --dry-run`, so the manifest-pinned `sha256`
is verified against the real wheel bytes **before install**; it prints
`Checksum verified` and never installs:

```bash
uv run --no-project --python 3.12 python scripts/release_smoke.py \
  --dist-dir dist \
  --manifest artifacts/release/awf-install-manifest.json \
  --smoke-manifest-out artifacts/release/awf-install-manifest.smoke.json \
  --method uv \
  --run
```

The generator only writes manifest metadata; it does not publish files to
GitHub Releases. After the tag exists and the publish workflow has produced the
release artifacts, upload the exact distributions, checksum file, and manifest
as GitHub Release assets before publishing release notes or pointing installers
at the manifest:

```bash
gh release create v0.1.0 \
  dist/* \
  artifacts/release/python-distribution-sha256.txt \
  artifacts/release/awf-install-manifest.json \
  --title "Agent Workspace Fabric v0.1.0" \
  --notes-file path/to/release-notes.md \
  --verify-tag
```

If the GitHub Release already exists, attach or replace the assets explicitly:

```bash
gh release upload v0.1.0 \
  dist/* \
  artifacts/release/python-distribution-sha256.txt \
  artifacts/release/awf-install-manifest.json \
  --clobber
```

Actions artifacts are workflow audit artifacts only; they are not served from
`/releases/download/`. Do not publish, advertise, or let installers consume
`awf-install-manifest.json` until every manifest artifact URL resolves from the
GitHub Release:

```bash
jq -r '.artifacts[].url' artifacts/release/awf-install-manifest.json |
  while IFS= read -r url; do
    curl --fail --head --location "$url" >/dev/null
  done
```

Channel semantics:

- `stable` is for final package versions such as `0.1.0` and `1.2.3`.
- `prerelease` is for alpha, beta, release-candidate, or dev versions such as
  `0.2.0a1`, `0.2.0b1`, `0.2.0rc1`, and `0.2.0.dev1`.
- `auto` maps final versions to `stable` and prerelease/dev versions to
  `prerelease`.

The v1 manifest includes reserved `signatures` fields. They are intentionally
empty until a later signing slice adds release signing and verification.

## Homebrew Follow-Up

Homebrew is planned after one stable tagged PyPI/GitHub release. Before
advertising a Homebrew install path:

```bash
brew audit --strict --online agent-workspace-fabric
brew audit --new --formula agent-workspace-fabric
brew test agent-workspace-fabric
```

The formula should install from the tagged sdist or GitHub release tarball,
depend on Homebrew Python, and use `awf --help` as its smoke test.

## Tagging

```bash
git tag -a v0.1.0 -m "Agent Workspace Fabric v0.1.0"
git push origin v0.1.0
```
