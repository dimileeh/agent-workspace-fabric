# Console workspace telemetry fixtures — provenance

Synthetic producer contract fixtures (not live tenant records).

| Field | Value |
| --- | --- |
| Producer repo | awf-cloud |
| Commit | `be0d7a3ac9871ef951860666a236c9c481f45c98` |
| Source path | `console_telemetry/contract_fixtures` |
| Cases | `success.json`, `partial.json`, `stale.json`, `unallocated.json` |

Record SHA-256 after write:

```bash
sha256sum apps/console/lib/fixtures/console-workspace-telemetry/*.json
```

Do not invent alternate field shapes. Schema is under active review; keep compatibility explicit.
