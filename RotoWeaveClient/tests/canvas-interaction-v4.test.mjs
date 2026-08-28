import assert from "node:assert/strict";
import test from "node:test";

import { zoomCanvasViewportAtPoint } from "../app/lib/canvas-viewport.ts";
import { constrainDragPoint } from "../app/lib/drag-constraint.ts";

test("cursor-centered viewport zoom preserves the world point and clamps every canvas", () => {
  const viewport = { x: 20, y: -30, scale: 2 };
  const pointer = { x: 140, y: 90 };
  const worldBefore = {
    x: (pointer.x - viewport.x) / viewport.scale,
    y: (pointer.y - viewport.y) / viewport.scale,
  };
  const zoomed = zoomCanvasViewportAtPoint(viewport, pointer, 1.5);
  assert.deepEqual({
    x: (pointer.x - zoomed.x) / zoomed.scale,
    y: (pointer.y - zoomed.y) / zoomed.scale,
  }, worldBefore);
  assert.equal(zoomCanvasViewportAtPoint(viewport, pointer, 1e-9).scale, 0.02);
  assert.equal(zoomCanvasViewportAtPoint(viewport, pointer, 1e9).scale, 50);
});

test("Shift locks to the first larger drag axis until release", () => {
  const origin = { x: 10, y: 20 };
  const first = constrainDragPoint(origin, { x: 14, y: 29 }, true);
  assert.deepEqual(first, { point: { x: 10, y: 29 }, axis: "y" });
  const locked = constrainDragPoint(origin, { x: 40, y: 31 }, true, first.axis);
  assert.deepEqual(locked, { point: { x: 10, y: 31 }, axis: "y" });
  const released = constrainDragPoint(origin, { x: 40, y: 31 }, false, locked.axis);
  assert.deepEqual(released, { point: { x: 40, y: 31 }, axis: null });
  const relocked = constrainDragPoint(origin, { x: 45, y: 32 }, true, released.axis);
  assert.deepEqual(relocked, { point: { x: 45, y: 20 }, axis: "x" });
});
