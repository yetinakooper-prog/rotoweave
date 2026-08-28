export const MIN_CORE_REFERENCE_SCALE = 0.005;
export const MAX_CORE_REFERENCE_SCALE = 8;

export function resolveCoreReferenceScale(value) {
  const numeric = Number(value);
  const finite = Number.isFinite(numeric) ? numeric : 1;
  return Math.max(
    MIN_CORE_REFERENCE_SCALE,
    Math.min(MAX_CORE_REFERENCE_SCALE, finite),
  );
}

export function coreReferenceRenderRect({
  width,
  height,
  originX,
  originY,
  scale,
}) {
  const resolvedScale = resolveCoreReferenceScale(scale);
  return {
    x: originX * resolvedScale,
    y: originY * resolvedScale,
    width: width * resolvedScale,
    height: height * resolvedScale,
    scale: resolvedScale,
  };
}

export function coreReferenceOriginFromRender(x, y, scale) {
  const resolvedScale = resolveCoreReferenceScale(scale);
  return {
    x: x / resolvedScale,
    y: y / resolvedScale,
  };
}
