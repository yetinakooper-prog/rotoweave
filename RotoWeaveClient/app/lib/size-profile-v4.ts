import { CANONICAL_PIXELS_PER_UNIT } from "./protocol-contract";

export type SizeUnitModeV4 = "pixels" | "unity";
export type SizeProfileV4 = { id: string; name: string; presetId?: string | null; unitMode: SizeUnitModeV4; width: number; height: number };
export type SharedSizePresetV4 = { id: string; name: string; unit_mode: SizeUnitModeV4; width_world: number; height_world: number };

export function sizeProfilePixels(profile: SizeProfileV4, pixelsPerUnit = CANONICAL_PIXELS_PER_UNIT) {
  const factor = profile.unitMode === "unity" ? pixelsPerUnit : 1;
  return { width: profile.width * factor, height: profile.height * factor };
}

export function sizeProfileWorld(profile: SizeProfileV4, pixelsPerUnit = CANONICAL_PIXELS_PER_UNIT) {
  const factor = profile.unitMode === "pixels" ? 1 / pixelsPerUnit : 1;
  return { width: profile.width * factor, height: profile.height * factor };
}

export function convertSizeProfileUnit(profile: SizeProfileV4, unitMode: SizeUnitModeV4, pixelsPerUnit = CANONICAL_PIXELS_PER_UNIT): SizeProfileV4 {
  if (profile.unitMode === unitMode) return profile;
  const resolved = unitMode === "pixels" ? sizeProfilePixels(profile, pixelsPerUnit) : sizeProfileWorld(profile, pixelsPerUnit);
  return { ...profile, unitMode, width: Number(resolved.width.toFixed(unitMode === "pixels" ? 2 : 4)), height: Number(resolved.height.toFixed(unitMode === "pixels" ? 2 : 4)) };
}

/** Freeze a shared workspace preset into one character-local calibration snapshot. */
export function profileSnapshotFromPreset(
  preset: SharedSizePresetV4,
  localProfileId: string,
  pixelsPerUnit = CANONICAL_PIXELS_PER_UNIT,
): SizeProfileV4 & { presetId: string } {
  const unitMode = preset.unit_mode ?? "unity";
  return {
    id: localProfileId,
    name: preset.name,
    presetId: preset.id,
    unitMode,
    width: unitMode === "pixels"
      ? Math.round(preset.width_world * pixelsPerUnit)
      : Number(preset.width_world.toFixed(4)),
    height: unitMode === "pixels"
      ? Math.round(preset.height_world * pixelsPerUnit)
      : Number(preset.height_world.toFixed(4)),
  };
}
