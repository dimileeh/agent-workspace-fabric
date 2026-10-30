import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { registerHooks } from "node:module";
import { fileURLToPath } from "node:url";
import test from "node:test";
import ts from "typescript";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

// Render the actual component tree without a browser, DOM shim or mocked React.
const root = new URL("../", import.meta.url);
registerHooks({
  resolve(specifier, context, next) {
    const base = specifier.startsWith("@/") ? new URL(specifier.slice(2), root)
      : specifier.startsWith(".") && context.parentURL?.startsWith(root.href) ? new URL(specifier, context.parentURL) : null;
    if (base) for (const ext of ["", ".ts", ".tsx"]) {
      const candidate = new URL(base.href + ext);
      if (existsSync(candidate) && /\.tsx?$/.test(candidate.pathname)) return { url: candidate.href, shortCircuit: true };
    }
    return next(specifier, context);
  },
  load(url, context, next) {
    if (url.startsWith(root.href) && /\.tsx?$/.test(url)) return {
      format: "module", shortCircuit: true,
      source: ts.transpileModule(readFileSync(new URL(url), "utf8"), {
        fileName: fileURLToPath(url), compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX },
      }).outputText,
    };
    return next(url, context);
  },
});
const { ConsoleWorkspaceTelemetry } = await import("../components/console-workspace-telemetry.tsx");
const { ConsoleWorkspaceTelemetryContainer } = await import("../components/console-workspace-telemetry-container.tsx");
const fixture = JSON.parse(readFileSync(new URL("./fixtures/console-workspace-telemetry/vm-node.json", import.meta.url)));
const { parseVMTelemetry, projectVMTelemetry } = await import("./console-vm-telemetry.ts");
const { parseTelemetryPresentation, projectWorkspaceTelemetryView } = await import("./console-workspace-telemetry.ts");
const parsed = body => parseVMTelemetry(body, { workspaceId: "ws", orgId: "org", projectId: "project", view: "1h" });
const render = (body = fixture, props = {}) => renderToStaticMarkup(createElement(ConsoleWorkspaceTelemetry, {
  viewModel: projectVMTelemetry(parsed(body)), selectedView: "1h", onViewChange() {}, ...props,
}));

test("VM widget renders utilization, unknown memory, reserved GB and partial shared costs", () => {
  const html = render();
  for (const value of ["VM resources", "63%", "Memory unavailable", "Own reservation", "2 cores", "4 GB",
    "Shared node", "Powered", "Persistent storage", "Idle storage", "Unallocated", "LLM", "partial",
    "18446744073709551615", "Boot 2", "Boot 3", "Boot 4", "gce-doha-reviewed-2026-10-30"]) assert.ok(html.includes(value), value);
  assert.ok(!html.includes("0.63 cores"));
  assert.ok(!html.includes("Pod"));
  assert.ok(!html.includes("persistent storage, and")); // GKE exclusion copy must not leak here.
  assert.equal((html.match(/data-vm-boot=/g) ?? []).length, 1); // one bounded, selectable boot at a time
  assert.ok(!html.includes("data-sparkline-path=")); // isolated boots never form a connecting line
});

test("all-disabled capabilities render nothing; no feed is mounted", () => {
  assert.equal(render(fixture, { available: false }), "");
  assert.equal(renderToStaticMarkup(createElement(ConsoleWorkspaceTelemetryContainer, {
    workspaceId: "ws", capabilities: null, ready: true, authEpoch: 1, epochRef: { current: 1 },
  })), "");
});

test("section gates independently suppress node usage, reservations and costs", () => {
  const cost = render(fixture, { showTelemetry: false, showAllocation: false });
  assert.ok(!cost.includes("63%") && !cost.includes("Own reservation"));
  assert.ok(cost.includes("Persistent storage"));
  const allocation = render(fixture, { showTelemetry: false, showCost: false });
  assert.ok(allocation.includes("Own reservation") && !allocation.includes("Persistent storage"));
  const usage = render(fixture, { showAllocation: false, showCost: false });
  assert.ok(usage.includes("63%") && !usage.includes("Own reservation") && !usage.includes("Workspace resource cost"));
  assert.equal(render(fixture, { showTelemetry: false, showAllocation: false, showCost: false }), "");
});

