import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import test from "node:test";

import { mergeDomainActionV4, mergeDomainCharacterV4 } from "../app/lib/domain-cache-v4.ts";
import { TIMELINE_EDGE_MAX_SPEED_PX_PER_SECOND, timelineEdgeVelocityV4 } from "../app/lib/timeline-edge-autoscroll-v4.ts";

test("timeline edge velocity is directional, proportional and stops at limits", () => {
  const base = { left: 100, width: 200, scrollWidth: 1000 };
  assert.equal(timelineEdgeVelocityV4({ ...base, pointerX: 100, scrollLeft: 200 }), -TIMELINE_EDGE_MAX_SPEED_PX_PER_SECOND);
  assert.equal(timelineEdgeVelocityV4({ ...base, pointerX: 300, scrollLeft: 200 }), TIMELINE_EDGE_MAX_SPEED_PX_PER_SECOND);
  assert.equal(timelineEdgeVelocityV4({ ...base, pointerX: 150, scrollLeft: 200 }), 0);
  assert.equal(timelineEdgeVelocityV4({ ...base, pointerX: 100, scrollLeft: 0 }), 0);
  assert.equal(timelineEdgeVelocityV4({ ...base, pointerX: 300, scrollLeft: 800 }), 0);
  assert.equal(timelineEdgeVelocityV4({ ...base, pointerX: 125, scrollLeft: 200 }), -90);
});

test("canonical action and character responses update only their cached entity and revision", () => {
  let cache = {
    revisionId: "rev-old",
    characters: [{ id: "char-1", name: "before" }],
    actions: [{ id: "act-1", name: "before" }],
    materialSources: [],
    materialVariants: [],
  };
  const queryClient = {
    setQueryData(_key, update) { cache = update(cache); },
  };
  assert.equal(mergeDomainActionV4(queryClient, { id: "act-1", name: "after" }, "rev-action"), true);
  assert.equal(cache.actions[0].name, "after");
  assert.equal(cache.revisionId, "rev-action");
  assert.equal(mergeDomainCharacterV4(queryClient, { id: "char-1", name: "after" }, "rev-character"), true);
  assert.equal(cache.characters[0].name, "after");
  assert.equal(cache.revisionId, "rev-character");
  assert.equal(mergeDomainActionV4(queryClient, { id: "missing" }, "rev-missing"), false);
  assert.equal(cache.revisionId, "rev-character");
});

test("both canvases fit once per open key and timeline owns one RAF loop", async () => {
  const actionCanvas = await readFile(resolve(import.meta.dirname, "../app/components/action-canvas-v4.tsx"), "utf8");
  const globalCanvas = await readFile(resolve(import.meta.dirname, "../app/components/global-settings-canvas.tsx"), "utf8");
  const editor = await readFile(resolve(import.meta.dirname, "../app/features/action-editor-v4.tsx"), "utf8");
  assert.match(actionCanvas, /fittedOpenKey\.current === openKey/);
  assert.match(actionCanvas, /frame && \(!imageWidth \|\| !imageHeight\)/);
  assert.match(globalCanvas, /fittedOpenKey\.current === sessionKey/);
  assert.match(editor, /timelineAutoScrollFrame\.current === null/);
  assert.match(editor, /setTimelineInsertionBoundary\(timelineBoundaryAtClientX\(track, pointerX\)\)/);
  assert.match(editor, /stopTimelineAutoScroll\(\)/);
});
