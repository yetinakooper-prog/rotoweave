import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";

import { fitActionCanvasViewport, rotatedFrameBounds } from "../app/lib/action-canvas-viewport.ts";

test("action canvas fit centers the complete visible bounds with padding", () => {
  const fitted = fitActionCanvasViewport({ width: 1000, height: 700 }, { left: -250, top: -500, right: 250, bottom: 0 });
  assert.ok(fitted.scale > 1);
  assert.equal(fitted.x, 500);
  assert.equal(fitted.y, 350 - (-250 * fitted.scale));
  const bounds = rotatedFrameBounds(100, 200, { x: 10, y: 20 }, { x: 1, y: 1 }, 90);
  assert.ok(bounds.left < bounds.right && bounds.top < bounds.bottom);
});

test("frame drag-end cannot bubble into the Stage viewport commit", () => {
  const source = readFileSync(new URL("../app/components/action-canvas-v4.tsx", import.meta.url), "utf8");
  assert.match(source, /event\.target !== event\.target\.getStage\(\)/);
  assert.match(source, />适配<\/button>/);
  assert.match(source, /useState\(\{ size: true, center: true, horizon: true, shadow: true \}\)/);
  assert.match(source, /aria-label="动作辅助线显示"/);
  assert.match(source, /aria-pressed=\{guides\.size\}/);
  assert.match(source, /aria-pressed=\{guides\.center\}/);
  assert.match(source, /useState\(\{ core: true, previous: false, next: false \}\)/);
  assert.match(source, /aria-label="动作参考图层显示"/);
  assert.match(source, /aria-pressed=\{references\.core\}[\s\S]*>核心形象<\/button>/);
  assert.match(source, /aria-pressed=\{references\.previous\}[\s\S]*>前帧洋葱<\/button>/);
  assert.match(source, /aria-pressed=\{references\.next\}[\s\S]*>后帧洋葱<\/button>/);
  assert.match(source, /references\.core && coreImage && coreRect/);
  assert.match(source, /references\.previous && previousFrame && previousOnionImage/);
  assert.match(source, /references\.next && nextFrame && nextOnionImage/);
  assert.match(source, /<Layer listening=\{false\}>[\s\S]*opacity=\{0\.34\}[\s\S]*opacity=\{0\.3\}/);
  assert.match(source, /v4-canvas-toolbar action-canvas-v4-toolbar/);
  assert.match(source, /useCanvasSpacePan\(true\)/);
  assert.match(source, /onMouseEnter=\{navigation\.onCanvasEnter\}/);
  assert.match(source, /onMouseLeave=\{navigation\.onCanvasLeave\}/);
  assert.match(source, /zoomCanvasViewportAtPoint\(current, pointer, factor\)/);
  assert.doesNotMatch(source, /onScale/);
  assert.match(source, /listening=\{!navigation\.spacePressed && mode === "frame"\} draggable=\{!navigation\.spacePressed && mode === "frame"\}/);
  assert.match(source, /listening=\{!navigation\.spacePressed && mode === "shadow-x"\} draggable=\{!navigation\.spacePressed && mode === "shadow-x"\}/);
  assert.match(source, /event\.target\.y\(origin\.y\)/);
  assert.match(source, /constrainDragPoint\(drag\.origin/);
});

test("global settings reuses action guide toggles without persisting visibility", () => {
  const settings = readFileSync(new URL("../app/features/global-settings-v4.tsx", import.meta.url), "utf8");
  const canvas = readFileSync(new URL("../app/components/global-settings-canvas.tsx", import.meta.url), "utf8");
  assert.match(settings, /useState\(\{ size: true, center: true, horizon: true, shadow: true \}\)/);
  assert.match(settings, /className="action-guide-toggles"/);
  assert.match(settings, /aria-label="全局辅助线显示"/);
  assert.match(settings, /guideVisibility=\{guideVisibility\}/);
  assert.match(settings, /if \(!visible && dragMode === guide\) setDragMode\("viewport"\)/);
  assert.doesNotMatch(settings, /预设在左侧“设置”中维护/);
  assert.match(canvas, /guideVisibility\.size && sizeProfile/);
  assert.match(canvas, /guideVisibility\.center \? \(/);
  assert.match(canvas, /guideVisibility\.horizon \? <Group/);
  assert.match(canvas, /guideVisibility\.shadow && shadowStandardY !== null/);
});
