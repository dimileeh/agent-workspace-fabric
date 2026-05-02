import assert from "node:assert/strict";
import test from "node:test";

import {
  DEFAULT_OPERATOR_PREFERENCES,
  OPERATOR_PREFERENCES_STORAGE_KEY,
  decodeOperatorPreferences,
  encodeOperatorPreferences,
  normalizeOperatorPreferences,
  operatorPreferenceAttributes,
} from "./operator-preferences.ts";

test("decodeOperatorPreferences defaults missing and invalid persisted payloads", () => {
  assert.deepEqual(decodeOperatorPreferences(null), DEFAULT_OPERATOR_PREFERENCES);
  assert.deepEqual(decodeOperatorPreferences(""), DEFAULT_OPERATOR_PREFERENCES);
  assert.deepEqual(decodeOperatorPreferences("not json"), DEFAULT_OPERATOR_PREFERENCES);
  assert.deepEqual(
    decodeOperatorPreferences(
      JSON.stringify({
        version: 1,
        theme: "system",
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

test("operator preferences round-trip through stable serialization", () => {
  const preferences = {
    theme: "dark",
    contrast: "high",
    fontSize: "large",
  };

  const encoded = encodeOperatorPreferences(preferences);

  assert.deepEqual(JSON.parse(encoded), {
    version: 1,
    theme: "dark",
    contrast: "high",
    fontSize: "large",
  });
  assert.deepEqual(decodeOperatorPreferences(encoded), preferences);
});

test("operator preference attributes map to stable html data attributes", () => {
  assert.deepEqual(
    operatorPreferenceAttributes({
      theme: "dark",
      contrast: "high",
      fontSize: "large",
    }),
    {
      "data-awf-theme": "dark",
      "data-awf-contrast": "high",
      "data-awf-font-size": "large",
    },
  );
});

test("normalizeOperatorPreferences preserves valid partial fields and defaults invalid fields", () => {
  assert.deepEqual(
    normalizeOperatorPreferences({
      theme: "dark",
      contrast: "invalid",
    }),
    {
      theme: "dark",
      contrast: "normal",
      fontSize: "standard",
    },
  );
});
