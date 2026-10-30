/** VM variant of the shared telemetry contract. No Pod identity or cost allocation. */
import { compareTimestampInstants as compare, isFiniteTimestampString, timestampInstantKey } from "./console-workspace-telemetry-timestamps.ts";
import type { TelemetryQuality, TelemetryViewWindow } from "./console-workspace-telemetry-types.ts";

export type VMNode = {
  org_id: string; node_id: string; provider_id: string; recovery_generation: number; boot_generation: number;
};
export type VMSample = {
  boot_generation: number; sample_time: string; value: string; unit: "ratio" | "bytes";
  metric_type: string; quality: TelemetryQuality;
};
export type VMRate = {
  version: string; sku: string; unit: "instance_hour" | "gib_hour"; per_hour: string; currency: "USD";
  effective_at: string; source_url: string; source_sha256: string;
};
export type VMEstimate = {
  estimated_usd: string | null; currency: "USD"; estimate_state: "complete" | "partial" | "unpriced";
  rate_table_version: string; priced_interval_seconds: number; unpriced_interval_seconds: number;
  evidence: { rates?: VMRate[] };
};
export type VMReservation = {
  observed_at: string; state: "active" | "none"; quality: "ok" | "stale";
  values: {
    id: string; steady_cpu: number; steady_memory_gb: number; peak_cpu: number; peak_memory_gb: number;
    disk_mb: number | null; dind_slots: number; phase: string; reserved_at: string; released_at: null;
  } | null;
};
export type VMNodeCost = {
  scope: "shared_node_observed_window"; coverage: "unknown" | "partial";
  observed_powered_seconds: number | null; observed_idle_seconds: number | null;
  observed_storage_gib_seconds: string | null; observed_idle_storage_gib_seconds: string | null;
  compute: VMEstimate; idle_compute: VMEstimate; persistent_storage: VMEstimate; idle_storage: VMEstimate | null;
};
export type VMTelemetry = {
  resource_kind: "vm_node";
  workspace: { org_id: string; project_id: string; workspace_record_id: string; owner_record_id: string; authorization_generation: number };
  node: VMNode | null; previous_node: VMNode | null; historical_nodes: VMNode[];
  node_visibility: "authorized" | "not_authorized"; window_end_at: string; view: TelemetryViewWindow;
  state: "partial" | "stale"; quality: "partial" | "stale"; observed_at: string | null; stale_after_seconds: number;
  cpu_utilization_samples: VMSample[]; memory_bytes_samples: VMSample[]; reservation: VMReservation | null;
  workspace_cost: { estimate_state: "unallocated"; estimated_usd: null }; node_cost: VMNodeCost | null;
  data_quality_notes: string[];
};
export type VMReadScope = { workspaceId: string; orgId?: string | null; projectId?: string | null; view?: TelemetryViewWindow };
type ObjectValue = Record<string, unknown>;
const record = (v: unknown): v is ObjectValue => v !== null && typeof v === "object" && !Array.isArray(v);
const keys = (v: ObjectValue, required: string[], optional: string[] = []) => required.every(k => Object.hasOwn(v, k))
  && Object.keys(v).every(k => required.includes(k) || optional.includes(k));
const text = (v: unknown, max = 128): v is string => typeof v === "string" && v.length > 0 && v.length <= max;
const integer = (v: unknown): v is number => typeof v === "number" && Number.isSafeInteger(v) && v >= 0;
const generation = (v: unknown): v is number => integer(v) && v > 0;
const decimal = (v: unknown): v is string => text(v, 64) && /^(?:0|[1-9]\d*)(?:\.\d+)?$/.test(v) && Number(v) <= 1e15;
const time = (v: unknown): v is string => typeof v === "string" && isFiniteTimestampString(v);
const duration = { "1h": 3600, "6h": 21600, "24h": 86400 };
const estimateKeys = ["estimated_usd", "currency", "estimate_state", "rate_table_version", "priced_interval_seconds", "unpriced_interval_seconds", "evidence"];
const exclusions = ["credits", "taxes", "llm", "shared_infrastructure"];

