# Changelog

## Unreleased

- Added companion `environment_secrets` for env-backed companion service
  secrets, while clarifying that literal companion `environment` values reject
  Docker Compose interpolation.
- Canonicalized the first-run quickstart and added upgrade documentation.
- Improved DX-oriented CLI pretty output for profile preview and Core release
  readiness.
- Added local console discovery to smoke reports through the default
  `http://localhost:3000` URL.

## 0.1.0

- Initial local AWF Core MVP with CLI, REST API, MCP primitives, profile-driven
  workspace execution, local service bootstrap, PR monitor flows, and smoke
  diagnostics.
