# Quickstart

Pick one lane and follow only that lane. Each available lane gets AWF
installed, runs the host setup check, starts local Core, initializes a project,
runs mocked smoke, and shows the matching upgrade and uninstall path.

Use the source checkout lanes when you want to inspect AWF before running it.
Use the `uv tool` / `pipx` lane when you want the published package. The hosted curl
installer lane is intentionally omitted until its public installer, manifest,
checksums, and distribution artifacts are published and verified.
All lanes use root `.env` for local runtime values. Existing legacy
`docker/compose/.env` files are migration sources only.

## Prerequisites

- Git.
- Docker Desktop or Docker Engine with the Compose plugin running.
- `uv` for lanes that use `uv`, or `pipx` for the `pipx` lane.
- GitHub CLI `gh` if you want AWF to create or monitor PRs.
- At least one coding-agent credential for real workspace execution.

The mocked smoke command below does not require live GitHub or provider access.
Local first-run service URLs use IPv4 loopback to match the loopback-only
Compose port bindings: use `http://127.0.0.1:8000` for API checks, and
`http://127.0.0.1:3000` when the console is running.

For source checkouts or raw Docker installs, root Compose can bring up the full
local stack with safe loopback-only defaults:

```bash
docker compose up --build
```

Open <http://127.0.0.1:3000> for the console, or call the API at
<http://127.0.0.1:8000>. Protected local API calls use
`Authorization: Bearer local-dev-token` unless you set `AWF_API_TOKEN`.

If you set or refresh the GitHub token after starting Core, rerun the start
command for the lane you used so Compose recreates the service containers with
the updated environment.

For Lane 1 (`uv tool` or `pipx`):

```bash
awf start
```

For Lane 2 (source checkout with global tool install), run from the checkout:

```bash
awf start --source-checkout "$PWD"
```

For Lane 3 (source checkout with no global install), run from the checkout:

```bash
uv run --python 3.12 --extra dev awf start --source-checkout "$PWD"
```

## Lane 1: uv tool or pipx

This lane is release-installed and package-manager mediated. `uv tool` and
`pipx` install the published `agent-workspace-fabric` package into an isolated
tool environment.

Install AWF with one package manager.

`uv tool`:

```bash
uv tool install agent-workspace-fabric
```

`pipx`:

```bash
pipx install agent-workspace-fabric
```

Then run the shared first-run commands from the directory where AWF should keep
the package-lane `.env`. Persist the generated local service values before
setup/start so a later upgrade can restore the same running Core token and
password, so host-side database checks use a URL-encoded copy of that same
password, and so the persisted password stays literal under Compose dotenv
parsing:

```bash
export AWF_API_TOKEN="$(openssl rand -hex 32)"
export AWF_POSTGRES_PASSWORD="${AWF_POSTGRES_PASSWORD:-awf_dev}"
export AWF_POSTGRES_HOST_PORT="${AWF_POSTGRES_HOST_PORT:-5433}"
awf_postgres_password_urlencoded="$(python3 -c 'from os import environ; from urllib.parse import quote; print(quote(environ["AWF_POSTGRES_PASSWORD"], safe=""))')"
awf_postgres_password_dotenv="$(
  python3 - <<'PY'
from os import environ

value = environ["AWF_POSTGRES_PASSWORD"]
if "\n" in value or "\r" in value:
    raise SystemExit("AWF_POSTGRES_PASSWORD cannot contain newlines")
print('"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$") + '"')
PY
)"
export AWF_DATABASE_URL="postgresql+asyncpg://awf:${awf_postgres_password_urlencoded}@localhost:${AWF_POSTGRES_HOST_PORT}/awf"
awf_env_tmp="$(mktemp)"
{
  printf 'AWF_API_TOKEN=%s\n' "$AWF_API_TOKEN"
  printf 'AWF_POSTGRES_PASSWORD=%s\n' "$awf_postgres_password_dotenv"
  printf 'AWF_POSTGRES_HOST_PORT=%s\n' "$AWF_POSTGRES_HOST_PORT"
  printf 'AWF_DATABASE_URL=%s\n' "$AWF_DATABASE_URL"
  if [ -f .env ]; then
    sed \
      -e '/^[[:space:]]*\(export[[:space:]][[:space:]]*\)\{0,1\}AWF_API_TOKEN[[:space:]]*=/d' \
      -e '/^[[:space:]]*\(export[[:space:]][[:space:]]*\)\{0,1\}AWF_POSTGRES_PASSWORD[[:space:]]*=/d' \
      -e '/^[[:space:]]*\(export[[:space:]][[:space:]]*\)\{0,1\}AWF_POSTGRES_HOST_PORT[[:space:]]*=/d' \
      -e '/^[[:space:]]*\(export[[:space:]][[:space:]]*\)\{0,1\}AWF_DATABASE_URL[[:space:]]*=/d' \
      .env
  fi
} > "$awf_env_tmp"
mv "$awf_env_tmp" .env
# [optional] Only needed for PR creation/monitoring; skip for mocked smoke.
# Provide AWF_GITHUB_TOKEN, GH_TOKEN, or GITHUB_TOKEN manually if needed.
awf setup
awf start
awf service status --format pretty

mkdir -p "$HOME/awf-eval-project"
awf init "$HOME/awf-eval-project"
awf smoke run --project "$HOME/awf-eval-project" --mocked-local --format pretty
```

This is the `awf init <path>` step. Supply any path, such as an empty eval
directory or a checked-out project.

Upgrade:

Run the upgrade command for the package manager you used to install AWF.

`uv tool`:

```bash
uv tool upgrade agent-workspace-fabric
```

`pipx`:

```bash
pipx upgrade agent-workspace-fabric
```

Then restart AWF and rerun smoke. If `AWF_API_TOKEN`,
`AWF_POSTGRES_PASSWORD`, and `AWF_DATABASE_URL` are not already persisted in
`.env`, restore them in this shell before restarting. Restore the same
`AWF_API_TOKEN`, `AWF_POSTGRES_PASSWORD`, and `AWF_DATABASE_URL` used by the
running local Core; do not generate replacement service secrets during upgrade:

