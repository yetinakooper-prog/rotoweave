import assert from "node:assert/strict";
import test from "node:test";

import { fromPercentDisplay, toPercentDisplay } from "../app/lib/percent-input-v4.ts";

test("V4 percent input maps presentation to unchanged ratio values", () => {
  assert.equal(fromPercentDisplay(100), 1);
  assert.equal(fromPercentDisplay(0.5), 0.005);
  assert.equal(toPercentDisplay(1), 100);
  assert.equal(toPercentDisplay(0.005), 0.5);
  assert.equal(fromPercentDisplay(toPercentDisplay(1.234567)), 1.234567);
});
