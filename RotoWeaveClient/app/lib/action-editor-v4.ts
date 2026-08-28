import type { ActionFrameRefV4, DomainActionV4 } from "./types";

export function countCharacterFrameUsage(
  actions: readonly DomainActionV4[],
  currentActionId: string | null | undefined,
  currentDraft: readonly Pick<ActionFrameRefV4, "variantId" | "frameId">[],
): Map<string, number> {
  const counts = new Map<string, number>();
  for (const action of actions) {
    const frames = action.id === currentActionId ? currentDraft : action.frameRefs;
    for (const frame of frames) {
      const key = `${frame.variantId}:${frame.frameId}`;
      counts.set(key, (counts.get(key) ?? 0) + 1);
    }
  }
  return counts;
}

export type ShadowEnabledModeV4 = "inherit" | "enabled" | "disabled";

export function shadowEnabledMode(value: boolean | null): ShadowEnabledModeV4 {
  if (value === null) return "inherit";
  return value ? "enabled" : "disabled";
}

export function shadowEnabledOverride(mode: ShadowEnabledModeV4): boolean | null {
  if (mode === "inherit") return null;
  return mode === "enabled";
}

export function inheritAllFrameShadows(frames: ActionFrameRefV4[]): ActionFrameRefV4[] {
  return frames.map((frame) => ({
    ...frame,
    transform: {
      ...frame.transform,
      shadow: {
        ...frame.transform.shadow,
        enabled: null,
        color: null,
        opacity: null,
      },
    },
  }));
}

export function selectFrameIndex(
  selected: ReadonlySet<number>,
  index: number,
  anchor: number | null,
  options: { shift: boolean; additive: boolean },
): { selected: Set<number>; anchor: number } {
  if (options.shift && anchor !== null) {
    const next = options.additive ? new Set(selected) : new Set<number>();
    const start = Math.min(anchor, index);
    const end = Math.max(anchor, index);
    for (let cursor = start; cursor <= end; cursor += 1) next.add(cursor);
    return { selected: next, anchor };
  }
  if (options.additive) {
    const next = new Set(selected);
    if (next.has(index)) next.delete(index);
    else next.add(index);
    return { selected: next, anchor: index };
  }
  return { selected: new Set([index]), anchor: index };
}

export function normalizeFrameDurations<T extends Pick<ActionFrameRefV4, "durationSeconds">>(
  frames: T[],
): T[] {
  return frames.map((frame) => ({ ...frame, durationSeconds: 1 / 24 }));
}

export function distributeEnabledFrameDurations<
  T extends Pick<ActionFrameRefV4, "durationSeconds"> & { enabled?: boolean },
>(frames: T[], totalSeconds: number): T[] {
  const enabledCount = frames.filter((frame) => frame.enabled !== false).length;
  if (!Number.isFinite(totalSeconds) || totalSeconds <= 0 || totalSeconds > 3600 || !enabledCount) {
    return [...frames];
  }
  const durationSeconds = totalSeconds / enabledCount;
  return frames.map((frame) => (
    frame.enabled === false ? frame : { ...frame, durationSeconds }
  ));
}

export function setFramesEnabled<T extends { enabled?: boolean }>(
  frames: T[],
  indices: Iterable<number>,
  enabled: boolean,
): Array<T & { enabled: boolean }> {
  const targets = new Set(indices);
  return frames.map((frame, index) => ({ ...frame, enabled: targets.has(index) ? enabled : frame.enabled !== false }));
}

export function enabledFrameDuration(
  frames: Array<Pick<ActionFrameRefV4, "durationSeconds"> & { enabled?: boolean }>,
): number {
  return frames.reduce(
    (sum, frame) => frame.enabled === false ? sum : sum + Math.max(0, frame.durationSeconds),
    0,
  );
}

export function adjacentEnabledFrameIndices<T extends { enabled?: boolean }>(
  frames: T[],
  currentIndex: number,
): { previous: number | null; next: number | null } {
  if (!Number.isInteger(currentIndex) || currentIndex < 0 || currentIndex >= frames.length) {
    return { previous: null, next: null };
  }
  let previous: number | null = null;
  let next: number | null = null;
  for (let index = currentIndex - 1; index >= 0; index -= 1) {
    if (frames[index].enabled !== false) {
      previous = index;
      break;
    }
  }
  for (let index = currentIndex + 1; index < frames.length; index += 1) {
    if (frames[index].enabled !== false) {
      next = index;
      break;
    }
  }
  return { previous, next };
}

export function reorderFrames<T>(frames: T[], from: number, to: number): T[] {
  if (from === to || from < 0 || to < 0 || from >= frames.length || to >= frames.length) {
    return [...frames];
  }
  const next = [...frames];
  const [moved] = next.splice(from, 1);
  next.splice(to, 0, moved);
  return next;
}

