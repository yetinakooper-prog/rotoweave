import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import test from "node:test";

import {
  adjacentEnabledFrameIndices,
  buildShadowPreviewRequest,
  countCharacterFrameUsage,
  createPlaybackTimeline,
  distributeEnabledFrameDurations,
  enabledFrameDuration,
  frameIndexAtTime,
  frameIndexAtTimelineTime,
  inheritAllFrameShadows,
  moveFrameBlockAtBoundary,
  normalizeFrameDurations,
  reorderFrames,
  resolveCanvasFocusIndex,
  selectFrameIndex,
  setFramesEnabled,
  shadowEnabledMode,
  shadowEnabledOverride,
  shouldRequestShadowPreview,
} from "../app/lib/action-editor-v4.ts";

test("onion skins resolve the nearest enabled frames without wrapping", () => {
  const frames = [
    { enabled: true },
    { enabled: false },
    { enabled: true },
    { enabled: false },
    { enabled: true },
  ];
  assert.deepEqual(adjacentEnabledFrameIndices(frames, 2), { previous: 0, next: 4 });
  assert.deepEqual(adjacentEnabledFrameIndices(frames, 0), { previous: null, next: 2 });
  assert.deepEqual(adjacentEnabledFrameIndices(frames, 4), { previous: 2, next: null });
  assert.deepEqual(adjacentEnabledFrameIndices(frames, -1), { previous: null, next: null });
});

test("total duration is distributed only to enabled frames", () => {
  const frames = [
    { durationSeconds: 0.1, enabled: true, marker: "first" },
    { durationSeconds: 9, enabled: false, marker: "disabled" },
    { durationSeconds: 0.2, marker: "last" },
  ];
  const distributed = distributeEnabledFrameDurations(frames, 0.9);
  assert.equal(distributed[0].durationSeconds, 0.45);
  assert.equal(distributed[1].durationSeconds, 9);
  assert.equal(distributed[2].durationSeconds, 0.45);
  assert.ok(Math.abs(enabledFrameDuration(distributed) - 0.9) < 1e-12);
  assert.deepEqual(distributeEnabledFrameDurations(frames, 0), frames);
  assert.deepEqual(distributeEnabledFrameDurations(frames, 3600.1), frames);
  assert.deepEqual(
    distributeEnabledFrameDurations(frames.map((frame) => ({ ...frame, enabled: false })), 1),
    frames.map((frame) => ({ ...frame, enabled: false })),
  );
});

test("Windows-style selection supports replace, Ctrl toggle, and Shift range", () => {
  const first = selectFrameIndex(new Set(), 2, null, { shift: false, additive: false });
  assert.deepEqual([...first.selected], [2]);
  const added = selectFrameIndex(first.selected, 5, first.anchor, { shift: false, additive: true });
  assert.deepEqual([...added.selected], [2, 5]);
  const range = selectFrameIndex(added.selected, 7, added.anchor, { shift: true, additive: false });
  assert.deepEqual([...range.selected], [5, 6, 7]);
});

test("timeline helpers preserve order and normalize to exactly 24 fps", () => {
  assert.deepEqual(reorderFrames(["a", "b", "c"], 0, 2), ["b", "c", "a"]);
  const normalized = normalizeFrameDurations([
    { durationSeconds: 0.2 },
    { durationSeconds: 0.4 },
  ]);
  assert.equal(normalized[0].durationSeconds, 1 / 24);
  assert.equal(normalized[1].durationSeconds, 1 / 24);
});

