# AWF Start Console Plan

## Summary

Make `awf start` start the local web console by default, matching the raw
`docker compose up -d --build` first-run experience. The command should still be
a friendly wrapper around the existing service bootstrap path, not a second
Compose implementation.

## Implementation

- Add `--headless` and `--console-port <PORT>` to `awf start`.
- Reject `--headless --console-port <PORT>` before bootstrap.
- Pass `ServiceBootstrapOptions(start_console=True)` from `awf start` by
  default; pass `False` when `--headless` is set.
- Extend bootstrap stage construction so `start_console=True` appends a
  `console` Compose stage after `api_worker`.
- Keep lower-level `awf service bootstrap` behavior unchanged by making
  `ServiceBootstrapOptions.start_console` default to `False`.
- When `--console-port` is provided, pass `AWF_CONSOLE_HOST_PORT` into the local
  service environment before resolving settings and bootstrapping.
- Derive `ServiceSettings.console_url` from `AWF_CONSOLE_HOST_PORT` when no
  explicit `AWF_CONSOLE_URL` is configured.
- In headless success output, omit the console URL and console-open next step.

## Tests

- CLI tests for help text, default console startup, headless startup, console
  port override, and incompatible flag handling.
- Bootstrap tests proving the console stage is opt-in at the bootstrap layer.
- Config tests proving `AWF_CONSOLE_HOST_PORT` derives the displayed console URL,
  explicit `AWF_CONSOLE_URL` wins, and invalid ports are rejected.
- Focused docs tests for updated first-run documentation if needed.

## Assumptions

- "Launch console" means start the Docker Compose `console` service on
  localhost, not open a browser tab.
- The public CLI spelling is `--console-port`, matching existing hyphenated
  Typer flag style.