export function moveFrameBlockAtBoundary<T>(
  frames: T[],
  indices: Iterable<number>,
  boundary: number,
): { frames: T[]; selectedIndices: number[] } {
  const selectedIndices = [...new Set(indices)]
    .filter((index) => Number.isInteger(index) && index >= 0 && index < frames.length)
    .sort((left, right) => left - right);
  if (!selectedIndices.length) return { frames: [...frames], selectedIndices: [] };

  const insertionBoundary = Number.isFinite(boundary)
    ? Math.max(0, Math.min(frames.length, Math.trunc(boundary)))
    : 0;
  const selected = new Set(selectedIndices);
  const moved = selectedIndices.map((index) => frames[index]);
  const remaining = frames.filter((_, index) => !selected.has(index));
  const removedBeforeBoundary = selectedIndices.filter((index) => index < insertionBoundary).length;
  const insertAt = Math.max(
    0,
    Math.min(remaining.length, insertionBoundary - removedBeforeBoundary),
  );
  const next = [...remaining];
  next.splice(insertAt, 0, ...moved);
  return {
    frames: next,
    selectedIndices: moved.map((_, index) => insertAt + index),
  };
}

export type PlaybackTimeline = {
  indices: number[];
  cumulativeSeconds: number[];
  totalSeconds: number;
};

export function createPlaybackTimeline(
  frames: Array<Pick<ActionFrameRefV4, "durationSeconds"> & { enabled?: boolean }>,
): PlaybackTimeline {
  const indices: number[] = [];
  const cumulativeSeconds: number[] = [];
  let totalSeconds = 0;
  frames.forEach((frame, index) => {
    if (frame.enabled === false) return;
    totalSeconds += Math.max(0, frame.durationSeconds);
    indices.push(index);
    cumulativeSeconds.push(totalSeconds);
  });
  return { indices, cumulativeSeconds, totalSeconds };
}

export function frameIndexAtTimelineTime(
  timeline: PlaybackTimeline,
  elapsedSeconds: number,
  loop: boolean,
): number {
  if (!timeline.indices.length) return -1;
  if (timeline.totalSeconds <= 0) return timeline.indices[0];
  const time = loop
    ? ((elapsedSeconds % timeline.totalSeconds) + timeline.totalSeconds) % timeline.totalSeconds
    : Math.min(Math.max(0, elapsedSeconds), Math.max(0, timeline.totalSeconds - Number.EPSILON));
  let low = 0;
  let high = timeline.cumulativeSeconds.length;
  while (low < high) {
    const middle = Math.floor((low + high) / 2);
    if (time < timeline.cumulativeSeconds[middle]) high = middle;
    else low = middle + 1;
  }
  return timeline.indices[Math.min(low, timeline.indices.length - 1)];
}

export function resolveCanvasFocusIndex<T extends { id: string }>(
  frames: T[],
  focusedFrameId: string | null,
  selectedIndices: Iterable<number>,
): number {
  const focusedIndex = focusedFrameId === null
    ? -1
    : frames.findIndex((frame) => frame.id === focusedFrameId);
  if (focusedIndex >= 0) return focusedIndex;
  const selectedIndex = [...selectedIndices]
    .filter((index) => Number.isInteger(index) && index >= 0 && index < frames.length)
    .sort((left, right) => left - right)[0];
  return selectedIndex ?? (frames.length ? 0 : -1);
}

export type ShadowPreviewRequest<T> = {
  key: string;
  frames: T[];
  previewIndex: number;
  cacheAll: boolean;
};

export function buildShadowPreviewRequest<T extends { id: string; enabled?: boolean }>(
  frames: T[],
  focusedFrameId: string,
  loop: boolean,
  maximumBatchSize = 256,
): ShadowPreviewRequest<T> | null {
  const enabledFrames = frames.filter((frame) => frame.enabled !== false);
  const previewIndex = enabledFrames.findIndex((frame) => frame.id === focusedFrameId);
  if (previewIndex < 0) return null;
  if (enabledFrames.length <= maximumBatchSize) {
    return { key: "batch", frames: enabledFrames, previewIndex, cacheAll: true };
  }
  const previous = loop
    ? enabledFrames[(previewIndex - 1 + enabledFrames.length) % enabledFrames.length]
    : enabledFrames[Math.max(0, previewIndex - 1)];
  const next = loop
    ? enabledFrames[(previewIndex + 1) % enabledFrames.length]
    : enabledFrames[Math.min(enabledFrames.length - 1, previewIndex + 1)];
  return {
    key: focusedFrameId,
    frames: [previous, enabledFrames[previewIndex], next],
    previewIndex: 1,
    cacheAll: false,
  };
}

export function shouldRequestShadowPreview(
  cachedKeys: { has(key: string): boolean },
  pendingKeys: { has(key: string): boolean },
  key: string,
): boolean {
  return !cachedKeys.has(key) && !pendingKeys.has(key);
}

export function frameIndexAtTime(
  frames: Array<Pick<ActionFrameRefV4, "durationSeconds"> & { enabled?: boolean }>,
  elapsedSeconds: number,
  loop: boolean,
): number {
  return frameIndexAtTimelineTime(createPlaybackTimeline(frames), elapsedSeconds, loop);
}
