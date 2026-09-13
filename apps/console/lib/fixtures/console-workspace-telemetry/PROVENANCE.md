# Console workspace telemetry fixtures — provenance

Synthetic producer contract fixtures (not live tenant records).

| Field | Value |
| --- | --- |
| Producer repo | awf-cloud |
| Commit | `be0d7a3ac9871ef951860666a236c9c481f45c98` |
| Source path | `console_telemetry/contract_fixtures` |
| Cases | `success.json`, `partial.json`, `stale.json`, `unallocated.json` |

## SHA-256 (recorded 2026-09-12)

```
463793cc73b187044a46391dd16375af3a95d81a7e217698e9df3c31e43debb0  success.json
9ec02a35ed0135dec25888944123b4c6c3f8d7e6db854e581d280a55e7ec0214  partial.json
5bbf48e1af8d04106afcd974f41d16e7b1400af8b77646c7a65031be642a2401  stale.json
4e3099d5cbea4a15f747adfb76ce849d654d93e063e214c38c4c9887b069805f  unallocated.json
```

Do not invent alternate field shapes. Schema is under active review; keep compatibility explicit.

## Stage 3 paired contract convergence (2026-09-13)

The four files and hashes above are preserved exactly. They remain canonical
producer presentations from the recorded commit, not PostgreSQL query exports.

Authenticated source inspection of awf-cloud
`70567f1e152d01b2220244481e23b1fab5422284` covered
`apps/api/awf_cloud_api/console_telemetry/{types.py,query.py,metrics_normalize.py,fixtures.py}`
and `tests/test_console_telemetry_persistence_postgres.py`.
That source establishes nullable admitted/estimate, nullable compute class,
recorded unpriced state, and memory gauge `interval_start=null` /
`interval_end=sample_time`. Expected consumer normalization: only the internal
memory start becomes sample time; UID, container, value, unit and quality remain
unchanged. CPU intervals remain required.

New tests named `paired` and harness `scenario` variants are consumer mutations
of these original fixtures, **not** additional Cloud-produced query payloads.
Their clock starts from `2026-09-12T12:00:00Z`; lag probes retain observed time
`11:59:00Z` with chart end `12:00:00Z`. Scope/window additions exercise the
approved paired decision; they are not attributed to the pinned source.

Counterpart exports from actual normalizers through isolated PostgreSQL, query,
and `to_contract_dict` have not been supplied. Final sync must record the new
producer commit, generating command/test, fixed query clock, scenario, expected
normalization and SHA-256, then rerun the focused consumer and independent audit.
Do not relabel the current four files or add invented query DTOs to fill the gap.
See [the contract and evidence matrix](../../../../../docs/CONSOLE_WORKSPACE_TELEMETRY_CONTRACT.md).
