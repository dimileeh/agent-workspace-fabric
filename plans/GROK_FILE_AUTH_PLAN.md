# Grok File Auth Plan

## Problem
AWF's Grok provider currently treats `XAI_API_KEY` as the only usable auth
signal. Local investigation showed that host Grok OAuth auth works through
`~/.grok/auth.json`, and that a filtered copy of `auth.json` plus `config.toml`
works inside `awf-agent-runtime`. Mounting or copying the whole `~/.grok`
directory is unsafe because it can contain host-platform managed binaries that
shadow the Linux runtime `grok` binary.

## Scope
- Support Grok file auth from host `~/.grok` before falling back to
  `XAI_API_KEY`.
- Copy only portable Grok auth/config files into per-workspace isolated auth:
  `auth.json` and optional `config.toml`.
- Keep Cursor unchanged; Cursor continues to use `CURSOR_API_KEY` until a
  portable OAuth token source is proven.
- Avoid logging or serializing secret values.

## Requirements
- Provider readiness reports Grok ready when `~/.grok/auth.json` exists.
- Grok file auth has precedence over `XAI_API_KEY` in readiness payloads.
- Missing Grok file auth continues to fall back to `XAI_API_KEY`.
- Workspace auth mount resolution creates `/home/agent/.grok` from a filtered
  isolated copy, not a direct whole-directory mount.
- The filtered copy excludes non-portable runtime/cache/session files such as
  `bin`, `downloads`, `sessions`, logs, and platform-specific managed binaries.
- Existing `XAI_API_KEY` environment propagation remains unchanged.

## Implementation Steps
1. Add failing tests for Grok file readiness, env fallback, and file precedence.
2. Add failing tests for filtered per-workspace Grok auth copying.
3. Add Grok target/file constants and filtered copy helper in
   `src/awf/node/auth_mounts.py`.
4. Extend `src/awf/service/provider_readiness.py` to accept `host_home` for
   Grok and check file auth before env auth.
5. Update docs/reason text only if test expectations or operator clarity
   require it.

## Verification
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_service_auth_mounts.py tests/unit/service/test_provider_readiness_parts/test_provider_readiness_part_001.py tests/unit/service/test_provider_readiness_parts/test_provider_readiness_part_002.py -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/node/auth_mounts.py src/awf/service/provider_readiness.py tests/unit/node/test_service_auth_mounts.py tests/unit/service/test_provider_readiness_parts/test_provider_readiness_part_001.py tests/unit/service/test_provider_readiness_parts/test_provider_readiness_part_002.py`
- `uv run --python 3.12 --extra dev mypy src/awf/node/auth_mounts.py src/awf/service/provider_readiness.py`
