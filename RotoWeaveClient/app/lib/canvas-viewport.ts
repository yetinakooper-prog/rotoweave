export type CanvasViewport = { x: number; y: number; scale: number };
export type CanvasPoint = { x: number; y: number };

export const MIN_CANVAS_SCALE = 0.02;
export const MAX_CANVAS_SCALE = 50;

export function zoomCanvasViewportAtPoint(
  viewport: CanvasViewport,
  pointer: CanvasPoint,
  factor: number,
  minimum = MIN_CANVAS_SCALE,
  maximum = MAX_CANVAS_SCALE,
): CanvasViewport {
  const nextScale = Math.max(minimum, Math.min(maximum, viewport.scale * factor));
  const local = {
    x: (pointer.x - viewport.x) / viewport.scale,
    y: (pointer.y - viewport.y) / viewport.scale,
  };
  return {
    scale: nextScale,
    x: pointer.x - local.x * nextScale,
    y: pointer.y - local.y * nextScale,
  };
}