```bash
awf_decode_double_quoted_dotenv() {
  python3 -c 'import re, sys
replacements = {"n": "\n", "r": "\r", "t": "\t", "\\": "\\", chr(34): chr(34), "$": "$"}
print(re.sub(r"\\(.)", lambda match: replacements.get(match[1], match[1]), sys.argv[1]), end="")' "$1"
}
awf_strip_unquoted_dotenv_inline_comment() {
  case "$1" in
    \"*\"[[:space:]]*\#*)
      python3 -c 'import sys
value = sys.argv[1]
escaped = False
for index in range(1, len(value)):
    char = value[index]
    if escaped:
        escaped = False
    elif char == "\\":
        escaped = True
    elif char == chr(34):
        rest = value[index + 1:]
        if rest == "" or rest.lstrip(" \t").startswith("#"):
            print(value[: index + 1], end="")
            raise SystemExit
        break
print(value, end="")' "$1"
      ;;
    \'*\'[[:space:]]*\#*)
      python3 -c 'import sys
value = sys.argv[1]
index = value.find(chr(39), 1)
if index != -1:
    rest = value[index + 1:]
    if rest == "" or rest.lstrip(" \t").startswith("#"):
        print(value[: index + 1], end="")
        raise SystemExit
print(value, end="")' "$1"
      ;;
    \"*|\'*) printf "%s" "$1" ;;
    \#*) printf "%s" "" ;;
    *) printf "%s" "$1" | sed 's/[[:space:]]#.*$//; s/[[:space:]]*$//' ;;
  esac
}
AWF_PERSISTED_API_TOKEN="$(sed -n 's/^[[:space:]]*\(export[[:space:]][[:space:]]*\)\{0,1\}AWF_API_TOKEN[[:space:]]*=[[:space:]]*//p' .env 2>/dev/null | tail -n 1)"
AWF_PERSISTED_API_TOKEN="$(awf_strip_unquoted_dotenv_inline_comment "$AWF_PERSISTED_API_TOKEN")"
case "$AWF_PERSISTED_API_TOKEN" in
  \"*\")
    AWF_PERSISTED_API_TOKEN="${AWF_PERSISTED_API_TOKEN#\"}"
    AWF_PERSISTED_API_TOKEN="${AWF_PERSISTED_API_TOKEN%\"}"
    AWF_PERSISTED_API_TOKEN="$(awf_decode_double_quoted_dotenv "$AWF_PERSISTED_API_TOKEN")"
    ;;
  \'*\')
    AWF_PERSISTED_API_TOKEN="${AWF_PERSISTED_API_TOKEN#\'}"
    AWF_PERSISTED_API_TOKEN="${AWF_PERSISTED_API_TOKEN%\'}"
    ;;
esac
if [ -n "$AWF_PERSISTED_API_TOKEN" ]; then
  export AWF_API_TOKEN="$AWF_PERSISTED_API_TOKEN"
else
  : "${AWF_API_TOKEN:?restore the AWF_API_TOKEN used for the running local Core or persist it in .env before upgrading}"
  export AWF_API_TOKEN
fi
AWF_PERSISTED_POSTGRES_PASSWORD="$(sed -n 's/^[[:space:]]*\(export[[:space:]][[:space:]]*\)\{0,1\}AWF_POSTGRES_PASSWORD[[:space:]]*=[[:space:]]*//p' .env 2>/dev/null | tail -n 1)"
AWF_PERSISTED_POSTGRES_PASSWORD="$(awf_strip_unquoted_dotenv_inline_comment "$AWF_PERSISTED_POSTGRES_PASSWORD")"
case "$AWF_PERSISTED_POSTGRES_PASSWORD" in
  \"*\")
    AWF_PERSISTED_POSTGRES_PASSWORD="${AWF_PERSISTED_POSTGRES_PASSWORD#\"}"
    AWF_PERSISTED_POSTGRES_PASSWORD="${AWF_PERSISTED_POSTGRES_PASSWORD%\"}"
    AWF_PERSISTED_POSTGRES_PASSWORD="$(awf_decode_double_quoted_dotenv "$AWF_PERSISTED_POSTGRES_PASSWORD")"
    ;;
  \'*\')
    AWF_PERSISTED_POSTGRES_PASSWORD="${AWF_PERSISTED_POSTGRES_PASSWORD#\'}"
    AWF_PERSISTED_POSTGRES_PASSWORD="${AWF_PERSISTED_POSTGRES_PASSWORD%\'}"
    ;;
esac
if [ -n "$AWF_PERSISTED_POSTGRES_PASSWORD" ]; then
  export AWF_POSTGRES_PASSWORD="$AWF_PERSISTED_POSTGRES_PASSWORD"
else
  : "${AWF_POSTGRES_PASSWORD:?restore the AWF_POSTGRES_PASSWORD used for the running local Core or persist it in .env before upgrading}"
  export AWF_POSTGRES_PASSWORD
fi
AWF_PERSISTED_DATABASE_URL="$(sed -n 's/^[[:space:]]*\(export[[:space:]][[:space:]]*\)\{0,1\}AWF_DATABASE_URL[[:space:]]*=[[:space:]]*//p' .env 2>/dev/null | tail -n 1)"
AWF_PERSISTED_DATABASE_URL="$(awf_strip_unquoted_dotenv_inline_comment "$AWF_PERSISTED_DATABASE_URL")"
case "$AWF_PERSISTED_DATABASE_URL" in
  \"*\")
    AWF_PERSISTED_DATABASE_URL="${AWF_PERSISTED_DATABASE_URL#\"}"
    AWF_PERSISTED_DATABASE_URL="${AWF_PERSISTED_DATABASE_URL%\"}"
    AWF_PERSISTED_DATABASE_URL="$(awf_decode_double_quoted_dotenv "$AWF_PERSISTED_DATABASE_URL")"
    ;;
  \'*\')
    AWF_PERSISTED_DATABASE_URL="${AWF_PERSISTED_DATABASE_URL#\'}"
    AWF_PERSISTED_DATABASE_URL="${AWF_PERSISTED_DATABASE_URL%\'}"
    ;;
esac
if [ -n "$AWF_PERSISTED_DATABASE_URL" ]; then
  export AWF_DATABASE_URL="$AWF_PERSISTED_DATABASE_URL"
else
  : "${AWF_DATABASE_URL:?restore the AWF_DATABASE_URL used for the running local Core or persist it in .env before upgrading}"
  export AWF_DATABASE_URL
fi
awf start
awf smoke run --project "$HOME/awf-eval-project" --mocked-local --format pretty
```

