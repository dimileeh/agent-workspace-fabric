import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { registerHooks } from "node:module";
import { fileURLToPath } from "node:url";
import test from "node:test";
import ts from "typescript";

// Execute the actual reader effect with controlled React state/timers and HTTP
// responses. This is a polling unit test, not a browser or React-render proof.
const root = new URL("../", import.meta.url);
const hookURL = new URL("hooks/use-workspace-telemetry.ts", root).href;
const stateKey = "awf.vm-telemetry.poll-test";
registerHooks({
  resolve(specifier, context, next) {
    if (specifier === "react" && context.parentURL === hookURL) return { url: "awf:poll-react", shortCircuit: true };
    const base = specifier.startsWith("@/") ? new URL(specifier.slice(2), root)
      : specifier.startsWith(".") && context.parentURL?.startsWith(root.href) ? new URL(specifier, context.parentURL) : null;
    if (base) for (const ext of ["", ".ts", ".tsx"]) {
      const candidate = new URL(base.href + ext);
      if (existsSync(candidate) && /\.tsx?$/.test(candidate.pathname)) return { url: candidate.href, shortCircuit: true };
    }
    return next(specifier, context);
  },
  load(url, context, next) {
    if (url === "awf:poll-react") return { format: "module", shortCircuit: true, source: `
      const harness = () => globalThis[Symbol.for(${JSON.stringify(stateKey)})];
      export function useState(initial) {
        const h = harness(); h.state = initial;
        return [initial, update => { h.state = typeof update === "function" ? update(h.state) : update; }];
      }
      export function useEffect(effect) { harness().cleanup = effect(); }
    ` };
    if (url.startsWith(root.href) && /\.tsx?$/.test(url)) return {
      format: "module", shortCircuit: true,
      source: ts.transpileModule(readFileSync(new URL(url), "utf8"), {
        fileName: fileURLToPath(url), compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX },
      }).outputText,
    };
    return next(url, context);
  },
});
// Only this harness substitutes React; the production hook keeps normal linting.
const { useWorkspaceTelemetry: exerciseReader } = await import(hookURL);
const { workspaceTelemetryPath } = await import("./console-urls.ts");
const fixture = JSON.parse(readFileSync(new URL("./fixtures/console-workspace-telemetry/vm-node.json", import.meta.url)));
const settle = () => new Promise(resolve => setImmediate(resolve));

async function withReader(run) {
  const savedWindow = globalThis.window, savedFetch = globalThis.fetch;
  const savedContext = process.env.NEXT_PUBLIC_AWF_CONSOLE_CONTEXT_QUERY_KEYS;
  const h = { state: null, cleanup: null }, timers = new Map();
  const replies = [Response.json(fixture)], requests = [];
  let sequence = 0;
  const epochRef = { current: 1 };
  globalThis[Symbol.for(stateKey)] = h;
  globalThis.window = {
    location: { origin: "https://console.example.test", search: "?org_id=org&project_id=project" },
    setTimeout(callback, delay) { const id = ++sequence; timers.set(id, { callback, delay }); return id; },
    clearTimeout(id) { timers.delete(id); },
  };
  globalThis.fetch = async (url, options) => {
    assert.equal(options.cache, "no-store");
    requests.push(url);
    assert.ok(replies.length, "unexpected extra HTTP request");
    return replies.shift();
  };
  process.env.NEXT_PUBLIC_AWF_CONSOLE_CONTEXT_QUERY_KEYS = "org_id,project_id";
  try {
    exerciseReader({ workspaceId: "ws", view: "1h", url: workspaceTelemetryPath("ws", "1h"), authEpoch: 1, epochRef });
    await settle();
    assert.equal(h.state.data.resource_kind, "vm_node");
    const good = h.state;
    const poll = async response => {
      replies.push(response);
      const scheduled = [...timers].find(([, timer]) => timer.delay === 60_000);
      assert.ok(scheduled, "poll was not scheduled");
      timers.delete(scheduled[0]);
      scheduled[1].callback();
      await settle();
    };
    await run({ h, good, poll, timers, requests, epochRef });
  } finally {
    h.cleanup?.();
    globalThis.window = savedWindow;
    globalThis.fetch = savedFetch;
    delete globalThis[Symbol.for(stateKey)];
    if (savedContext === undefined) delete process.env.NEXT_PUBLIC_AWF_CONSOLE_CONTEXT_QUERY_KEYS;
    else process.env.NEXT_PUBLIC_AWF_CONSOLE_CONTEXT_QUERY_KEYS = savedContext;
  }
}

