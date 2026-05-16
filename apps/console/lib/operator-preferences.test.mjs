import assert from "node:assert/strict";
import test from "node:test";

import {
  DEFAULT_OPERATOR_PREFERENCES,
  OPERATOR_PREFERENCES_STORAGE_KEY,
  decodeOperatorPreferences,
  encodeOperatorPreferences,
  normalizeOperatorPreferences,
  operatorPreferenceAttributes,
  resolveOperatorTheme,
} from "./operator-preferences.ts";

test("decodeOperatorPreferences defaults missing and invalid persisted payloads", () => {
  assert.deepEqual(decodeOperatorPreferences(null), DEFAULT_OPERATOR_PREFERENCES);
  assert.deepEqual(decodeOperatorPreferences(""), DEFAULT_OPERATOR_PREFERENCES);
  assert.deepEqual(decodeOperatorPreferences("not json"), DEFAULT_OPERATOR_PREFERENCES);
  assert.deepEqual(
    decodeOperatorPreferences(
      JSON.stringify({
        version: 1,
        theme: "sepia",
        contrast: "more",
        fontSize: "huge",
      }),
    ),
    DEFAULT_OPERATOR_PREFERENCES,
  );
});

test("operator preference storage key is versioned", () => {
  assert.equal(OPERATOR_PREFERENCES_STORAGE_KEY, "awf.operator.preferences.v1");
});

test("normalizeOperatorPreferences derives invalid field fallbacks from exported defaults", () => {
  const originalDefaults = { ...DEFAULT_OPERATOR_PREFERENCES };

  try {
    DEFAULT_OPERATOR_PREFERENCES.theme = "dark";
    DEFAULT_OPERATOR_PREFERENCES.contrast = "high";
    DEFAULT_OPERATOR_PREFERENCES.fontSize = "large";

    assert.deepEqual(
      normalizeOperatorPreferences({
        theme: "sepia",
        contrast: "more",
        fontSize: "huge",
      }),
      DEFAULT_OPERATOR_PREFERENCES,
    );
  } finally {
    Object.assign(DEFAULT_OPERATOR_PREFERENCES, originalDefaults);
  }
});

test("operator preferences round-trip through stable serialization", () => {
  const preferences = {
    theme: "system",
    contrast: "high",
    fontSize: "large",
  };

  const encoded = encodeOperatorPreferences(preferences);

  assert.deepEqual(JSON.parse(encoded), {
    version: 1,
    theme: "system",
    contrast: "high",
    fontSize: "large",
  });
  assert.deepEqual(decodeOperatorPreferences(encoded), preferences);
});

test("operator preference attributes map to stable html data attributes", () => {
  assert.deepEqual(
    operatorPreferenceAttributes({
      theme: "system",
      contrast: "high",
      fontSize: "large",
    }, "dark"),
    {
      "data-awf-theme": "dark",
      "data-awf-theme-mode": "system",
      "data-awf-contrast": "high",
      "data-awf-font-size": "large",
    },
  );
});

test("resolveOperatorTheme follows system theme only in system mode", () => {
  assert.equal(resolveOperatorTheme({ ...DEFAULT_OPERATOR_PREFERENCES, theme: "system" }, "dark"), "dark");
  assert.equal(resolveOperatorTheme({ ...DEFAULT_OPERATOR_PREFERENCES, theme: "system" }, "light"), "light");
  assert.equal(resolveOperatorTheme({ ...DEFAULT_OPERATOR_PREFERENCES, theme: "dark" }, "light"), "dark");
});

test("normalizeOperatorPreferences preserves valid partial fields and defaults invalid fields", () => {
  assert.deepEqual(
    normalizeOperatorPreferences({
      theme: "dark",
      contrast: "invalid",
    }),
    {
      theme: "dark",
      contrast: DEFAULT_OPERATOR_PREFERENCES.contrast,
      fontSize: DEFAULT_OPERATOR_PREFERENCES.fontSize,
    },
  );
});