Uninstall:

Run the uninstall command for the package manager you used to install AWF.

`uv tool`:

```bash
uv tool uninstall agent-workspace-fabric
```

`pipx`:

```bash
pipx uninstall agent-workspace-fabric
```

## Lane 2: Source Checkout With Global Tool Install

This lane uses inspectable source and then installs `awf` as a global tool from
that checkout. It is useful when you want to inspect or patch AWF but still want
the normal `awf` executable on `PATH`.

Keep local runtime values in the checkout-root `.env` so a later upgrade can
restore the same running Core token and password, and so host-side database
checks use that same password.

```bash
git clone https://github.com/dimileeh/aira-agent-workspace-fabric.git
cd aira-agent-workspace-fabric
uv tool install . --force
cp .env.example .env
# [optional] Only needed for PR creation/monitoring; skip for mocked smoke.
# Provide AWF_GITHUB_TOKEN, GH_TOKEN, or GITHUB_TOKEN manually if needed.
awf setup --source-checkout "$PWD"
awf start --source-checkout "$PWD"
awf service status --format pretty

mkdir -p ../awf-eval-project
awf init ../awf-eval-project
awf smoke run --project ../awf-eval-project --mocked-local --format pretty
```

This is the `awf init <path>` step for a checked-out project repository.

Upgrade:

Run this from the existing `aira-agent-workspace-fabric` checkout. If your shell
is elsewhere, first `cd /path/to/aira-agent-workspace-fabric`. Stop local Core
before pulling new source files or refreshing source-checkout metadata; setup
checks the API and Postgres host ports and blocks while the previous Core stack
still holds them.