test("absent evidence is not free compute or a verified empty reservation", () => {
  const body = structuredClone(fixture);
  body.cpu_utilization_samples[2].value = "0";
  body.reservation = null;
  Object.assign(body.node_cost, { observed_powered_seconds: null, observed_idle_seconds: null,
    observed_storage_gib_seconds: null, observed_idle_storage_gib_seconds: null, coverage: "unknown" });
  for (const key of ["compute", "idle_compute", "persistent_storage", "idle_storage"]) {
    body.node_cost[key] = structuredClone(fixture.node_cost.idle_storage);
  }
  const html = render(body);
  assert.ok(html.includes("0%") && html.includes("Unknown") && html.includes("Unpriced"));
  assert.ok(html.includes("Reservation not recorded") && !html.includes("$0"));
  body.reservation = { observed_at: body.window_end_at, state: "none", quality: "ok", values: null };
  assert.ok(render(body).includes("No active reservation"));
});

test("VM memory uses binary byte units without inventing capacity or memory utilization", () => {
  const body = structuredClone(fixture);
  body.memory_bytes_samples = [{ ...body.cpu_utilization_samples[2],
    metric_type: "agent.googleapis.com/memory/bytes_used", value: "1073741824", unit: "bytes" }];
  const html = render(body);
  assert.ok(html.includes("1 GiB"));
  assert.ok(!html.includes("Memory utilization") && !html.includes("req "));
});

test("hidden telemetry stale does not qualify allocation-only; historical mode ignores age only", () => {
  const body = structuredClone(fixture);
  body.state = body.quality = "stale";
  assert.ok(!render(body, { showTelemetry: false, showCost: false }).includes('data-awf-stale="true"'));
  const viewModel = { ...projectVMTelemetry(parsed(fixture)), isStale: true };
  assert.ok(render(fixture, { viewModel }).includes('data-awf-stale="true"'));
  assert.ok(!render(fixture, { viewModel, mode: "historical" }).includes('data-awf-stale="true"'));
  assert.ok(render(body, { mode: "historical" }).includes('data-awf-stale="true"'));
});

for (const name of ["success", "partial", "stale", "unallocated", "cloud_shared_unallocated_no_target"]) {
  test(`existing GKE ${name} fixture still renders through the unchanged variant`, () => {
    const body = JSON.parse(readFileSync(new URL(`./fixtures/console-workspace-telemetry/${name}.json`, import.meta.url)));
    const parsed = parseTelemetryPresentation(body);
    assert.ok(parsed);
    const html = renderToStaticMarkup(createElement(ConsoleWorkspaceTelemetry, {
      viewModel: projectWorkspaceTelemetryView(parsed, { nowMs: Date.parse(body.window_end_at) }),
      selectedView: "6h", onViewChange() {},
    }));
    assert.ok(html.includes("Allocation estimate"));
    assert.ok(html.includes('data-awf-requested-view="6h"'));
    assert.ok(!html.includes('data-awf-resource-kind="vm_node"') && !html.includes("Physical history"));
  });
}

test("hidden node does not leak physical history or total org costs", () => {
  const body = structuredClone(fixture);
  Object.assign(body, { node: null, previous_node: null, historical_nodes: [], node_visibility: "not_authorized",
    observed_at: null, node_cost: null, cpu_utilization_samples: [], memory_bytes_samples: [] });
  const html = render(body);
  assert.ok(html.includes("Own reservation") && html.includes("Unallocated"));
  assert.ok(!html.includes("18446744073709551615") && !html.includes("63%") && !html.includes("$0.0157"));
});

test("known zero memory is distinct from missing; stale/error and requested view are visible", () => {
  const body = structuredClone(fixture);
  body.memory_bytes_samples = [{ ...body.cpu_utilization_samples[2], metric_type: "agent.googleapis.com/memory/bytes_used", value: "0", unit: "bytes" }];
  body.state = body.quality = "stale";
  const html = render(body, { requestError: "Telemetry unavailable", lastGoodAt: fixture.observed_at, selectedView: "6h" });
  assert.ok(html.includes("0 B") && !html.includes("Memory unavailable"));
  assert.ok(html.includes("Telemetry unavailable") && html.includes("Last good"));
  assert.ok(html.includes('data-awf-stale="true"'));
  assert.ok(html.includes('data-awf-requested-view="6h"') && html.includes('data-awf-displayed-view="1h"'));
});
