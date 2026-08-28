import assert from "node:assert/strict";
import test from "node:test";

import {
  coreReferenceOriginFromRender,
  coreReferenceRenderRect,
  resolveCoreReferenceScale,
} from "../app/lib/core-reference-scale.mjs";


test("core reference supports 0.5%-800% rendering and drag coordinates", () => {
  for (const scale of [0.005, 0.01, 0.05, 0.1, 1, 8]) {
    const rect = coreReferenceRenderRect({
      width: 320,
      height: 480,
      originX: -160,
      originY: -480,
      scale,
    });
    assert.equal(rect.scale, scale);
    assert.equal(rect.width, 320 * scale);
    assert.equal(rect.height, 480 * scale);
    assert.equal(rect.x, -160 * scale);
    assert.equal(rect.y, -480 * scale);
    assert.deepEqual(
      coreReferenceOriginFromRender(rect.x, rect.y, scale),
      { x: -160, y: -480 },
    );
  }
});

test("core reference renderer clamps only outside the 0.5%-800% contract", () => {
  assert.equal(resolveCoreReferenceScale(0), 0.005);
  assert.equal(resolveCoreReferenceScale(0.004), 0.005);
  assert.equal(resolveCoreReferenceScale(8.001), 8);
  assert.equal(resolveCoreReferenceScale(Number.NaN), 1);
});
