import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  CANVAS_GUIDE_COLORS,
  CANVAS_GUIDE_DASHES,
  CANVAS_GUIDE_WIDTHS,
} from "../app/lib/canvas-guide-style.ts";

test("canvas guides use distinct high-contrast colors and a dark halo", () => {
  assert.deepEqual(CANVAS_GUIDE_COLORS, {
    halo: "#101418",
    size: "#00e6ad",
    center: "#d946ef",
    horizon: "#009dff",
    shadowY: "#ff6b00",
    label: "#ffffff",
  });
  assert.ok(CANVAS_GUIDE_WIDTHS.halo > CANVAS_GUIDE_WIDTHS.normal);
  assert.ok(CANVAS_GUIDE_WIDTHS.active > CANVAS_GUIDE_WIDTHS.normal);
  assert.notDeepEqual(CANVAS_GUIDE_DASHES.center, CANVAS_GUIDE_DASHES.size);
  assert.notDeepEqual(CANVAS_GUIDE_DASHES.horizon, CANVAS_GUIDE_DASHES.shadowY);
});

test("global and action canvases share the current guide system", async () => {
  for (const path of [
    "../app/components/global-settings-canvas.tsx",
    "../app/components/action-canvas-v4.tsx",
  ]) {
    const source = await readFile(new URL(path, import.meta.url), "utf8");
    assert.match(source, /CANVAS_GUIDE_COLORS\.halo/);
    assert.match(source, /CANVAS_GUIDE_COLORS\.size/);
    assert.match(source, /CANVAS_GUIDE_COLORS\.center/);
    assert.match(source, /CANVAS_GUIDE_COLORS\.horizon/);
    assert.match(source, /CANVAS_GUIDE_COLORS\.shadowY/);
  }
});

test("center guide is session-only, visible by default, and independent from the size box", async () => {
  const globalFeature = await readFile(new URL("../app/features/global-settings-v4.tsx", import.meta.url), "utf8");
  const actionCanvas = await readFile(new URL("../app/components/action-canvas-v4.tsx", import.meta.url), "utf8");
  for (const source of [globalFeature, actionCanvas]) {
    assert.match(source, /center: true/);
    assert.match(source, />中轴线<\/button>/);
  }
  assert.match(actionCanvas, /guides\.center \? <><Line points=\{\[calibration\.sizeGuideCenterX/);
  assert.match(actionCanvas, /text=\{`中轴 X \$\{calibration\.sizeGuideCenterX\}`\}/);
  assert.match(globalFeature, /guideVisibility\.center/);
});