test("timeline insertion moves arbitrary selections as one ordered block", () => {
  const forward = moveFrameBlockAtBoundary(["a", "b", "c", "d"], [1], 4);
  assert.deepEqual(forward, { frames: ["a", "c", "d", "b"], selectedIndices: [3] });

  const backward = moveFrameBlockAtBoundary(["a", "b", "c", "d"], [3], 1);
  assert.deepEqual(backward, { frames: ["a", "d", "b", "c"], selectedIndices: [1] });

  const nonContiguous = moveFrameBlockAtBoundary(["a", "b", "c", "d", "e"], [3, 1], 5);
  assert.deepEqual(nonContiguous, {
    frames: ["a", "c", "e", "b", "d"],
    selectedIndices: [3, 4],
  });

  const insideSelection = moveFrameBlockAtBoundary(["a", "b", "c", "d"], [1, 2], 2);
  assert.deepEqual(insideSelection, {
    frames: ["a", "b", "c", "d"],
    selectedIndices: [1, 2],
  });

  const first = moveFrameBlockAtBoundary(["a", "b", "c"], [2], Number.NaN);
  assert.deepEqual(first, { frames: ["c", "a", "b"], selectedIndices: [0] });
  assert.deepEqual(
    moveFrameBlockAtBoundary(["a", "b"], [-1, 9], 1),
    { frames: ["a", "b"], selectedIndices: [] },
  );
});

test("playback follows accumulated frame durations and loop mode", () => {
  const frames = [
    { durationSeconds: 0.1 },
    { durationSeconds: 0.3 },
    { durationSeconds: 0.2 },
  ];
  assert.equal(frameIndexAtTime(frames, 0.05, false), 0);
  assert.equal(frameIndexAtTime(frames, 0.25, false), 1);
  assert.equal(frameIndexAtTime(frames, 0.55, false), 2);
  assert.equal(frameIndexAtTime(frames, 0.65, true), 0);
  assert.equal(frameIndexAtTime(frames, 99, false), 2);
  const timeline = createPlaybackTimeline(frames);
  assert.deepEqual(timeline.cumulativeSeconds, [0.1, 0.4, 0.6000000000000001]);
  assert.equal(frameIndexAtTimelineTime(timeline, 0.25, false), 1);
});

test("canvas focus follows stable frame ids independently from selection", () => {
  const frames = [{ id: "a" }, { id: "b" }, { id: "c" }];
  assert.equal(resolveCanvasFocusIndex(frames, "c", new Set([0, 1])), 2);
  const reordered = [frames[2], frames[0], frames[1]];
  assert.equal(resolveCanvasFocusIndex(reordered, "c", new Set([1, 2])), 0);
  assert.equal(resolveCanvasFocusIndex(frames.slice(0, 2), "c", new Set([1])), 1);
  assert.equal(resolveCanvasFocusIndex([], "c", new Set()), -1);
});

test("used frame counts span the character while current unsaved draft wins", () => {
  const actions = [
    { id: "action-a", frameRefs: [
      { variantId: "variant-1", frameId: "frame-1", enabled: true },
      { variantId: "variant-1", frameId: "frame-1", enabled: false },
    ] },
    { id: "action-b", frameRefs: [
      { variantId: "variant-2", frameId: "frame-2", enabled: true },
    ] },
  ];
  const counts = countCharacterFrameUsage(actions, "action-a", [
    { variantId: "variant-3", frameId: "frame-3" },
    { variantId: "variant-3", frameId: "frame-3" },
  ]);
  assert.equal(counts.has("variant-1:frame-1"), false);
  assert.equal(counts.get("variant-2:frame-2"), 1);
  assert.equal(counts.get("variant-3:frame-3"), 2);
});

test("shadow preview requests batch once or deduplicate per focused frame", () => {
  const small = Array.from({ length: 30 }, (_, index) => ({ id: `f-${index}` }));
  const batch = buildShadowPreviewRequest(small, "f-10", true);
  assert.equal(batch?.key, "batch");
  assert.equal(batch?.cacheAll, true);
  assert.equal(batch?.previewIndex, 10);

  const large = Array.from({ length: 500 }, (_, index) => ({ id: `f-${index}` }));
  const window = buildShadowPreviewRequest(large, "f-250", true);
  assert.equal(window?.key, "f-250");
  assert.deepEqual(window?.frames.map((frame) => frame.id), ["f-249", "f-250", "f-251"]);
  assert.equal(shouldRequestShadowPreview(new Set(), new Set(), "f-250"), true);
  assert.equal(shouldRequestShadowPreview(new Set(["f-250"]), new Set(), "f-250"), false);
  assert.equal(shouldRequestShadowPreview(new Set(), new Set(["f-250"]), "f-250"), false);
});

