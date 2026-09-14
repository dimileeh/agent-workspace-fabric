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

## PR643 persisted synthetic exports

Imported verbatim via authenticated gh from dimileeh/awf-cloud at `8d2b59ead8ca3f2dcb34e6a049d40d48a2fd7c21`. Core audited base: `2cc77e2c235560aabcfcb28f08cb22ba814b858d`. Historical producer provenance is preserved verbatim; this import is not a new database audit. Source directory: `tests/fixtures/console_telemetry/`.

- `persisted_active_no_estimate.json`: `8a6e57b72640e80b5c8efbf876f23692fd9227e0ff9d7ae914352a9d207ba2a0`
- `persisted_checkpoint_no_samples.json`: `368739f44f026935f69f9563ad423ac7daeb98f6dfe6688db92314acc2a129fc`
- `persisted_cold_absent.json`: `5b82754a9a54974af5ac09ed641746e1519b60ab2ea9503bb85ac21f6a4e5511`
- `persisted_cpu_lagging_memory.json`: `f4b53e86fa241ff772253afed4ef93eab3d9458428a5e9f7eac38d81326ec872`
- `persisted_day_two_containers.json`: `77fe90cbba816947c0b822d2691d2169e8463532e35728b9606958214e5b2efb`
- `persisted_estimate_no_checkpoint.json`: `048e64a90d4c5cb7028fa6d0a024e467d7a4b272623ed359c1d23538dcf674c9`
- `persisted_missing_family.json`: `66d63d9581278a1f87a6876f1444b93799d64266b40459b71aad0d1de8ed39e9`
- `persisted_provenance.json`: `f35ba114e3025243f3cf40b7bf323d0d8553eaeaff4c44b1945992f5ae37fa4c`
- `persisted_retained_cleaned_terminal.json`: `59db36232bc10dc6b630fd32009cba01c106d4fa690e20dacd3514361bf6ac39`
- `persisted_shared_unallocated.json`: `b9ddc691307958bf4e5b9fb77a9d3bffe882568ad924a457113b6e50edcc4dcc`
- `persisted_stale_series.json`: `8c92244dcb45484a5c16df0ba2a32f9b3b4920a77b6c31233b6d059d68371a3f`
- `persisted_terminal_1h.json`: `e7b70a9cdb9630b40f639f18845f175c8f9bbd2a28593da31d4bfb164128d745`
- `persisted_terminal_2h_under_1h.json`: `eb7d87421a8675adc549dc350317989ba40244d6ee00f55ba2ebc018a4f042fe`
- `persisted_unpriced_allocation.json`: `6d61a29f888fde55a3c5d7e3ce5cc42a4a9ca74b7cfacc0f0bdd23bdb2f0e2c6`
