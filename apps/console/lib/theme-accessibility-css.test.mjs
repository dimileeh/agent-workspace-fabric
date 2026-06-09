import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const globalsCss = readFileSync(new URL("../app/globals.css", import.meta.url), "utf8");

test("root font size preferences stay relative to the browser default", () => {
  assert.equal(fontSizeFor(":root"), "100%");
  assert.equal(fontSizeFor(':root[data-awf-font-size="large"]'), "112.5%");
});

function fontSizeFor(selector) {
  const escapedSelector = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = globalsCss.match(new RegExp(`${escapedSelector}\\s*{([^}]*)}`));
  assert.ok(match, `Expected ${selector} block to exist`);

  const fontSize = match[1].match(/font-size:\s*([^;]+);/);
  assert.ok(fontSize, `Expected ${selector} to declare font-size`);
  assert.doesNotMatch(fontSize[1], /\d+(?:\.\d+)?px/);
  return fontSize[1].trim();
}