function node(v: unknown, org: string): v is VMNode {
  return record(v) && keys(v, ["org_id", "node_id", "provider_id", "recovery_generation", "boot_generation"])
    && v.org_id === org && text(v.node_id) && text(v.provider_id, 20) && /^[1-9][0-9]*$/.test(v.provider_id)
    && BigInt(v.provider_id) <= BigInt("18446744073709551615") && generation(v.recovery_generation) && generation(v.boot_generation);
}
function estimate(v: unknown, end: string): v is VMEstimate {
  if (!record(v) || !keys(v, estimateKeys) || v.currency !== "USD" || !integer(v.priced_interval_seconds)
    || !integer(v.unpriced_interval_seconds) || !record(v.evidence)) return false;
  const e = v.evidence;
  if (!keys(e, [], ["allocation_kind", "source", "excluded", "coverage", "interval_unit", "rates", "unpriced_reason"])
    || (e.allocation_kind !== undefined && e.allocation_kind !== "shared_node")
    || (e.source != null && !["gce_full_instance", "core_quiescence", "sampled_disk_allocation", "stopped_disk_allocation"].includes(String(e.source)))
    || (e.coverage !== undefined && e.coverage !== "observed_intervals_only")
    || (e.interval_unit !== undefined && e.interval_unit !== "resource_seconds")
    || (e.unpriced_reason != null && !["duration_unknown", "missing_profile_or_reviewed_rate"].includes(String(e.unpriced_reason)))
    || (e.excluded !== undefined && (!Array.isArray(e.excluded) || e.excluded.length > 4 || e.excluded.some(x => !exclusions.includes(x))))) return false;
  const rates = e.rates ?? [];
  if (!Array.isArray(rates) || rates.length > 32 || e.rates === null) return false;
  for (const rate of rates) {
    if (!record(rate) || !keys(rate, ["version", "sku", "unit", "per_hour", "currency", "effective_at", "source_url", "source_sha256"])
      || !text(rate.version) || !text(rate.sku) || !["instance_hour", "gib_hour"].includes(String(rate.unit))
      || !decimal(rate.per_hour) || rate.currency !== "USD" || !time(rate.effective_at) || compare(rate.effective_at, end) > 0
      || !["https://cloud.google.com/products/compute/pricing/general-purpose?hl=en", "https://cloud.google.com/compute/disks-image-pricing?hl=en"].includes(String(rate.source_url))
      || !text(rate.source_sha256, 64) || !/^[0-9a-f]{64}$/.test(rate.source_sha256)) return false;
  }
  if (v.estimate_state === "unpriced") return v.estimated_usd === null && v.priced_interval_seconds === 0 && v.rate_table_version === "";
  return ["complete", "partial"].includes(String(v.estimate_state)) && decimal(v.estimated_usd)
    && v.priced_interval_seconds > 0 && (v.estimate_state === "complete") === (v.unpriced_interval_seconds === 0)
    && e.allocation_kind === "shared_node" && JSON.stringify(e.excluded) === JSON.stringify(exclusions)
    && rates.length > 0 && v.rate_table_version === [...new Set(rates.map(r => r.version))].sort().join("+");
}
function reservation(v: unknown, end: string, staleAfter: number): v is VMReservation {
  if (!record(v) || !keys(v, ["observed_at", "state", "quality", "values"]) || !time(v.observed_at)
    || compare(v.observed_at, end) > 0 || !["active", "none"].includes(String(v.state)) || !["ok", "stale"].includes(String(v.quality))
    || (compare(v.observed_at, end, -staleAfter * 1000) < 0 && v.quality !== "stale")
    || (v.state === "active") !== (v.values !== null)) return false;
  const r = v.values;
  const capacity = (n: unknown) => typeof n === "number" && Number.isFinite(n) && n >= 0 && n <= 1e9;
  return r === null || (record(r) && keys(r, ["id", "steady_cpu", "steady_memory_gb", "peak_cpu", "peak_memory_gb", "disk_mb", "dind_slots", "phase", "reserved_at", "released_at"])
    && text(r.id) && [r.steady_cpu, r.steady_memory_gb, r.peak_cpu, r.peak_memory_gb].every(capacity)
    && (r.disk_mb === null || integer(r.disk_mb)) && integer(r.dind_slots) && r.dind_slots <= 1e6
    && text(r.phase, 64) && time(r.reserved_at) && compare(r.reserved_at, v.observed_at) <= 0 && r.released_at === null);
}

