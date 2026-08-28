const DISPLAY_PRECISION = 6;

export function toPercentDisplay(value: number): number {
  return Number((value * 100).toFixed(DISPLAY_PRECISION));
}

export function fromPercentDisplay(value: number): number {
  return Number((value / 100).toFixed(DISPLAY_PRECISION + 2));
}
