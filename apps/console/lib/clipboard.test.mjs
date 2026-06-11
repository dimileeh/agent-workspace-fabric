import assert from "node:assert/strict";
import test from "node:test";

import { copyTextToClipboard } from "./clipboard.ts";

// Swap a property on globalThis (navigator/document) for the duration of a test,
// returning a restore function. `document` does not exist in Node by default, so
// the restore deletes it; `navigator` does, so we restore the prior descriptor.
function setGlobal(name, value) {
  const prev = Object.getOwnPropertyDescriptor(globalThis, name);
  Object.defineProperty(globalThis, name, { configurable: true, writable: true, value });
  return () => {
    if (prev) {
      Object.defineProperty(globalThis, name, prev);
    } else {
      delete globalThis[name];
    }
  };
}

function makeFakeDocument({ execResult = true, throwOnExec = false, withSelection = false } = {}) {
  const appended = [];
  const removed = [];
  const execCalls = [];
  const restoredRanges = [];
  const focusOptions = [];
  const textarea = {
    value: "",
    style: {},
    setAttribute() {},
    focus(options) {
      focusOptions.push(options);
    },
    select() {},
    remove() {
      removed.push(this);
    },
  };
  const range = { id: "prev-range" };
  const selection = withSelection
    ? {
        rangeCount: 1,
        getRangeAt: () => range,
        removeAllRanges() {},
        addRange: (r) => restoredRanges.push(r),
      }
    : { rangeCount: 0, getRangeAt: () => null, removeAllRanges() {}, addRange() {} };
  const doc = {
    createElement: () => textarea,
    body: { appendChild: (el) => appended.push(el) },
    getSelection: () => selection,
    execCommand(cmd) {
      execCalls.push(cmd);
      if (throwOnExec) throw new Error("execCommand unavailable");
      return execResult;
    },
  };
  return { doc, textarea, appended, removed, execCalls, restoredRanges, range, focusOptions };
}

test("uses navigator.clipboard.writeText in a secure context", async () => {
  let written = null;
  const restoreNav = setGlobal("navigator", {
    clipboard: { writeText: async (t) => { written = t; } },
  });
  const restoreDoc = setGlobal("document", undefined);
  try {
    assert.equal(await copyTextToClipboard("ws_secure"), true);
    assert.equal(written, "ws_secure");
  } finally {
    restoreDoc();
    restoreNav();
  }
});

test("falls back to execCommand when navigator.clipboard is missing (HTTP / Tailscale)", async () => {
  const restoreNav = setGlobal("navigator", {});
  const { doc, textarea, appended, removed, execCalls } = makeFakeDocument({ execResult: true });
  const restoreDoc = setGlobal("document", doc);
  try {
    assert.equal(await copyTextToClipboard("ws_http"), true);
    assert.equal(textarea.value, "ws_http");
    assert.deepEqual(execCalls, ["copy"]);
    assert.equal(appended.length, 1);
    assert.equal(removed.length, 1, "textarea is cleaned up");
  } finally {
    restoreDoc();
    restoreNav();
  }
});

test("falls back to execCommand when navigator.clipboard.writeText rejects", async () => {
  const restoreNav = setGlobal("navigator", {
    clipboard: { writeText: async () => { throw new Error("blocked"); } },
  });
  const { doc, execCalls } = makeFakeDocument({ execResult: true });
  const restoreDoc = setGlobal("document", doc);
  try {
    assert.equal(await copyTextToClipboard("ws_reject"), true);
    assert.deepEqual(execCalls, ["copy"]);
  } finally {
    restoreDoc();
    restoreNav();
  }
});

test("focuses the textarea with preventScroll so the page does not jump", async () => {
  const restoreNav = setGlobal("navigator", {});
  const { doc, focusOptions } = makeFakeDocument({ execResult: true });
  const restoreDoc = setGlobal("document", doc);
  try {
    await copyTextToClipboard("ws_focus");
    assert.deepEqual(focusOptions, [{ preventScroll: true }]);
  } finally {
    restoreDoc();
    restoreNav();
  }
});

test("restores the prior text selection after the legacy copy", async () => {
  const restoreNav = setGlobal("navigator", {});
  const { doc, restoredRanges, range } = makeFakeDocument({ execResult: true, withSelection: true });
  const restoreDoc = setGlobal("document", doc);
  try {
    await copyTextToClipboard("ws_sel");
    assert.deepEqual(restoredRanges, [range], "the displaced selection is re-added");
  } finally {
    restoreDoc();
    restoreNav();
  }
});

test("returns false when execCommand reports failure but still cleans up", async () => {
  const restoreNav = setGlobal("navigator", {});
  const { doc, removed } = makeFakeDocument({ execResult: false });
  const restoreDoc = setGlobal("document", doc);
  try {
    assert.equal(await copyTextToClipboard("ws_fail"), false);
    assert.equal(removed.length, 1);
  } finally {
    restoreDoc();
    restoreNav();
  }
});

test("returns false when execCommand throws", async () => {
  const restoreNav = setGlobal("navigator", {});
  const { doc } = makeFakeDocument({ throwOnExec: true });
  const restoreDoc = setGlobal("document", doc);
  try {
    assert.equal(await copyTextToClipboard("ws_throw"), false);
  } finally {
    restoreDoc();
    restoreNav();
  }
});

test("returns false when document.body is not yet available (early load / SSR)", async () => {
  const restoreNav = setGlobal("navigator", {});
  const { doc } = makeFakeDocument({ execResult: true });
  doc.body = null;
  const restoreDoc = setGlobal("document", doc);
  try {
    assert.equal(await copyTextToClipboard("ws_no_body"), false);
  } finally {
    restoreDoc();
    restoreNav();
  }
});

test("returns false when neither the clipboard API nor document is available", async () => {
  const restoreNav = setGlobal("navigator", {});
  const restoreDoc = setGlobal("document", undefined);
  try {
    assert.equal(await copyTextToClipboard("ws_none"), false);
  } finally {
    restoreDoc();
    restoreNav();
  }
});