```bash
awf_decode_double_quoted_dotenv() {
  python3 -c 'import re, sys
replacements = {"n": "\n", "r": "\r", "t": "\t", "\\": "\\", chr(34): chr(34), "$": "$"}
print(re.sub(r"\\(.)", lambda match: replacements.get(match[1], match[1]), sys.argv[1]), end="")' "$1"
}
awf_strip_unquoted_dotenv_inline_comment() {
  case "$1" in
    \"*\"[[:space:]]*\#*)
      python3 -c 'import sys
value = sys.argv[1]
escaped = False
for index in range(1, len(value)):
    char = value[index]
    if escaped:
        escaped = False
    elif char == "\\":
        escaped = True
    elif char == chr(34):
        rest = value[index + 1:]
        if rest == "" or rest.lstrip(" \t").startswith("#"):
            print(value[: index + 1], end="")
            raise SystemExit
        break
print(value, end="")' "$1"
      ;;
    \'*\'[[:space:]]*\#*)
      python3 -c 'import sys
value = sys.argv[1]
index = value.find(chr(39), 1)
if index != -1:
    rest = value[index + 1:]
    if rest == "" or rest.lstrip(" \t").startswith("#"):
        print(value[: index + 1], end="")
        raise SystemExit
print(value, end="")' "$1"
      ;;
    \"*|\'*) printf "%s" "$1" ;;
    \#*) printf "%s" "" ;;
    *) printf "%s" "$1" | sed 's/[[:space:]]#.*$//; s/[[:space:]]*$//' ;;
  esac
}
AWF_PERSISTED_API_TOKEN=""
for env_file in .env docker/compose/.env; do
  [ -f "$env_file" ] || continue
  AWF_PERSISTED_API_TOKEN="$(sed -n 's/^[[:space:]]*\(export[[:space:]][[:space:]]*\)\{0,1\}AWF_API_TOKEN[[:space:]]*=[[:space:]]*//p' "$env_file" | tail -n 1)"
  AWF_PERSISTED_API_TOKEN="$(awf_strip_unquoted_dotenv_inline_comment "$AWF_PERSISTED_API_TOKEN")"
  case "$AWF_PERSISTED_API_TOKEN" in
    \"*\")
      AWF_PERSISTED_API_TOKEN="${AWF_PERSISTED_API_TOKEN#\"}"
      AWF_PERSISTED_API_TOKEN="${AWF_PERSISTED_API_TOKEN%\"}"
      AWF_PERSISTED_API_TOKEN="$(awf_decode_double_quoted_dotenv "$AWF_PERSISTED_API_TOKEN")"
      ;;
    \'*\')
      AWF_PERSISTED_API_TOKEN="${AWF_PERSISTED_API_TOKEN#\'}"
      AWF_PERSISTED_API_TOKEN="${AWF_PERSISTED_API_TOKEN%\'}"
      ;;
  esac
  [ -n "$AWF_PERSISTED_API_TOKEN" ] && break
done
if [ -n "$AWF_PERSISTED_API_TOKEN" ]; then
  export AWF_API_TOKEN="$AWF_PERSISTED_API_TOKEN"
elif grep -q '^[[:space:]]*\(export[[:space:]][[:space:]]*\)\{0,1\}AWF_API_TOKEN[[:space:]]*=' .env docker/compose/.env 2>/dev/null; then
  export AWF_API_TOKEN="${AWF_API_TOKEN:-local-dev-token}"
else
  : "${AWF_API_TOKEN:?restore the AWF_API_TOKEN used for the running local Core or persist it in .env or docker/compose/.env before upgrading}"
  export AWF_API_TOKEN
fi
AWF_PERSISTED_POSTGRES_PASSWORD=""
for env_file in .env docker/compose/.env; do
  [ -f "$env_file" ] || continue
  AWF_PERSISTED_POSTGRES_PASSWORD="$(sed -n 's/^[[:space:]]*\(export[[:space:]][[:space:]]*\)\{0,1\}AWF_POSTGRES_PASSWORD[[:space:]]*=[[:space:]]*//p' "$env_file" | tail -n 1)"
  AWF_PERSISTED_POSTGRES_PASSWORD="$(awf_strip_unquoted_dotenv_inline_comment "$AWF_PERSISTED_POSTGRES_PASSWORD")"
  case "$AWF_PERSISTED_POSTGRES_PASSWORD" in
    \"*\")
      AWF_PERSISTED_POSTGRES_PASSWORD="${AWF_PERSISTED_POSTGRES_PASSWORD#\"}"
      AWF_PERSISTED_POSTGRES_PASSWORD="${AWF_PERSISTED_POSTGRES_PASSWORD%\"}"
      AWF_PERSISTED_POSTGRES_PASSWORD="$(awf_decode_double_quoted_dotenv "$AWF_PERSISTED_POSTGRES_PASSWORD")"
      ;;
    \'*\')
      AWF_PERSISTED_POSTGRES_PASSWORD="${AWF_PERSISTED_POSTGRES_PASSWORD#\'}"
      AWF_PERSISTED_POSTGRES_PASSWORD="${AWF_PERSISTED_POSTGRES_PASSWORD%\'}"
      ;;
  esac
  [ -n "$AWF_PERSISTED_POSTGRES_PASSWORD" ] && break
done
if [ -n "$AWF_PERSISTED_POSTGRES_PASSWORD" ]; then
  export AWF_POSTGRES_PASSWORD="$AWF_PERSISTED_POSTGRES_PASSWORD"
else
  : "${AWF_POSTGRES_PASSWORD:?restore the AWF_POSTGRES_PASSWORD used for the running local Core or persist it in .env or docker/compose/.env before upgrading}"
  export AWF_POSTGRES_PASSWORD
fi
AWF_PERSISTED_POSTGRES_HOST_PORT=""
for env_file in .env docker/compose/.env; do
  [ -f "$env_file" ] || continue
  AWF_PERSISTED_POSTGRES_HOST_PORT="$(sed -n 's/^[[:space:]]*\(export[[:space:]][[:space:]]*\)\{0,1\}AWF_POSTGRES_HOST_PORT[[:space:]]*=[[:space:]]*//p' "$env_file" | tail -n 1)"
  AWF_PERSISTED_POSTGRES_HOST_PORT="$(awf_strip_unquoted_dotenv_inline_comment "$AWF_PERSISTED_POSTGRES_HOST_PORT")"
  case "$AWF_PERSISTED_POSTGRES_HOST_PORT" in
    \"*\")
      AWF_PERSISTED_POSTGRES_HOST_PORT="${AWF_PERSISTED_POSTGRES_HOST_PORT#\"}"
      AWF_PERSISTED_POSTGRES_HOST_PORT="${AWF_PERSISTED_POSTGRES_HOST_PORT%\"}"
      AWF_PERSISTED_POSTGRES_HOST_PORT="$(awf_decode_double_quoted_dotenv "$AWF_PERSISTED_POSTGRES_HOST_PORT")"
      ;;
    \'*\')
      AWF_PERSISTED_POSTGRES_HOST_PORT="${AWF_PERSISTED_POSTGRES_HOST_PORT#\'}"
      AWF_PERSISTED_POSTGRES_HOST_PORT="${AWF_PERSISTED_POSTGRES_HOST_PORT%\'}"
      ;;
  esac
  [ -n "$AWF_PERSISTED_POSTGRES_HOST_PORT" ] && break
done
if [ -n "$AWF_PERSISTED_POSTGRES_HOST_PORT" ]; then
  export AWF_POSTGRES_HOST_PORT="$AWF_PERSISTED_POSTGRES_HOST_PORT"
fi
AWF_PERSISTED_DATABASE_URL=""
for env_file in .env docker/compose/.env; do
  [ -f "$env_file" ] || continue
  AWF_PERSISTED_DATABASE_URL="$(sed -n 's/^[[:space:]]*\(export[[:space:]][[:space:]]*\)\{0,1\}AWF_DATABASE_URL[[:space:]]*=[[:space:]]*//p' "$env_file" | tail -n 1)"
  AWF_PERSISTED_DATABASE_URL="$(awf_strip_unquoted_dotenv_inline_comment "$AWF_PERSISTED_DATABASE_URL")"
  case "$AWF_PERSISTED_DATABASE_URL" in
    \"*\")
      AWF_PERSISTED_DATABASE_URL="${AWF_PERSISTED_DATABASE_URL#\"}"
      AWF_PERSISTED_DATABASE_URL="${AWF_PERSISTED_DATABASE_URL%\"}"
      AWF_PERSISTED_DATABASE_URL="$(awf_decode_double_quoted_dotenv "$AWF_PERSISTED_DATABASE_URL")"
      ;;
    \'*\')
      AWF_PERSISTED_DATABASE_URL="${AWF_PERSISTED_DATABASE_URL#\'}"
      AWF_PERSISTED_DATABASE_URL="${AWF_PERSISTED_DATABASE_URL%\'}"
      ;;
  esac
  [ -n "$AWF_PERSISTED_DATABASE_URL" ] && break
done
if [ -n "$AWF_PERSISTED_DATABASE_URL" ]; then
  export AWF_DATABASE_URL="$AWF_PERSISTED_DATABASE_URL"
else
  unset AWF_DATABASE_URL
fi
if [ -f .env ]; then
  docker compose --env-file .env -f docker/compose/local-service.yml stop
elif [ -f docker/compose/.env ]; then
  docker compose --env-file docker/compose/.env -f docker/compose/local-service.yml stop
else
  docker compose -f docker/compose/local-service.yml stop
fi
git pull
uv tool install . --force
awf setup --source-checkout "$PWD"
awf start --source-checkout "$PWD"
awf smoke run --project ../awf-eval-project --mocked-local --format pretty
```

Uninstall:

Before uninstalling the global tool or deleting the checkout, make sure
`~/.awf/config.yml` no longer records it under `source_checkout`. Refreshing
through `awf setup --source-checkout ...` is not metadata-only. Stop local Core
before refreshing source-checkout metadata; `awf setup` checks the API and
Postgres host ports and blocks while the previous Core stack still holds them.
Editing `~/.awf/config.yml` remains the no-stop option. To refresh the persisted
path:

