import assert from "node:assert/strict";
import test from "node:test";

import { omitUndefined } from "./api-payload.ts";

test("omitUndefined removes nested undefined values from JSON payloads", () => {
  assert.deepEqual(
    omitUndefined({
      keep: "value",
      drop: undefined,
      nested: {
        keep: 1,
        drop: undefined,
      },
      items: [
        undefined,
        {
          keep: true,
          drop: undefined,
        },
      ],
    }),
    {
      keep: "value",
      nested: {
        keep: 1,
      },
      items: [
        {
          keep: true,
        },
      ],
    },
  );
});