test("disabled timeline frames stay in place but are skipped by playback", () => {
  const frames = [
    { durationSeconds: 0.1, enabled: true },
    { durationSeconds: 10, enabled: false },
    { durationSeconds: 0.2, enabled: true },
  ];
  assert.ok(Math.abs(enabledFrameDuration(frames) - 0.3) < 1e-12);
  assert.equal(frameIndexAtTime(frames, 0.05, false), 0);
  assert.equal(frameIndexAtTime(frames, 0.15, false), 2);
  assert.equal(frameIndexAtTime(frames, 0.35, true), 0);
  assert.equal(frameIndexAtTime(frames.map((frame) => ({ ...frame, enabled: false })), 0, false), -1);

  const disabled = setFramesEnabled(frames, [0, 2], false);
  assert.deepEqual(disabled.map((frame) => frame.enabled), [false, false, false]);
  const restored = setFramesEnabled(disabled, [1], true);
  assert.deepEqual(restored.map((frame) => frame.enabled), [false, true, false]);
});

test("shadow enablement stays explicitly three-state and bulk inheritance preserves geometry", () => {
  assert.equal(shadowEnabledMode(null), "inherit");
  assert.equal(shadowEnabledMode(true), "enabled");
  assert.equal(shadowEnabledMode(false), "disabled");
  assert.equal(shadowEnabledOverride("inherit"), null);
  assert.equal(shadowEnabledOverride("enabled"), true);
  assert.equal(shadowEnabledOverride("disabled"), false);

  const frames = [{
    id: "frame-1",
    variantId: "variant-1",
    frameId: "source-frame-1",
    durationSeconds: 1 / 24,
    enabled: true,
    transform: {
      position: { x: 0, y: 0 },
      scale: { x: 1, y: 1 },
      rotationDegrees: 0,
      color: "#ffffff",
      opacity: 1,
      shadow: {
        enabled: false,
        color: "#123456",
        opacity: 0.2,
        offset: { x: -8, y: 3 },
        scale: { x: 1.5, y: 0.7 },
      },
    },
  }];
  const inherited = inheritAllFrameShadows(frames);
  assert.deepEqual(
    {
      enabled: inherited[0].transform.shadow.enabled,
      color: inherited[0].transform.shadow.color,
      opacity: inherited[0].transform.shadow.opacity,
    },
    { enabled: null, color: null, opacity: null },
  );
  assert.deepEqual(inherited[0].transform.shadow.offset, { x: -8, y: 3 });
  assert.deepEqual(inherited[0].transform.shadow.scale, { x: 1.5, y: 0.7 });
  assert.equal(frames[0].transform.shadow.enabled, false);
});

test("timeline locate stays inline while status badges and source identity are removed", async () => {
  const editor = await readFile(resolve(import.meta.dirname, "../app/features/action-editor-v4.tsx"), "utf8");
  const shell = await readFile(resolve(import.meta.dirname, "../app/client-shell-v4.tsx"), "utf8");
  const styles = await readFile(resolve(import.meta.dirname, "../app/globals.css"), "utf8");

  assert.match(editor, /setLibrarySelection\(new Set\(\[libraryIndex\]\)\)/);
  assert.match(editor, /setCollapsedSources[\s\S]*next\.delete\(groupKey\)/);
  assert.match(editor, /scrollIntoView\(\{ behavior: "smooth", block: "center", inline: "nearest" \}\)/);
  assert.match(editor, /在当前动作页素材帧库中展开并选中此帧/);
  assert.match(editor, /aria-label="定位到素材帧"/);
  assert.match(editor, /className="timeline-locate-button"/);
  assert.match(editor, /<LocateFixed size=\{14\} \/><\/button>/);
  assert.doesNotMatch(editor, /<LocateFixed size=\{14\} \/>定位/);
  assert.match(editor, /TIMELINE_DRAG_TYPE/);
  assert.match(editor, /timelineInsertionBoundary/);
  assert.match(editor, /timelineBoundaryAtClientX/);
  assert.match(editor, /timelineEdgeVelocityV4/);
  assert.match(styles, /\.action-editor-timeline-track > \.timeline-drop-before::before/);
  assert.match(styles, /\.timeline-locate-button/);
  assert.doesNotMatch(shell, /onLocateSource=/);
  assert.doesNotMatch(editor, /timeline-enabled-mark/);
  assert.doesNotMatch(editor, /未知源素材|未知素材.*源/);
  assert.match(editor, /时间轴 \{String\(index \+ 1\)\.padStart\(3, "0"\)\}/);
  assert.match(editor, /type="range" min="0\.25" max="2" step="any"/);
  assert.match(editor, />总时长（秒）<input/);
  assert.match(editor, />均分到启用帧<\/button>/);
  assert.match(editor, /setPlaying\(false\)[\s\S]*distributeEnabledFrameDurations/);
  assert.match(styles, /\.timeline-frame-summary/);
  assert.match(shell, /保存并继续/);
  assert.match(shell, /放弃修改/);
  assert.doesNotMatch(shell, /当前动作有未保存修改，确定离开吗/);
});

