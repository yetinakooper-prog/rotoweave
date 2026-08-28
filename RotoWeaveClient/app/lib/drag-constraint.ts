export type DragPoint = { x: number; y: number };
export type DragAxis = "x" | "y" | null;

export function constrainDragPoint(
  origin: DragPoint,
  current: DragPoint,
  shiftKey: boolean,
  lockedAxis: DragAxis = null,
): { point: DragPoint; axis: DragAxis } {
  if (!shiftKey) return { point: current, axis: null };

  const axis =
    lockedAxis ??
    (Math.abs(current.x - origin.x) >= Math.abs(current.y - origin.y)
      ? "x"
      : "y");
  return {
    point:
      axis === "x"
        ? { x: current.x, y: origin.y }
        : { x: origin.x, y: current.y },
    axis,
  };
}
