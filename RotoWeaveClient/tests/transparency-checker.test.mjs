import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const stylesheetUrl = new URL("../app/globals.css", import.meta.url);

test("all transparency checkerboards share the Photoshop reference palette", async () => {
  const css = await readFile(stylesheetUrl, "utf8");

  assert.match(css, /--transparency-checker-light:\s*#ffffff;/i);
  assert.match(css, /--transparency-checker-dark:\s*#cccccc;/i);

  for (const selector of [
    ".canvas-background.checker,",
    ".background-picker > button.checker {",
    ".action-editor-canvas {",
    ".action-canvas-v4 {",
    ".global-settings-canvas {",
    ".export-atlas-preview-v4 img {",
    ".unity-scene-preview {",
    ".alignment-canvas { position:",
    ".calibration-canvas-viewport {",
    ".live-preview-stage {",
  ]) {
    const selectorIndex = css.indexOf(selector);
    assert.notEqual(selectorIndex, -1, selector);

    const ruleEnd = css.indexOf("}", selectorIndex);
    const rule = css.slice(selectorIndex, ruleEnd);
    assert.match(rule, /var\(--transparency-checker-(?:image|light)\)/, selector);
  }

  for (const retiredColor of ["#252c28", "#303934", "#d8ddd9", "#7f8b85", "#25282a", "#151719", "#c2cec6"]) {
    assert.equal(css.includes(retiredColor), false, retiredColor);
  }
});