for (const [name, mutate] of Object.entries({
  "unknown field": body => { body.future_metric = "unsupported"; },
  "malformed metric": body => { body.cpu_utilization_samples[0].value = "invalid"; },
})) test(`same-identity ${name} retains last-good with an error`, async () => {
  await withReader(async ({ h, good, poll }) => {
    const body = structuredClone(fixture); mutate(body);
    await poll(Response.json(body));
    assert.deepEqual(h.state.data, good.data);
    assert.equal(h.state.lastGoodAt, good.lastGoodAt);
    assert.match(h.state.error, /Malformed/);
  });
});

for (const [name, mutate] of Object.entries({
  tenant: body => { body.workspace.org_id = "foreign"; },
  project: body => { body.workspace.project_id = "foreign"; },
  workspace: body => { body.workspace.workspace_record_id = "foreign"; },
  owner: body => { body.workspace.owner_record_id = "new-owner"; },
  authorization: body => { body.workspace.authorization_generation++; },
  boot: body => { body.node.boot_generation++; },
  physical: body => { body.node.provider_id = "103"; },
  visibility: body => { body.node_visibility = "not_authorized"; body.node = null; },
  view: body => { body.view = "6h"; },
  binding: body => { delete body.workspace; },
})) test(`malformed poll with changed ${name} cannot retain old data`, async () => {
  await withReader(async ({ h, poll }) => {
    const body = structuredClone(fixture); mutate(body); body.future_metric = true;
    await poll(Response.json(body));
    assert.equal(h.state.data, null);
    assert.equal(h.state.lastGoodAt, null);
    assert.ok(h.state.error);
  });
});

test("reordered identity fields retain last-good, then valid polling recovers", async () => {
  await withReader(async ({ h, good, poll }) => {
    const body = structuredClone(fixture);
    body.workspace = Object.fromEntries(Object.entries(body.workspace).reverse());
    body.node = Object.fromEntries(Object.entries(body.node).reverse());
    body.future_metric = true;
    await poll(Response.json(body));
    assert.deepEqual(h.state.data, good.data);
    assert.equal(h.state.lastGoodAt, good.lastGoodAt);
    assert.ok(h.state.error);
    await poll(Response.json(fixture));
    assert.equal(h.state.error, null);
    assert.deepEqual(h.state.data, good.data);
  });
});

for (const status of [401, 403]) test(`${status} clears and stops polling`, async () => {
  await withReader(async ({ h, poll, timers }) => {
    await poll(new Response("denied", { status }));
    assert.equal(h.state.data, null);
    assert.equal(h.state.lastGoodAt, null);
    assert.equal(timers.size, 0);
  });
});

test("transport failure preserves the already-authorized snapshot", async () => {
  await withReader(async ({ h, good, poll }) => {
    await poll(new Response("upstream unavailable", { status: 503 }));
    assert.deepEqual(h.state.data, good.data);
    assert.equal(h.state.lastGoodAt, good.lastGoodAt);
    assert.match(h.state.error, /upstream unavailable/);
  });
});

test("changed auth epoch stops the previous reader before another request", async () => {
  await withReader(async ({ good, h, poll, epochRef, requests, timers }) => {
    epochRef.current++;
    await poll(Response.json(fixture));
    assert.equal(requests.length, 1);
    assert.deepEqual(h.state, good);
    assert.equal(timers.size, 0);
  });
});