```bash
awf_decode_double_quoted_dotenv() {
  python3 -c 'import re, sys
replacements = {"n": "\n", "r": "\r", "t": "\t", "\\": "\\", chr(34): chr(34), "$": "$"}
print(re.sub(r"\\(.)", lambda match: replacements.get(match[1], match[1]), sys.argv[1]), end="")' "$1"
}
awf_strip_unquoted_dotenv_inline_comment() {
  case "$1" in
    \"*\"[[:space:]]*\#*)
      python3 -c 'import sys
value = sys.argv[1]
escaped = False
for index in range(1, len(value)):
    char = value[index]
    if escaped:
        escaped = False
    elif char == "\\":
        escaped = True
    elif char == chr(34):
        rest = value[index + 1:]
        if rest == "" or rest.lstrip(" \t").startswith("#"):
            print(value[: index + 1], end="")
            raise SystemExit
        break
print(value, end="")' "$1"
      ;;
    \'*\'[[:space:]]*\#*)
      python3 -c 'import sys
value = sys.argv[1]
index = value.find(chr(39), 1)
if index != -1:
    rest = value[index + 1:]
    if rest == "" or rest.lstrip(" \t").startswith("#"):
        print(value[: index + 1], end="")
        raise SystemExit
print(value, end="")' "$1"
      ;;
    \"*|\'*) printf "%s" "$1" ;;
    \#*) printf "%s" "" ;;
    *) printf "%s" "$1" | sed 's/[[:space:]]#.*$//; s/[[:space:]]*$//' ;;
  esac
}
AWF_PERSISTED_API_TOKEN=""
for env_file in .env docker/compose/.env; do
  [ -f "$env_file" ] || continue
  AWF_PERSISTED_API_TOKEN="$(sed -n 's/^[[:space:]]*\(export[[:space:]][[:space:]]*\)\{0,1\}AWF_API_TOKEN[[:space:]]*=[[:space:]]*//p' "$env_file" | tail -n 1)"
  AWF_PERSISTED_API_TOKEN="$(awf_strip_unquoted_dotenv_inline_comment "$AWF_PERSISTED_API_TOKEN")"
  case "$AWF_PERSISTED_API_TOKEN" in
    \"*\")
      AWF_PERSISTED_API_TOKEN="${AWF_PERSISTED_API_TOKEN#\"}"
      AWF_PERSISTED_API_TOKEN="${AWF_PERSISTED_API_TOKEN%\"}"
      AWF_PERSISTED_API_TOKEN="$(awf_decode_double_quoted_dotenv "$AWF_PERSISTED_API_TOKEN")"
      ;;
    \'*\')
      AWF_PERSISTED_API_TOKEN="${AWF_PERSISTED_API_TOKEN#\'}"
      AWF_PERSISTED_API_TOKEN="${AWF_PERSISTED_API_TOKEN%\'}"
      ;;
  esac
  [ -n "$AWF_PERSISTED_API_TOKEN" ] && break
done
if [ -n "$AWF_PERSISTED_API_TOKEN" ]; then
  export AWF_API_TOKEN="$AWF_PERSISTED_API_TOKEN"
elif grep -q '^[[:space:]]*\(export[[:space:]][[:space:]]*\)\{0,1\}AWF_API_TOKEN[[:space:]]*=' .env docker/compose/.env 2>/dev/null; then
  export AWF_API_TOKEN="${AWF_API_TOKEN:-local-dev-token}"
else
  : "${AWF_API_TOKEN:?restore the AWF_API_TOKEN used for the running local Core or persist it in .env or docker/compose/.env before refreshing source-checkout metadata}"
  export AWF_API_TOKEN
fi
AWF_PERSISTED_POSTGRES_PASSWORD=""
for env_file in .env docker/compose/.env; do
  [ -f "$env_file" ] || continue
  AWF_PERSISTED_POSTGRES_PASSWORD="$(sed -n 's/^[[:space:]]*\(export[[:space:]][[:space:]]*\)\{0,1\}AWF_POSTGRES_PASSWORD[[:space:]]*=[[:space:]]*//p' "$env_file" | tail -n 1)"
  AWF_PERSISTED_POSTGRES_PASSWORD="$(awf_strip_unquoted_dotenv_inline_comment "$AWF_PERSISTED_POSTGRES_PASSWORD")"
  case "$AWF_PERSISTED_POSTGRES_PASSWORD" in
    \"*\")
      AWF_PERSISTED_POSTGRES_PASSWORD="${AWF_PERSISTED_POSTGRES_PASSWORD#\"}"
      AWF_PERSISTED_POSTGRES_PASSWORD="${AWF_PERSISTED_POSTGRES_PASSWORD%\"}"
      AWF_PERSISTED_POSTGRES_PASSWORD="$(awf_decode_double_quoted_dotenv "$AWF_PERSISTED_POSTGRES_PASSWORD")"
      ;;
    \'*\')
      AWF_PERSISTED_POSTGRES_PASSWORD="${AWF_PERSISTED_POSTGRES_PASSWORD#\'}"
      AWF_PERSISTED_POSTGRES_PASSWORD="${AWF_PERSISTED_POSTGRES_PASSWORD%\'}"
      ;;
  esac
  [ -n "$AWF_PERSISTED_POSTGRES_PASSWORD" ] && break
done
if [ -n "$AWF_PERSISTED_POSTGRES_PASSWORD" ]; then
  export AWF_POSTGRES_PASSWORD="$AWF_PERSISTED_POSTGRES_PASSWORD"
else
  : "${AWF_POSTGRES_PASSWORD:?restore the AWF_POSTGRES_PASSWORD used for the running local Core or persist it in .env or docker/compose/.env before refreshing source-checkout metadata}"
  export AWF_POSTGRES_PASSWORD
fi
if [ -f .env ]; then
  docker compose --env-file .env -f docker/compose/local-service.yml stop
elif [ -f docker/compose/.env ]; then
  docker compose --env-file docker/compose/.env -f docker/compose/local-service.yml stop
else
  docker compose -f docker/compose/local-service.yml stop
fi
awf setup --source-checkout /path/to/replacement/aira-agent-workspace-fabric
```

or edit `~/.awf/config.yml` and remove only the top-level `source_checkout:` block.
Keep provider, client, and consent entries unless you intentionally want to reset
host setup state.

```bash
uv tool uninstall agent-workspace-fabric
```

```bash
cd ..
rm -rf aira-agent-workspace-fabric
```

Only delete the AWF checkout if it was created just for evaluation and no
persisted `source_checkout` metadata points at it.

## Lane 3: Source Checkout With No Global Install

This lane uses inspectable source and no global install. It does not place an
`awf` executable on the global `PATH`; every AWF command runs through `uv run`
from the checkout.

