export type CanvasBounds = { left: number; top: number; right: number; bottom: number };

export function rotatedFrameBounds(
  width: number,
  height: number,
  position: { x: number; y: number },
  scale: { x: number; y: number },
  rotationDegrees: number,
): CanvasBounds {
  const radians = rotationDegrees * Math.PI / 180;
  const cosine = Math.cos(radians);
  const sine = Math.sin(radians);
  const corners = [[-width / 2, -height], [width / 2, -height], [width / 2, 0], [-width / 2, 0]] as const;
  const points = corners.map(([x, y]) => {
    const scaledX = x * scale.x;
    const scaledY = y * scale.y;
    return {
      x: position.x + scaledX * cosine - scaledY * sine,
      y: -position.y + scaledX * sine + scaledY * cosine,
    };
  });
  return {
    left: Math.min(...points.map((point) => point.x)),
    top: Math.min(...points.map((point) => point.y)),
    right: Math.max(...points.map((point) => point.x)),
    bottom: Math.max(...points.map((point) => point.y)),
  };
}

export function fitActionCanvasViewport(
  stage: { width: number; height: number },
  bounds: CanvasBounds,
) {
  const contentWidth = Math.max(1, bounds.right - bounds.left);
  const contentHeight = Math.max(1, bounds.bottom - bounds.top);
  const padding = Math.max(28, Math.min(stage.width, stage.height) * 0.08);
  const scale = Math.max(0.02, Math.min(50, Math.min(
    (stage.width - padding * 2) / contentWidth,
    (stage.height - padding * 2) / contentHeight,
  )));
  const centerX = (bounds.left + bounds.right) / 2;
  const centerY = (bounds.top + bounds.bottom) / 2;
  return { scale, x: stage.width / 2 - centerX * scale, y: stage.height / 2 - centerY * scale };
}
