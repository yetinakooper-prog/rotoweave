export const TIMELINE_EDGE_MAX_SPEED_PX_PER_SECOND = 180;

export function timelineEdgeVelocityV4(input: {
  pointerX: number;
  left: number;
  width: number;
  scrollLeft: number;
  scrollWidth: number;
}): number {
  const { pointerX, left, width, scrollLeft, scrollWidth } = input;
  if (width <= 0 || pointerX < left || pointerX > left + width) return 0;
  const edgeWidth = Math.min(64, width / 4);
  const maxScrollLeft = Math.max(0, scrollWidth - width);
  if (pointerX < left + edgeWidth && scrollLeft > 0) {
    const ratio = Math.min(1, Math.max(0, (left + edgeWidth - pointerX) / edgeWidth));
    return -TIMELINE_EDGE_MAX_SPEED_PX_PER_SECOND * ratio;
  }
  if (pointerX > left + width - edgeWidth && scrollLeft < maxScrollLeft) {
    const ratio = Math.min(1, Math.max(0, (pointerX - (left + width - edgeWidth)) / edgeWidth));
    return TIMELINE_EDGE_MAX_SPEED_PX_PER_SECOND * ratio;
  }
  return 0;
}
