export const MIN_CORE_REFERENCE_SCALE: 0.01;
export const MAX_CORE_REFERENCE_SCALE: 8;

export function resolveCoreReferenceScale(value: unknown): number;

export function coreReferenceRenderRect(options: {
  width: number;
  height: number;
  originX: number;
  originY: number;
  scale: unknown;
}): {
  x: number;
  y: number;
  width: number;
  height: number;
  scale: number;
};

export function coreReferenceOriginFromRender(
  x: number,
  y: number,
  scale: unknown,
): { x: number; y: number };
