import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { parseVMTelemetry, projectVMTelemetry, vmTelemetryFreshness } from "./console-vm-telemetry.ts";

// Exported through the frozen Cloud VMTelemetryResponse, not a hand-invented DTO.
const fixture = JSON.parse(readFileSync(new URL("./fixtures/console-workspace-telemetry/vm-node.json", import.meta.url)));
const scope = { workspaceId: "ws", orgId: "org", projectId: "project", view: "1h" };
const now = Date.parse(fixture.window_end_at);
const parse = (body = structuredClone(fixture), expected = scope) => parseVMTelemetry(body, expected);

test("actual VM DTO retains exact uint64 and explicit multi-physical history", () => {
  const p = parse();
  assert.ok(p);
  assert.equal(p.node.provider_id, "102");
  assert.equal(p.historical_nodes[0].provider_id, "18446744073709551615");
  assert.equal(p.resource_kind, "vm_node");
  assert.equal(p.reservation.values.steady_cpu, 2);
  assert.equal(p.node_cost.compute.estimated_usd, "0.015733117");
});

for (const [name, mutate] of Object.entries({
  workspace: p => { p.workspace.workspace_record_id = "foreign"; },
  org: p => { p.workspace.org_id = "foreign"; },
  project: p => { p.workspace.project_id = "foreign"; },
  view: p => { p.view = "24h"; },
  owner: p => { p.workspace.owner_record_id = ""; },
  authGeneration: p => { p.workspace.authorization_generation = 0; },
  foreignNode: p => { p.node.org_id = "foreign"; },
  numericId: p => { p.node.provider_id = 102; },
  overflow: p => { p.node.provider_id = "18446744073709551616"; },
  pod: p => { p.ownership = { provider_resource_uid: "pod" }; },
  ratio: p => { p.cpu_utilization_samples[0].value = "1.01"; },
  coerced: p => { p.cpu_utilization_samples[0].value = 0.5; },
  wrongUnit: p => { p.cpu_utilization_samples[0].unit = "cores"; },
  wrongMetric: p => { p.cpu_utilization_samples[0].metric_type = "kubernetes.io/container/cpu/core_usage_time"; },
  negative: p => { p.cpu_utilization_samples[0].value = "-1"; },
  future: p => { p.cpu_utilization_samples[0].sample_time = "2026-10-30T10:00:00.000001Z"; },
  old: p => { p.cpu_utilization_samples[0].sample_time = "2026-10-30T08:59:59.999999Z"; },
  duplicate: p => { p.cpu_utilization_samples.push(p.cpu_utilization_samples[0]); },
  futureBoot: p => { p.cpu_utilization_samples[0].boot_generation = 5; },
  oversized: p => { p.cpu_utilization_samples = Array(2049).fill(p.cpu_utilization_samples[0]); },
  hidden: p => { p.node_visibility = "not_authorized"; },
  stale: p => { p.observed_at = "2026-10-30T09:00:00Z"; },
  inventedComplete: p => { p.state = p.quality = "success"; },
  historyDuplicate: p => { p.historical_nodes[0].provider_id = "101"; },
  historyOrder: p => { p.historical_nodes.reverse(); },
  historyLogical: p => { p.historical_nodes[0].node_id = "foreign"; },
  historyBoot: p => { p.historical_nodes[0].boot_generation = 3; },
  historyLatest: p => { p.previous_node = p.historical_nodes[0]; },
  oversizedHistory: p => { p.historical_nodes = Array(257).fill(p.previous_node); },
  reservationFuture: p => { p.reservation.values.reserved_at = "2026-10-31T10:00:00Z"; },
  reservationStale: p => { p.reservation.observed_at = "2026-10-30T08:00:00Z"; },
  reservationSecret: p => { p.reservation.values.token = "not-a-real-secret"; },
  workspaceBill: p => { p.workspace_cost.estimated_usd = "1"; },
  unknownPowered: p => { p.node_cost.observed_powered_seconds = null; },
  idle: p => { p.node_cost.observed_idle_seconds = 3601; },
  unpricedAmount: p => { p.node_cost.compute.estimate_state = "unpriced"; },
  version: p => { p.node_cost.compute.rate_table_version = "other"; },
  missingRates: p => { p.node_cost.compute.evidence.rates = []; },
  rateFuture: p => { p.node_cost.compute.evidence.rates[0].effective_at = "2026-11-01T00:00:00Z"; },
  rateHash: p => { p.node_cost.compute.evidence.rates[0].source_sha256 = "invalid"; },
  rateURL: p => { p.node_cost.compute.evidence.rates[0].source_url = "javascript:alert(1)"; },
  rateSecret: p => { p.node_cost.compute.evidence.rates[0].secret = "synthetic"; },
})) test(`VM parser rejects ${name}`, () => {
  const p = structuredClone(fixture);
  mutate(p);
  assert.equal(parse(p), null);
});

test("project-only response retains own reservation without node disclosure", () => {
  const p = structuredClone(fixture);
  Object.assign(p, { node_visibility: "not_authorized", node: null, previous_node: null, historical_nodes: [],
    node_cost: null, observed_at: null, cpu_utilization_samples: [], memory_bytes_samples: [] });
  assert.ok(parse(p));
  p.historical_nodes = fixture.historical_nodes;
  assert.equal(parse(p), null);
});

test("legacy optional history fields and independent peak reservations remain compatible", () => {
  const p = structuredClone(fixture);
  delete p.previous_node;
  delete p.historical_nodes;
  p.reservation.values.peak_cpu = 0;
  assert.ok(parse(p));
});

test("current VM meter never relabels prior boot samples or absent memory as zero", () => {
  const p = parse();
  const view = projectVMTelemetry(p);
  assert.equal(view.cpu.value, 0.63);
  assert.equal(view.memory.value, null);
  assert.deepEqual(view.cpu.series.map(s => s.bootGeneration), [2, 3, 4]);
  assert.ok(view.cpu.series.every(s => s.points.length === 1));
  p.cpu_utilization_samples.pop();
  assert.equal(projectVMTelemetry(p).cpu.value, null);
});

test("sample ages and reservation ages are independent of hidden widget sections", () => {
  const p = parse();
  assert.equal(vmTelemetryFreshness(p, now).isStale, false);
  assert.equal(vmTelemetryFreshness(p, now + 301_000).isStale, true);
  assert.equal(vmTelemetryFreshness(p, now - 120_000).hasFutureTimestamp, true);
  assert.equal(vmTelemetryFreshness(p, now + 301_000, { telemetry: false, allocation: false }).isStale, false);
});

test("submillisecond sample identity is not rounded into duplicates", () => {
  const p = structuredClone(fixture);
  p.cpu_utilization_samples = [1, 2].map(n => ({ ...p.cpu_utilization_samples[2], sample_time: `2026-10-30T09:59:00.00000${n}Z` }));
  assert.ok(parse(p));
});