export function parseVMTelemetry(payload: unknown, scope: VMReadScope): VMTelemetry | null {
  if (!record(payload)) return null;
  const p = payload;
  if (!keys(p, ["resource_kind", "workspace", "node", "node_visibility", "window_end_at", "view", "state", "quality", "observed_at",
    "stale_after_seconds", "cpu_utilization_samples", "memory_bytes_samples", "reservation", "workspace_cost", "node_cost", "data_quality_notes"],
    ["previous_node", "historical_nodes"]) || p.resource_kind !== "vm_node"
    || (p.view !== "1h" && p.view !== "6h" && p.view !== "24h") || (scope.view !== undefined && p.view !== scope.view)
    || !["partial", "stale"].includes(String(p.state)) || p.quality !== p.state || !time(p.window_end_at)
    || !integer(p.stale_after_seconds) || p.stale_after_seconds < 60 || p.stale_after_seconds > 300
    || !Array.isArray(p.data_quality_notes) || p.data_quality_notes.length > 16 || !p.data_quality_notes.every(n => text(n, 64))) return null;
  const w = p.workspace;
  if (!record(w) || !keys(w, ["org_id", "project_id", "workspace_record_id", "owner_record_id", "authorization_generation"])
    || ![w.org_id, w.project_id, w.workspace_record_id, w.owner_record_id].every(v => text(v))
    || !generation(w.authorization_generation) || w.workspace_record_id !== scope.workspaceId
    || (scope.orgId != null && w.org_id !== scope.orgId) || (scope.projectId != null && w.project_id !== scope.projectId)) return null;
  const org = String(w.org_id), current = p.node;
  if ((current !== null && !node(current, org)) || !["authorized", "not_authorized"].includes(String(p.node_visibility))
    || (p.node_visibility === "authorized" && current === null)) return null;
  const previous = p.previous_node ?? null, history = p.historical_nodes ?? [];
  if (p.historical_nodes === null || !Array.isArray(history) || history.length > 256) return null;
  const before = (old: VMNode, next: VMNode) => old.node_id === next.node_id && old.provider_id !== next.provider_id
    && old.recovery_generation < next.recovery_generation && old.boot_generation < next.boot_generation;
  if (previous !== null && (!node(previous, org) || !node(current, org) || p.node_visibility !== "authorized" || !before(previous, current))) return null;
  if (history.length) {
    if (!node(current, org) || !node(previous, org) || p.node_visibility !== "authorized") return null;
    const seen = new Set([current.provider_id]);
    for (let i = 0; i < history.length; i++) {
      const old = history[i], next = history[i + 1] ?? current;
      if (!node(old, org) || !node(next, org) || !before(old, next) || seen.has(old.provider_id)) return null;
      seen.add(old.provider_id);
    }
    const latest = history[history.length - 1];
    if (Object.keys(previous).some(k => previous[k as keyof VMNode] !== latest[k])) return null;
  }
  const end = p.window_end_at, seconds = duration[p.view];
  const inWindow = (at: string) => compare(at, end) <= 0 && compare(at, end, -seconds * 1000) >= 0;
  if (p.observed_at !== null && (!time(p.observed_at) || !inWindow(p.observed_at)
    || (compare(p.observed_at, end, -p.stale_after_seconds * 1000) < 0 && p.state !== "stale"))) return null;
  for (const [field, unit, metric] of [["cpu_utilization_samples", "ratio", "compute.googleapis.com/instance/cpu/utilization"],
    ["memory_bytes_samples", "bytes", "agent.googleapis.com/memory/bytes_used"]]) {
    const samples = p[field];
    if (!Array.isArray(samples) || samples.length > 2048) return null;
    const seen = new Set<string>();
    for (const s of samples) {
      if (!record(s) || !keys(s, ["boot_generation", "metric_type", "sample_time", "value", "unit", "quality"])
        || !node(current, org) || !generation(s.boot_generation) || s.boot_generation > current.boot_generation
        || s.unit !== unit || s.metric_type !== metric || !decimal(s.value) || (unit === "ratio" && Number(s.value) > 1)
        || !["ok", "partial", "stale"].includes(String(s.quality)) || !time(s.sample_time) || !inWindow(s.sample_time)) return null;
      const key = `${s.boot_generation}:${timestampInstantKey(s.sample_time)}`;
      if (seen.has(key)) return null;
      seen.add(key);
    }
  }
  if (p.node_visibility === "not_authorized" && (current !== null || p.node_cost !== null || p.observed_at !== null
    || (p.cpu_utilization_samples as unknown[]).length || (p.memory_bytes_samples as unknown[]).length || previous !== null || history.length)) return null;
  if (p.reservation !== null && !reservation(p.reservation, end, p.stale_after_seconds)) return null;
  const cost = p.workspace_cost;
  if (!record(cost) || !keys(cost, estimateKeys) || cost.estimate_state !== "unallocated" || cost.estimated_usd !== null
    || cost.currency !== "USD" || cost.priced_interval_seconds !== 0 || cost.unpriced_interval_seconds !== 0 || cost.rate_table_version !== ""
    || !record(cost.evidence) || !keys(cost.evidence, [], ["allocation_kind", "unpriced_reason"])
    || (cost.evidence.allocation_kind !== undefined && cost.evidence.allocation_kind !== "unallocated")
    || (cost.evidence.unpriced_reason !== undefined && cost.evidence.unpriced_reason !== "no_workspace_allocation")) return null;
  const c = p.node_cost;
  if (c !== null) {
    if (!node(current, org) || !record(c) || !keys(c, ["scope", "observed_powered_seconds", "observed_idle_seconds", "observed_storage_gib_seconds",
      "observed_idle_storage_gib_seconds", "coverage", "compute", "idle_compute", "persistent_storage", "idle_storage"])
      || c.scope !== "shared_node_observed_window" || !["unknown", "partial"].includes(String(c.coverage))
      || (c.observed_powered_seconds !== null && (!integer(c.observed_powered_seconds) || c.observed_powered_seconds > seconds))
      || (c.observed_idle_seconds !== null && (!integer(c.observed_idle_seconds) || !integer(c.observed_powered_seconds) || c.observed_idle_seconds > c.observed_powered_seconds))
      || (c.observed_storage_gib_seconds !== null && !decimal(c.observed_storage_gib_seconds))
      || (c.observed_idle_storage_gib_seconds !== null && !decimal(c.observed_idle_storage_gib_seconds))
      || !estimate(c.compute, end) || !estimate(c.idle_compute, end) || !estimate(c.persistent_storage, end)
      || (c.idle_storage !== null && !estimate(c.idle_storage, end))) return null;
    if (c.compute.priced_interval_seconds + c.compute.unpriced_interval_seconds > Number(c.observed_powered_seconds ?? 0)
      || c.idle_compute.priced_interval_seconds + c.idle_compute.unpriced_interval_seconds > Number(c.observed_idle_seconds ?? 0)) return null;
  }
  // The discriminator and every projected field have been checked above.
  return { ...p, previous_node: previous, historical_nodes: history } as VMTelemetry;
}