test("historical locate, image comparison, and server-confirmed save baseline stay explicit", async () => {
  const editor = await readFile(resolve(import.meta.dirname, "../app/features/action-editor-v4.tsx"), "utf8");
  const canvas = await readFile(resolve(import.meta.dirname, "../app/components/action-canvas-v4.tsx"), "utf8");
  assert.match(editor, /locatedHistoricalFrame/);
  assert.match(editor, /projectLatestMaterialFrames/);
  assert.match(editor, /countCharacterFrameUsage/);
  assert.match(editor, /action-editor-library-usage-mark/);
  assert.match(editor, /当前角色动作共引用/);
  assert.match(editor, /latestFrameKeys\.has\(libraryKey\)/);
  assert.doesNotMatch(editor, /latestVariantIds/);
  assert.match(editor, /历史定位/);
  assert.match(editor, /setPendingLocateKey\(libraryKey\)/);
  assert.match(editor, /focusedOriginalUrl/);
  assert.match(editor, /adjacentEnabledFrameIndices\(draft, focusedIndex\)/);
  assert.match(editor, /previousFrame=\{previousOnionFrame\}/);
  assert.match(editor, /nextFrame=\{nextOnionFrame\}/);
  assert.match(canvas, /useState<"original" \| "result">\("result"\)/);
  assert.match(canvas, /aria-label="动作画布底图"/);
  assert.match(canvas, />原图<\/button><button[\s\S]*>成图<\/button>/);
  assert.match(editor, /const \[serverBaseline, setServerBaseline\]/);
  assert.match(editor, /const latestDraft = cloneFrames\(draftSnapshot\.current\)/);
  assert.match(editor, /setServerBaseline\(confirmed\)[\s\S]*updateDraft\(confirmed\)[\s\S]*onDirtyChange\?\.\(false\)/);
});

test("Alt focus stays separate from multi-selection and playback work is cached", async () => {
  const editor = await readFile(resolve(import.meta.dirname, "../app/features/action-editor-v4.tsx"), "utf8");
  assert.match(editor, /event\.altKey && selectedIndices\.length > 1/);
  assert.match(editor, /setFocusedFrameId\(draft\[index\]\?\.id \?\? null\)[\s\S]*setPlaying\(false\)[\s\S]*return/);
  assert.match(editor, /const targets = new Set\(selectedIndices\.length \? selectedIndices : \[focusedIndex\]\)/);
  assert.match(editor, /批量调整 \$\{selectedIndices\.length\} 帧 · 画布显示第 \$\{focusedIndex \+ 1\} 帧/);
  assert.match(editor, /className=\{`\$\{selection\.has\(index\)/);
  assert.match(editor, /canvas-focused/);
  assert.match(editor, /createPlaybackTimeline\(draft\)/);
  assert.match(editor, /setPlayIndex\(\(current\) => current === nextIndex \? current : nextIndex\)/);
  assert.match(editor, /pendingShadowPreviewKeys/);
  assert.match(editor, /shouldRequestShadowPreview/);
  assert.doesNotMatch(editor, /onScale=/);
});