Keep local runtime values in the checkout-root `.env` so a later upgrade can
restore the same running Core token and password, and so host-side database
checks use that same password.

```bash
git clone https://github.com/dimileeh/aira-agent-workspace-fabric.git
cd aira-agent-workspace-fabric
uv sync --extra dev

cp .env.example .env
# [optional] Only needed for PR creation/monitoring; skip for mocked smoke.
# Provide AWF_GITHUB_TOKEN, GH_TOKEN, or GITHUB_TOKEN manually if needed.
uv run --python 3.12 --extra dev awf setup --source-checkout "$PWD"
uv run --python 3.12 --extra dev awf start --source-checkout "$PWD"
uv run --python 3.12 --extra dev awf service status --format pretty

mkdir -p ../awf-eval-project
uv run --python 3.12 --extra dev awf init ../awf-eval-project
uv run --python 3.12 --extra dev awf smoke run --project ../awf-eval-project --mocked-local --format pretty
```

This is the `awf init <path>` step for a checked-out project repository.

Upgrade:

Run this from the existing `aira-agent-workspace-fabric` checkout. If your shell
is elsewhere, first `cd /path/to/aira-agent-workspace-fabric`. Stop local Core
before pulling new source files or refreshing source-checkout metadata; setup
checks the API and Postgres host ports and blocks while the previous Core stack
still holds them.

