import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { afterEach, test } from "node:test";

import { subscribeToHistoryNavigation } from "./history-navigation.ts";

const cleanups = [];

afterEach(() => {
  while (cleanups.length > 0) {
    cleanups.pop()();
  }
  delete globalThis.window;
});

function installWindow() {
  const calls = [];
  const popstate = new Set();
  const history = {
    pushState(data, unused, url) {
      calls.push(["push", data, unused, url]);
    },
    replaceState(data, unused, url) {
      calls.push(["replace", data, unused, url]);
    },
  };
  const window = {
    history,
    addEventListener(type, listener) {
      if (type === "popstate") {
        popstate.add(listener);
      }
    },
    removeEventListener(type, listener) {
      if (type === "popstate") {
        popstate.delete(listener);
      }
    },
  };
  globalThis.window = window;
  return {
    calls,
    history,
    dispatchPopstate() {
      for (const listener of [...popstate]) {
        listener();
      }
    },
  };
}

function track(unsubscribe) {
  cleanups.push(unsubscribe);
  return unsubscribe;
}

test("one history patch notifies every subscriber until the last unsubscribe", () => {
  const host = installWindow();
  const seen = { prompt: 0, dashboard: 0 };
  const patchedAfterFirst = (() => {
    track(subscribeToHistoryNavigation(() => {
      seen.prompt += 1;
    }));
    return host.history.pushState;
  })();

  track(subscribeToHistoryNavigation(() => {
    seen.dashboard += 1;
  }));
  assert.equal(host.history.pushState, patchedAfterFirst);
  assert.equal(host.history.replaceState === patchedAfterFirst, false);

  const url = new URL("https://example.test/path?org_id=1");
  host.history.pushState({ step: 1 }, "", url);
  host.history.replaceState(null, "", null);
  assert.deepEqual(host.calls, [
    ["push", { step: 1 }, "", url],
    ["replace", null, "", null],
  ]);
  assert.deepEqual(seen, { prompt: 2, dashboard: 2 });

  cleanups.shift()();
  host.history.pushState({}, "", "/still-open");
  assert.equal(seen.prompt, 2);
  assert.equal(seen.dashboard, 3);
  assert.equal(host.history.pushState, patchedAfterFirst);

  cleanups.shift()();
  host.history.replaceState({}, "", "/closed");
  assert.equal(seen.dashboard, 3);
  assert.equal(host.calls.length, 4);
  assert.notEqual(host.history.pushState, patchedAfterFirst);
});

test("resubscribing one listener does not drop the listener that stayed mounted", () => {
  const host = installWindow();
  const seen = { prompt: 0, dashboard: 0, nextDashboard: 0 };
  track(subscribeToHistoryNavigation(() => {
    seen.prompt += 1;
  }));
  const stopDashboard = track(subscribeToHistoryNavigation(() => {
    seen.dashboard += 1;
  }));

  stopDashboard();
  track(subscribeToHistoryNavigation(() => {
    seen.nextDashboard += 1;
  }));
  host.history.pushState({}, "", "/?project_id=next");

  assert.deepEqual(seen, { prompt: 1, dashboard: 0, nextDashboard: 1 });
});

test("popstate reaches current subscribers only", () => {
  const host = installWindow();
  let prompt = 0;
  let dashboard = 0;
  const stopPrompt = track(subscribeToHistoryNavigation(() => {
    prompt += 1;
  }));
  track(subscribeToHistoryNavigation(() => {
    dashboard += 1;
  }));

  host.dispatchPopstate();
  assert.deepEqual([prompt, dashboard], [1, 1]);
  stopPrompt();
  host.dispatchPopstate();
  assert.deepEqual([prompt, dashboard], [1, 2]);
});

test("subscribe and repeated unsubscribe do nothing without window", () => {
  delete globalThis.window;
  const stop = subscribeToHistoryNavigation(() => {
    throw new Error("listener must not run");
  });
  stop();
  stop();
});

test("dashboard and task-details prompt share one history subscription", () => {
  const dashboard = readFileSync(new URL("../components/console-dashboard.tsx", import.meta.url), "utf8");
  const prompt = readFileSync(new URL("../hooks/use-task-details-prompt.ts", import.meta.url), "utf8");
  for (const source of [dashboard, prompt]) {
    assert.match(source, /subscribeToHistoryNavigation/);
    assert.doesNotMatch(source, /history\.pushState\s*=/);
    assert.doesNotMatch(source, /history\.replaceState\s*=/);
  }
});
