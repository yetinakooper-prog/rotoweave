/** High-contrast guide styling shared by every editable canvas. */
export const CANVAS_GUIDE_COLORS = {
  halo: "#101418",
  size: "#00e6ad",
  center: "#d946ef",
  horizon: "#009dff",
  shadowY: "#ff6b00",
  label: "#ffffff",
} as const;

export const CANVAS_GUIDE_WIDTHS = {
  halo: 5,
  normal: 2.5,
  active: 3.25,
} as const;

export const CANVAS_GUIDE_DASHES = {
  size: [12, 5],
  center: [5, 5],
  horizon: [14, 6],
  shadowY: [8, 5],
} as const;
