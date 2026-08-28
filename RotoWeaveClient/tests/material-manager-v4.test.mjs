import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import test from "node:test";

import {
  hexToRgb,
  projectLatestMaterialFrames,
  projectMaterialFrameDisplay,
  projectMaterialFrameIndexes,
  selectedFrameSequence,
  selectMaterialFrame,
} from "../app/lib/material-manager-v4.ts";

test("material selection follows Windows replace, Ctrl toggle, and Shift range rules", () => {
  let state = selectMaterialFrame(new Set(), 2, null, { shift: false, additive: false });
  assert.deepEqual([...state.selected], [2]);
  state = selectMaterialFrame(state.selected, 5, state.anchor, { shift: false, additive: true });
  assert.deepEqual(selectedFrameSequence(state.selected), [2, 5]);
  state = selectMaterialFrame(state.selected, 7, state.anchor, { shift: true, additive: false });
  assert.deepEqual(selectedFrameSequence(state.selected), [5, 6, 7]);
  state = selectMaterialFrame(state.selected, 6, state.anchor, { shift: false, additive: true });
  assert.deepEqual(selectedFrameSequence(state.selected), [5, 7]);
});

test("material color samples preserve the selected sRGB value", () => {
  assert.deepEqual(hexToRgb("#12aBef"), [18, 171, 239]);
  assert.deepEqual(hexToRgb("invalid"), [0, 255, 0]);
});

test("latest batch projection keeps only its explicit source-frame mapping", () => {
  assert.deepEqual(
    projectMaterialFrameIndexes(["source-0", "source-1", "source-2"], ["source-2", "source-0"]),
    [
      { sourceIndex: 2, variantIndex: 0 },
      { sourceIndex: 0, variantIndex: 1 },
    ],
  );
  assert.deepEqual(projectMaterialFrameIndexes(["source-0", "source-1"]), [
    { sourceIndex: 0, variantIndex: -1 },
    { sourceIndex: 1, variantIndex: -1 },
  ]);
});

test("cumulative projection replaces only frames covered by a later batch", () => {
  const variants = [
    {
      id: "variant-a",
      frames: [
        { sourceFrameId: "source-0" },
        { sourceFrameId: "source-2" },
      ],
    },
    {
      id: "variant-b",
      frames: [{ sourceFrameId: "source-1" }],
    },
    {
      id: "variant-c",
      frames: [
        { sourceFrameId: "source-2" },
        { sourceFrameId: "unknown" },
      ],
    },
  ];

  assert.deepEqual(
    projectLatestMaterialFrames(
      ["source-0", "source-1", "source-2", "source-3"],
      ["variant-a", "missing-variant", "variant-b", "variant-c"],
      variants,
    ),
    [
      { sourceIndex: 0, variantId: "variant-a", variantIndex: 0 },
      { sourceIndex: 1, variantId: "variant-b", variantIndex: 0 },
      { sourceIndex: 2, variantId: "variant-c", variantIndex: 0 },
    ],
  );
});

test("processed display keeps every source frame in source order", () => {
  assert.deepEqual(
    projectMaterialFrameDisplay(
      ["source-0", "source-1", "source-2", "source-3"],
      ["variant-a", "variant-b"],
      [
        { id: "variant-a", frames: [{ sourceFrameId: "source-2" }] },
        { id: "variant-b", frames: [{ sourceFrameId: "source-0" }] },
      ],
    ),
    [
      { sourceIndex: 0, variantId: "variant-b", variantIndex: 0, processed: true },
      { sourceIndex: 1, variantId: null, variantIndex: -1, processed: false },
      { sourceIndex: 2, variantId: "variant-a", variantIndex: 0, processed: true },
      { sourceIndex: 3, variantId: null, variantIndex: -1, processed: false },
    ],
  );
});

test("processed tab keeps mixed frames and submits any selected source indexes", async () => {
  const manager = await readFile(resolve(import.meta.dirname, "../app/features/material-manager-v4.tsx"), "utf8");
  const api = await readFile(resolve(import.meta.dirname, "../app/lib/api.ts"), "utf8");
  assert.match(manager, /!source \|\| !domain \|\| !selected\.length/);
  assert.doesNotMatch(manager, /Boolean\(variant\) \|\| !selected\.length/);
  assert.match(manager, /createMaterialBasicJob\([\s\S]*processingSettings \}, selected\)/);
  assert.match(manager, /将处理本次选择的 <strong>\{selected\.length\}<\/strong> 帧/);
  assert.doesNotMatch(manager, /Workspace 3<\/span>/);
  assert.match(manager, /source\.displayName[\s\S]*source\.frames\.length[\s\S]*已有处理后结果/);
  assert.match(manager, /projectLatestMaterialFrames/);
  assert.match(manager, /处理后 <small>已处理 \{processedFrames\.length\} \/ \{source\.frames\.length\}<\/small>/);
  assert.match(manager, /material-frame-status/);
  assert.match(manager, /processed \? "已处理" : "源图"/);
  assert.match(api, /frameIndexes: \[\.\.\.frameIndexes\]\.sort\(\(a, b\) => a - b\)/);
});

test("single-frame preview owns pointer panning and fit reset", async () => {
  const source = await readFile(resolve(import.meta.dirname, "../app/features/material-manager-v4.tsx"), "utf8");
  assert.match(source, /event\.preventDefault\(\)/);
  assert.match(source, /setPointerCapture\(event\.pointerId\)/);
  assert.match(source, /releasePointerCapture/);
  assert.match(source, /window\.addEventListener\("blur", interrupt\)/);
  assert.match(source, /onDragStart=\{\(event\) => event\.preventDefault\(\)\}/);
  assert.match(source, /按住拖拽平移 · 滚轮缩放 · 适配复位/);
  assert.match(source, /function resetPreviewViewport\(\)[\s\S]*setPreviewZoom\(1\)[\s\S]*setPreviewPan\(\{ x: 0, y: 0 \}\)/);
});