function latestSample(samples: VMSample[], boot: number | undefined) {
  let latest: VMSample | undefined;
  for (const sample of samples) if (sample.boot_generation === boot && (!latest || compare(sample.sample_time, latest.sample_time) > 0)) latest = sample;
  return latest;
}
function metric(samples: VMSample[], boot: number | undefined) {
  const sorted = [...samples].sort((a, b) => compare(a.sample_time, b.sample_time));
  const latest = latestSample(samples, boot);
  const groups = new Map<number, { sampleTime: string; value: number; quality: TelemetryQuality }[]>();
  for (const s of sorted) {
    if (!groups.has(s.boot_generation)) groups.set(s.boot_generation, []);
    groups.get(s.boot_generation)!.push({ sampleTime: s.sample_time, value: Number(s.value), quality: s.quality });
  }
  return {
    value: latest ? Number(latest.value) : null, sampleTime: latest?.sample_time ?? null,
    quality: latest?.quality ?? null,
    series: [...groups].sort(([a], [b]) => a - b).map(([bootGeneration, points]) => ({ bootGeneration, points })),
  };
}
export function projectVMTelemetry(data: VMTelemetry) {
  return { kind: "vm_node" as const, data, cpu: metric(data.cpu_utilization_samples, data.node?.boot_generation),
    memory: metric(data.memory_bytes_samples, data.node?.boot_generation) };
}
export type VMTelemetryView = ReturnType<typeof projectVMTelemetry> & { isStale?: boolean; hasFutureTimestamp?: boolean };

export function vmTelemetryFreshness(data: VMTelemetry, now: number,
  gates: { telemetry: boolean; allocation: boolean; cost?: boolean } = { telemetry: true, allocation: true, cost: true }) {
  const samples = gates.telemetry ? [latestSample(data.cpu_utilization_samples, data.node?.boot_generation), latestSample(data.memory_bytes_samples, data.node?.boot_generation)] : [];
  const times = [...samples.map(s => s?.sample_time), gates.telemetry ? data.observed_at : null,
    gates.allocation ? data.reservation?.observed_at : null, gates.cost && data.node_cost ? data.window_end_at : null].filter((v): v is string => v != null);
  const hasFutureTimestamp = times.some(at => Date.parse(at) > now);
  return { hasFutureTimestamp, isStale: hasFutureTimestamp || times.some(at => now - Date.parse(at) > data.stale_after_seconds * 1000)
    || (gates.telemetry && (data.state === "stale" || samples.some(s => s?.quality === "stale")))
    || (gates.allocation && data.reservation?.quality === "stale") };
}
