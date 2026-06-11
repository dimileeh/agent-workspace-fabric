# DinD Private-Registry Docker Auth Recipe — Validation

Plan reference: [`DIND_PRIVATE_REGISTRY_PLAN.md`](DIND_PRIVATE_REGISTRY_PLAN.md)

## Requirement-by-requirement status

| # | Requirement | Status | Evidence |
| - | ----------- | ------ | -------- |
| 1 | Explain why private pulls fail under DinD | Complete | `docs/recipes/dind-private-registry-auth.md` §Problem (daemon at `tcp://docker:2375` has no creds) |
| 2 | Copy-pasteable `.awf/workspace.yml` with `DOCKER_CONFIG` dir + `local-file` ro lease targeting `.../config.json` | Complete | recipe lines 19–45; `DOCKER_CONFIG: /run/awf/secrets/docker`, `target: /run/awf/secrets/docker/config.json`, `mode: ro` |
| 3 | Parseable sample profile at documented path | Complete | `docs/recipes/examples/dind-private-registry/.awf/workspace.yml`; test loads & `model_validate`s it |
| 4 | `ref` restrictions + required/optional documented | Complete | recipe §`ref` restrictions (reason-code table) and §Required vs optional leases |
| 5 | Security properties documented | Complete | recipe §Security notes (no token paste, read-only, sanitized metadata, local-mode) |
| 6 | Test: sample parses + resolves to exact read-only mount | Complete | `tests/unit/node/test_dind_private_registry_recipe.py` (2 tests) |

## Evidence — files changed

- `docs/recipes/dind-private-registry-auth.md` (new)
- `docs/recipes/examples/dind-private-registry/.awf/workspace.yml` (new)
- `tests/unit/node/test_dind_private_registry_recipe.py` (new)
- `docs/README.md`, `docs/PROJECT_ONBOARDING.md` (recipe links)
- `plans/DIND_PRIVATE_REGISTRY_PLAN.md`, `plans/DIND_PRIVATE_REGISTRY_VALIDATION.md`
  (this protocol artifact set, added retroactively to satisfy
  `plans/PLAN_EXECUTION_PROTOCOL.md`)

## Evidence — focused tests run

```bash
uv run --python 3.12 --extra dev pytest tests/unit/node/test_dind_private_registry_recipe.py -q
```

- `test_recipe_profile_parses_with_dind_and_docker_config_env` — asserts
  `docker.mode == dind`, `DOCKER_CONFIG == /run/awf/secrets/docker`, and the single
  `local-file` ro secret targets `/run/awf/secrets/docker/config.json`.
- `test_recipe_profile_resolves_config_json_into_readonly_mount` — repoints `ref`
  at a real temp `config.json` and asserts the resolver yields exactly
  `AuthMount(target="/run/awf/secrets/docker/config.json", mode="ro")`.

Broad validation (ruff/mypy/full pytest/coverage gate, OpenAPI drift, console) is
owned by AWF and GitHub CI after the agent phase, per the AWF workspace contract;
not re-run here.

## Gaps / iteration

None. All planned requirements are `Complete`. No documentation-only `PLAN_ONLY_OUTPUT`
risk: user-visible work (recipe, sample profile, test) exists alongside these
protocol artifacts.