```bash
awf_decode_double_quoted_dotenv() {
  python3 -c 'import re, sys
replacements = {"n": "\n", "r": "\r", "t": "\t", "\\": "\\", chr(34): chr(34), "$": "$"}
print(re.sub(r"\\(.)", lambda match: replacements.get(match[1], match[1]), sys.argv[1]), end="")' "$1"
}
awf_strip_unquoted_dotenv_inline_comment() {
  case "$1" in
    \"*\"[[:space:]]*\#*)
      python3 -c 'import sys
value = sys.argv[1]
escaped = False
for index in range(1, len(value)):
    char = value[index]
    if escaped:
        escaped = False
    elif char == "\\":
        escaped = True
    elif char == chr(34):
        rest = value[index + 1:]
        if rest == "" or rest.lstrip(" \t").startswith("#"):
            print(value[: index + 1], end="")
            raise SystemExit
        break
print(value, end="")' "$1"
      ;;
    \'*\'[[:space:]]*\#*)
      python3 -c 'import sys
value = sys.argv[1]
index = value.find(chr(39), 1)
if index != -1:
    rest = value[index + 1:]
    if rest == "" or rest.lstrip(" \t").startswith("#"):
        print(value[: index + 1], end="")
        raise SystemExit
print(value, end="")' "$1"
      ;;
    \"*|\'*) printf "%s" "$1" ;;
    \#*) printf "%s" "" ;;
    *) printf "%s" "$1" | sed 's/[[:space:]]#.*$//; s/[[:space:]]*$//' ;;
  esac
}
AWF_PERSISTED_API_TOKEN=""
for env_file in .env docker/compose/.env; do
  [ -f "$env_file" ] || continue
  AWF_PERSISTED_API_TOKEN="$(sed -n 's/^[[:space:]]*\(export[[:space:]][[:space:]]*\)\{0,1\}AWF_API_TOKEN[[:space:]]*=[[:space:]]*//p' "$env_file" | tail -n 1)"
  AWF_PERSISTED_API_TOKEN="$(awf_strip_unquoted_dotenv_inline_comment "$AWF_PERSISTED_API_TOKEN")"
  case "$AWF_PERSISTED_API_TOKEN" in
    \"*\")
      AWF_PERSISTED_API_TOKEN="${AWF_PERSISTED_API_TOKEN#\"}"
      AWF_PERSISTED_API_TOKEN="${AWF_PERSISTED_API_TOKEN%\"}"
      AWF_PERSISTED_API_TOKEN="$(awf_decode_double_quoted_dotenv "$AWF_PERSISTED_API_TOKEN")"
      ;;
    \'*\')
      AWF_PERSISTED_API_TOKEN="${AWF_PERSISTED_API_TOKEN#\'}"
      AWF_PERSISTED_API_TOKEN="${AWF_PERSISTED_API_TOKEN%\'}"
      ;;
  esac
  [ -n "$AWF_PERSISTED_API_TOKEN" ] && break
done
if [ -n "$AWF_PERSISTED_API_TOKEN" ]; then
  export AWF_API_TOKEN="$AWF_PERSISTED_API_TOKEN"
elif grep -q '^[[:space:]]*\(export[[:space:]][[:space:]]*\)\{0,1\}AWF_API_TOKEN[[:space:]]*=' .env docker/compose/.env 2>/dev/null; then
  export AWF_API_TOKEN="${AWF_API_TOKEN:-local-dev-token}"
else
  : "${AWF_API_TOKEN:?restore the AWF_API_TOKEN used for the running local Core or persist it in .env or docker/compose/.env before upgrading}"
  export AWF_API_TOKEN
fi
AWF_PERSISTED_POSTGRES_PASSWORD=""
for env_file in .env docker/compose/.env; do
  [ -f "$env_file" ] || continue
  AWF_PERSISTED_POSTGRES_PASSWORD="$(sed -n 's/^[[:space:]]*\(export[[:space:]][[:space:]]*\)\{0,1\}AWF_POSTGRES_PASSWORD[[:space:]]*=[[:space:]]*//p' "$env_file" | tail -n 1)"
  AWF_PERSISTED_POSTGRES_PASSWORD="$(awf_strip_unquoted_dotenv_inline_comment "$AWF_PERSISTED_POSTGRES_PASSWORD")"
  case "$AWF_PERSISTED_POSTGRES_PASSWORD" in
    \"*\")
      AWF_PERSISTED_POSTGRES_PASSWORD="${AWF_PERSISTED_POSTGRES_PASSWORD#\"}"
      AWF_PERSISTED_POSTGRES_PASSWORD="${AWF_PERSISTED_POSTGRES_PASSWORD%\"}"
      AWF_PERSISTED_POSTGRES_PASSWORD="$(awf_decode_double_quoted_dotenv "$AWF_PERSISTED_POSTGRES_PASSWORD")"
      ;;
    \'*\')
      AWF_PERSISTED_POSTGRES_PASSWORD="${AWF_PERSISTED_POSTGRES_PASSWORD#\'}"
      AWF_PERSISTED_POSTGRES_PASSWORD="${AWF_PERSISTED_POSTGRES_PASSWORD%\'}"
      ;;
  esac
  [ -n "$AWF_PERSISTED_POSTGRES_PASSWORD" ] && break
done
if [ -n "$AWF_PERSISTED_POSTGRES_PASSWORD" ]; then
  export AWF_POSTGRES_PASSWORD="$AWF_PERSISTED_POSTGRES_PASSWORD"
else
  : "${AWF_POSTGRES_PASSWORD:?restore the AWF_POSTGRES_PASSWORD used for the running local Core or persist it in .env or docker/compose/.env before upgrading}"
  export AWF_POSTGRES_PASSWORD
fi
AWF_PERSISTED_POSTGRES_HOST_PORT=""
for env_file in .env docker/compose/.env; do
  [ -f "$env_file" ] || continue
  AWF_PERSISTED_POSTGRES_HOST_PORT="$(sed -n 's/^[[:space:]]*\(export[[:space:]][[:space:]]*\)\{0,1\}AWF_POSTGRES_HOST_PORT[[:space:]]*=[[:space:]]*//p' "$env_file" | tail -n 1)"
  AWF_PERSISTED_POSTGRES_HOST_PORT="$(awf_strip_unquoted_dotenv_inline_comment "$AWF_PERSISTED_POSTGRES_HOST_PORT")"
  case "$AWF_PERSISTED_POSTGRES_HOST_PORT" in
    \"*\")
      AWF_PERSISTED_POSTGRES_HOST_PORT="${AWF_PERSISTED_POSTGRES_HOST_PORT#\"}"
      AWF_PERSISTED_POSTGRES_HOST_PORT="${AWF_PERSISTED_POSTGRES_HOST_PORT%\"}"
      AWF_PERSISTED_POSTGRES_HOST_PORT="$(awf_decode_double_quoted_dotenv "$AWF_PERSISTED_POSTGRES_HOST_PORT")"
      ;;
    \'*\')
      AWF_PERSISTED_POSTGRES_HOST_PORT="${AWF_PERSISTED_POSTGRES_HOST_PORT#\'}"
      AWF_PERSISTED_POSTGRES_HOST_PORT="${AWF_PERSISTED_POSTGRES_HOST_PORT%\'}"
      ;;
  esac
  [ -n "$AWF_PERSISTED_POSTGRES_HOST_PORT" ] && break
done
if [ -n "$AWF_PERSISTED_POSTGRES_HOST_PORT" ]; then
  export AWF_POSTGRES_HOST_PORT="$AWF_PERSISTED_POSTGRES_HOST_PORT"
fi
AWF_PERSISTED_DATABASE_URL=""
for env_file in .env docker/compose/.env; do
  [ -f "$env_file" ] || continue
  AWF_PERSISTED_DATABASE_URL="$(sed -n 's/^[[:space:]]*\(export[[:space:]][[:space:]]*\)\{0,1\}AWF_DATABASE_URL[[:space:]]*=[[:space:]]*//p' "$env_file" | tail -n 1)"
  AWF_PERSISTED_DATABASE_URL="$(awf_strip_unquoted_dotenv_inline_comment "$AWF_PERSISTED_DATABASE_URL")"
  case "$AWF_PERSISTED_DATABASE_URL" in
    \"*\")
      AWF_PERSISTED_DATABASE_URL="${AWF_PERSISTED_DATABASE_URL#\"}"
      AWF_PERSISTED_DATABASE_URL="${AWF_PERSISTED_DATABASE_URL%\"}"
      AWF_PERSISTED_DATABASE_URL="$(awf_decode_double_quoted_dotenv "$AWF_PERSISTED_DATABASE_URL")"
      ;;
    \'*\')
      AWF_PERSISTED_DATABASE_URL="${AWF_PERSISTED_DATABASE_URL#\'}"
      AWF_PERSISTED_DATABASE_URL="${AWF_PERSISTED_DATABASE_URL%\'}"
      ;;
  esac
  [ -n "$AWF_PERSISTED_DATABASE_URL" ] && break
done
if [ -n "$AWF_PERSISTED_DATABASE_URL" ]; then
  export AWF_DATABASE_URL="$AWF_PERSISTED_DATABASE_URL"
else
  unset AWF_DATABASE_URL
fi
if [ -f .env ]; then
  docker compose --env-file .env -f docker/compose/local-service.yml stop
elif [ -f docker/compose/.env ]; then
  docker compose --env-file docker/compose/.env -f docker/compose/local-service.yml stop
else
  docker compose -f docker/compose/local-service.yml stop
fi
git pull
uv sync --extra dev
uv run --python 3.12 --extra dev awf setup --source-checkout "$PWD"
uv run --python 3.12 --extra dev awf start --source-checkout "$PWD"
uv run --python 3.12 --extra dev awf smoke run --project ../awf-eval-project --mocked-local --format pretty
```

Uninstall:

Before deleting the checkout, make sure `~/.awf/config.yml` no longer records it
under `source_checkout`. Refreshing through `awf setup --source-checkout ...` is
not metadata-only. Stop local Core before refreshing source-checkout metadata;
`awf setup` checks the API and Postgres host ports and blocks while the previous
Core stack still holds them. Editing `~/.awf/config.yml` remains the no-stop
option. To refresh the persisted path:

```bash
awf_decode_double_quoted_dotenv() {
  python3 -c 'import re, sys
replacements = {"n": "\n", "r": "\r", "t": "\t", "\\": "\\", chr(34): chr(34), "$": "$"}
print(re.sub(r"\\(.)", lambda match: replacements.get(match[1], match[1]), sys.argv[1]), end="")' "$1"
}
awf_strip_unquoted_dotenv_inline_comment() {
  case "$1" in
    \"*\"[[:space:]]*\#*)
      python3 -c 'import sys
value = sys.argv[1]
escaped = False
for index in range(1, len(value)):
    char = value[index]
    if escaped:
        escaped = False
    elif char == "\\":
        escaped = True
    elif char == chr(34):
        rest = value[index + 1:]
        if rest == "" or rest.lstrip(" \t").startswith("#"):
            print(value[: index + 1], end="")
            raise SystemExit
        break
print(value, end="")' "$1"
      ;;
    \'*\'[[:space:]]*\#*)
      python3 -c 'import sys
value = sys.argv[1]
index = value.find(chr(39), 1)
if index != -1:
    rest = value[index + 1:]
    if rest == "" or rest.lstrip(" \t").startswith("#"):
        print(value[: index + 1], end="")
        raise SystemExit
print(value, end="")' "$1"
      ;;
    \"*|\'*) printf "%s" "$1" ;;
    \#*) printf "%s" "" ;;
    *) printf "%s" "$1" | sed 's/[[:space:]]#.*$//; s/[[:space:]]*$//' ;;
  esac
}
AWF_PERSISTED_API_TOKEN=""
for env_file in .env docker/compose/.env; do
  [ -f "$env_file" ] || continue
  AWF_PERSISTED_API_TOKEN="$(sed -n 's/^[[:space:]]*\(export[[:space:]][[:space:]]*\)\{0,1\}AWF_API_TOKEN[[:space:]]*=[[:space:]]*//p' "$env_file" | tail -n 1)"
  AWF_PERSISTED_API_TOKEN="$(awf_strip_unquoted_dotenv_inline_comment "$AWF_PERSISTED_API_TOKEN")"
  case "$AWF_PERSISTED_API_TOKEN" in
    \"*\")
      AWF_PERSISTED_API_TOKEN="${AWF_PERSISTED_API_TOKEN#\"}"
      AWF_PERSISTED_API_TOKEN="${AWF_PERSISTED_API_TOKEN%\"}"
      AWF_PERSISTED_API_TOKEN="$(awf_decode_double_quoted_dotenv "$AWF_PERSISTED_API_TOKEN")"
      ;;
    \'*\')
      AWF_PERSISTED_API_TOKEN="${AWF_PERSISTED_API_TOKEN#\'}"
      AWF_PERSISTED_API_TOKEN="${AWF_PERSISTED_API_TOKEN%\'}"
      ;;
  esac
  [ -n "$AWF_PERSISTED_API_TOKEN" ] && break
done
if [ -n "$AWF_PERSISTED_API_TOKEN" ]; then
  export AWF_API_TOKEN="$AWF_PERSISTED_API_TOKEN"
elif grep -q '^[[:space:]]*\(export[[:space:]][[:space:]]*\)\{0,1\}AWF_API_TOKEN[[:space:]]*=' .env docker/compose/.env 2>/dev/null; then
  export AWF_API_TOKEN="${AWF_API_TOKEN:-local-dev-token}"
else
  : "${AWF_API_TOKEN:?restore the AWF_API_TOKEN used for the running local Core or persist it in .env or docker/compose/.env before refreshing source-checkout metadata}"
  export AWF_API_TOKEN
fi
AWF_PERSISTED_POSTGRES_PASSWORD=""
for env_file in .env docker/compose/.env; do
  [ -f "$env_file" ] || continue
  AWF_PERSISTED_POSTGRES_PASSWORD="$(sed -n 's/^[[:space:]]*\(export[[:space:]][[:space:]]*\)\{0,1\}AWF_POSTGRES_PASSWORD[[:space:]]*=[[:space:]]*//p' "$env_file" | tail -n 1)"
  AWF_PERSISTED_POSTGRES_PASSWORD="$(awf_strip_unquoted_dotenv_inline_comment "$AWF_PERSISTED_POSTGRES_PASSWORD")"
  case "$AWF_PERSISTED_POSTGRES_PASSWORD" in
    \"*\")
      AWF_PERSISTED_POSTGRES_PASSWORD="${AWF_PERSISTED_POSTGRES_PASSWORD#\"}"
      AWF_PERSISTED_POSTGRES_PASSWORD="${AWF_PERSISTED_POSTGRES_PASSWORD%\"}"
      AWF_PERSISTED_POSTGRES_PASSWORD="$(awf_decode_double_quoted_dotenv "$AWF_PERSISTED_POSTGRES_PASSWORD")"
      ;;
    \'*\')
      AWF_PERSISTED_POSTGRES_PASSWORD="${AWF_PERSISTED_POSTGRES_PASSWORD#\'}"
      AWF_PERSISTED_POSTGRES_PASSWORD="${AWF_PERSISTED_POSTGRES_PASSWORD%\'}"
      ;;
  esac
  [ -n "$AWF_PERSISTED_POSTGRES_PASSWORD" ] && break
done
if [ -n "$AWF_PERSISTED_POSTGRES_PASSWORD" ]; then
  export AWF_POSTGRES_PASSWORD="$AWF_PERSISTED_POSTGRES_PASSWORD"
else
  : "${AWF_POSTGRES_PASSWORD:?restore the AWF_POSTGRES_PASSWORD used for the running local Core or persist it in .env or docker/compose/.env before refreshing source-checkout metadata}"
  export AWF_POSTGRES_PASSWORD
fi
if [ -f .env ]; then
  docker compose --env-file .env -f docker/compose/local-service.yml stop
elif [ -f docker/compose/.env ]; then
  docker compose --env-file docker/compose/.env -f docker/compose/local-service.yml stop
else
  docker compose -f docker/compose/local-service.yml stop
fi
uv run --python 3.12 --extra dev awf setup --source-checkout /path/to/replacement/aira-agent-workspace-fabric
```

or edit `~/.awf/config.yml` and remove only the top-level `source_checkout:` block.
Keep provider, client, and consent entries unless you intentionally want to reset
host setup state.

```bash
cd ..
rm -rf aira-agent-workspace-fabric
```

Only delete the AWF checkout if it was created just for evaluation and no
persisted `source_checkout` metadata points at it.

## After Start

`awf start` prints the local API and console URLs. Use
`http://127.0.0.1:3000` for the console when it is running, and
`http://127.0.0.1:8000/readyz` for a direct local API readiness check.

Next:

- [Project Onboarding](PROJECT_ONBOARDING.md)
- [Upgrade Guide](UPGRADE.md)
- [Uninstall Guide](UNINSTALL.md)
- [PR Monitor Adoption](PR_MONITOR_ADOPTION.md)
- [DX Smoke Command](SMOKE_COMMAND.md)
- [Troubleshooting](TROUBLESHOOTING.md)
