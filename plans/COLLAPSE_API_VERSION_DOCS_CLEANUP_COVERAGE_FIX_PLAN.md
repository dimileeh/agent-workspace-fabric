# Collapse API Version Coverage Fix Plan

## Summary

The first full local coverage run after collapsing workspace creation to the
canonical v1 rich path failed because remaining tests still assumed the retired
legacy v1 behavior: workspace creation could produce no task attempt and only a
single `workspace.created` event. The implementation now intentionally creates
task/attempt metadata and richer lifecycle events through `POST /v1/workspaces`.

## Checklist

- [x] Preserve response compatibility for `env_profile` on legacy-shaped create
      requests by mapping it from the canonical profile reference.
- [x] Update stale tests so canonical v1 creates are expected to have task
      attempts and richer event streams.
- [x] Keep payload matching compatible with older persisted rows where the
      workspace has only flat fields.
- [x] Re-run the previously failed node IDs.
- [x] Re-run the full `-n 20` 99% coverage gate until it passes.
- [ ] Commit the full API-collapse cleanup on a feature branch, push it, open a
      PR to `development`, rebuild/restart AWF, and attach a PR monitor.

## Validation Commands

```bash
uv run --python 3.12 --extra dev pytest <failed-node-ids> -q
uv run --python 3.12 --extra dev pytest -n 20 --dist=loadscope --cov=awf --cov-report=term-missing --cov-fail-under=99
```
