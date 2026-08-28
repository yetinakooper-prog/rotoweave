export type WindowsSelectionModifiers = { shift: boolean; additive: boolean };

export function selectMaterialFrame(
  current: ReadonlySet<number>,
  index: number,
  anchor: number | null,
  modifiers: WindowsSelectionModifiers,
): { selected: Set<number>; anchor: number } {
  if (modifiers.shift && anchor !== null) {
    const first = Math.min(anchor, index);
    const last = Math.max(anchor, index);
    const selected = modifiers.additive ? new Set(current) : new Set<number>();
    for (let cursor = first; cursor <= last; cursor += 1) selected.add(cursor);
    return { selected, anchor };
  }
  if (modifiers.additive) {
    const selected = new Set(current);
    if (selected.has(index)) selected.delete(index);
    else selected.add(index);
    return { selected, anchor: index };
  }
  return { selected: new Set([index]), anchor: index };
}

export function selectedFrameSequence(selection: ReadonlySet<number>): number[] {
  return [...selection].filter(Number.isInteger).sort((left, right) => left - right);
}

export function projectMaterialFrameIndexes(
  sourceFrameIds: readonly string[],
  variantSourceFrameIds?: readonly string[],
): Array<{ sourceIndex: number; variantIndex: number }> {
  if (!variantSourceFrameIds) {
    return sourceFrameIds.map((_, sourceIndex) => ({ sourceIndex, variantIndex: -1 }));
  }
  const sourceIndexes = new Map(sourceFrameIds.map((id, index) => [id, index]));
  return variantSourceFrameIds.flatMap((sourceFrameId, variantIndex) => {
    const sourceIndex = sourceIndexes.get(sourceFrameId);
    return sourceIndex === undefined ? [] : [{ sourceIndex, variantIndex }];
  });
}

export type MaterialFrameProjection = {
  sourceIndex: number;
  variantId: string;
  variantIndex: number;
};

export type MaterialFrameDisplayProjection = {
  sourceIndex: number;
  variantId: string | null;
  variantIndex: number;
  processed: boolean;
};

export function projectLatestMaterialFrames(
  sourceFrameIds: readonly string[],
  variantIds: readonly string[],
  variants: readonly {
    id: string;
    frames: readonly { sourceFrameId: string }[];
  }[],
): MaterialFrameProjection[] {
  const sourceIndexes = new Map(sourceFrameIds.map((id, index) => [id, index]));
  const variantsById = new Map(variants.map((variant) => [variant.id, variant]));
  const latestBySourceIndex = new Map<number, MaterialFrameProjection>();

  for (const variantId of variantIds) {
    const variant = variantsById.get(variantId);
    if (!variant) continue;
    variant.frames.forEach((frame, variantIndex) => {
      const sourceIndex = sourceIndexes.get(frame.sourceFrameId);
      if (sourceIndex === undefined) return;
      latestBySourceIndex.set(sourceIndex, { sourceIndex, variantId, variantIndex });
    });
  }

  return [...latestBySourceIndex.values()].sort(
    (left, right) => left.sourceIndex - right.sourceIndex,
  );
}

export function projectMaterialFrameDisplay(
  sourceFrameIds: readonly string[],
  variantIds: readonly string[],
  variants: readonly {
    id: string;
    frames: readonly { sourceFrameId: string }[];
  }[],
): MaterialFrameDisplayProjection[] {
  const processedByIndex = new Map(
    projectLatestMaterialFrames(sourceFrameIds, variantIds, variants)
      .map((item) => [item.sourceIndex, item] as const),
  );
  return sourceFrameIds.map((_, sourceIndex) => {
    const processed = processedByIndex.get(sourceIndex);
    return processed
      ? { ...processed, processed: true }
      : { sourceIndex, variantId: null, variantIndex: -1, processed: false };
  });
}

export function hexToRgb(hex: string): [number, number, number] {
  const normalized = hex.trim().replace(/^#/, "");
  if (!/^[0-9a-f]{6}$/i.test(normalized)) return [0, 255, 0];
  return [0, 2, 4].map((offset) => Number.parseInt(normalized.slice(offset, offset + 2), 16)) as [number, number, number];
}
