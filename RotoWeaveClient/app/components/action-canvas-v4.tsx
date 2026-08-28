import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Ellipse, Group, Image as KonvaImage, Layer, Line, Rect, Stage, Text } from "react-konva";
import useImage from "use-image";
import { api } from "../lib/api";
import { CANVAS_GUIDE_COLORS, CANVAS_GUIDE_DASHES, CANVAS_GUIDE_WIDTHS } from "../lib/canvas-guide-style";
import { fitActionCanvasViewport, rotatedFrameBounds, type CanvasBounds } from "../lib/action-canvas-viewport";
import { coreReferenceRenderRect } from "../lib/core-reference-scale.mjs";
import { CANONICAL_PIXELS_PER_UNIT } from "../lib/protocol-contract";
import { sizeProfilePixels } from "../lib/size-profile-v4";
import type { ActionFrameRefV4, DomainCharacterV4, ShadowPreview } from "../lib/types";
import { useCanvasSpacePan } from "../lib/use-canvas-navigation";
import { zoomCanvasViewportAtPoint } from "../lib/canvas-viewport";
import { constrainDragPoint, type DragAxis } from "../lib/drag-constraint";

export type ActionCanvasModeV4 = "canvas" | "frame" | "shadow-x";

type Props = {
  openKey: string;
  character: DomainCharacterV4;
  frame: ActionFrameRefV4 | undefined;
  frameUrl: string | null;
  originalFrameUrl: string | null;
  previousFrame?: ActionFrameRefV4;
  previousFrameUrl: string | null;
  nextFrame?: ActionFrameRefV4;
  nextFrameUrl: string | null;
  shadowPreview: ShadowPreview | null;
  selectedCount: number;
  playing: boolean;
  playableCount: number;
  hasPlayableOriginalFrame: boolean;
  onMove: (dx: number, dy: number) => void;
  onShadowX: (dx: number) => void;
};

function editable(target: EventTarget | null) {
  const element = target as HTMLElement | null;
  return Boolean(element?.closest("input,textarea,select,[contenteditable=true]"));
}

function tintedImage(image: HTMLImageElement | undefined, color: string | null | undefined) {
  if (!image || !color || color.toLowerCase() === "#ffffff") return image;
  const canvas = document.createElement("canvas"); canvas.width = image.naturalWidth; canvas.height = image.naturalHeight;
  const context = canvas.getContext("2d", { willReadFrequently: true }); if (!context) return image;
  context.drawImage(image, 0, 0); const data = context.getImageData(0, 0, canvas.width, canvas.height);
  const r = parseInt(color.slice(1, 3), 16) / 255; const g = parseInt(color.slice(3, 5), 16) / 255; const b = parseInt(color.slice(5, 7), 16) / 255;
  for (let index = 0; index < data.data.length; index += 4) { data.data[index] *= r; data.data[index + 1] *= g; data.data[index + 2] *= b; }
  context.putImageData(data, 0, 0); return canvas;
}

type ToolbarProps = {
  mode: ActionCanvasModeV4;
  selectedCount: number;
  viewportScale: number;
  imageMode: "original" | "result";
  originalAvailable: boolean;
  references: { core: boolean; previous: boolean; next: boolean };
  referenceAvailability: { core: boolean; previous: boolean; next: boolean };
  guides: { size: boolean; center: boolean; horizon: boolean; shadow: boolean };
  onModeChange: (mode: ActionCanvasModeV4) => void;
  onFit: () => void;
  onImageModeChange: (mode: "original" | "result") => void;
  onToggleReference: (key: "core" | "previous" | "next") => void;
  onToggleGuide: (key: "size" | "center" | "horizon" | "shadow") => void;
};

const ActionCanvasToolbar = memo(function ActionCanvasToolbar({ mode, selectedCount, viewportScale, imageMode, originalAvailable, references, referenceAvailability, guides, onModeChange, onFit, onImageModeChange, onToggleReference, onToggleGuide }: ToolbarProps) {
  return <div className="v4-canvas-toolbar action-canvas-v4-toolbar"><div role="group" aria-label="动作画布工具"><button type="button" className={mode === "canvas" ? "active" : ""} onClick={() => onModeChange("canvas")}><kbd>1</kbd>画布</button><button type="button" className={mode === "frame" ? "active" : ""} onClick={() => onModeChange("frame")}><kbd>2</kbd>帧</button><button type="button" className={mode === "shadow-x" ? "active" : ""} onClick={() => onModeChange("shadow-x")}><kbd>3</kbd>阴影 X</button><button type="button" onClick={onFit}>适配</button></div><span>{selectedCount > 1 ? `${selectedCount} 帧同步` : "单帧"} · {Math.round(viewportScale * 100)}%</span><div className="action-image-mode-toggle" role="group" aria-label="动作画布底图"><button type="button" aria-pressed={imageMode === "original"} disabled={!originalAvailable} onClick={() => onImageModeChange("original")}>原图</button><button type="button" aria-pressed={imageMode === "result"} onClick={() => onImageModeChange("result")}>成图</button></div><div className="action-reference-toggles" role="group" aria-label="动作参考图层显示"><button type="button" aria-pressed={references.core} disabled={!referenceAvailability.core} onClick={() => onToggleReference("core")}>核心形象</button><button type="button" aria-pressed={references.previous} disabled={!referenceAvailability.previous} onClick={() => onToggleReference("previous")}>前帧洋葱</button><button type="button" aria-pressed={references.next} disabled={!referenceAvailability.next} onClick={() => onToggleReference("next")}>后帧洋葱</button></div><div className="action-guide-toggles" role="group" aria-label="动作辅助线显示"><button type="button" aria-pressed={guides.size} onClick={() => onToggleGuide("size")}>尺寸框</button><button type="button" aria-pressed={guides.center} onClick={() => onToggleGuide("center")}>中轴线</button><button type="button" aria-pressed={guides.horizon} onClick={() => onToggleGuide("horizon")}>地平线</button><button type="button" aria-pressed={guides.shadow} onClick={() => onToggleGuide("shadow")}>阴影线</button></div></div>;
});

export function ActionCanvasV4({ openKey, character, frame, frameUrl, originalFrameUrl, previousFrame, previousFrameUrl, nextFrame, nextFrameUrl, shadowPreview, selectedCount, playing, playableCount, hasPlayableOriginalFrame, onMove, onShadowX }: Props) {
  const surface = useRef<HTMLDivElement>(null); const [size, setSize] = useState({ width: 800, height: 480 });
  const [surfaceMeasured, setSurfaceMeasured] = useState(false);
  const [mode, setMode] = useState<ActionCanvasModeV4>("canvas");
  const navigation = useCanvasSpacePan(true);
  const [guides, setGuides] = useState({ size: true, center: true, horizon: true, shadow: true });
  const [references, setReferences] = useState({ core: true, previous: false, next: false });
  const [imageMode, setImageMode] = useState<"original" | "result">("result");
  const [viewport, setViewport] = useState({ x: 400, y: 300, scale: 1 });
  const viewportDragRef = useRef<{ origin: { x: number; y: number }; axis: DragAxis } | null>(null);
  const frameDragRef = useRef<{ origin: { x: number; y: number }; axis: DragAxis } | null>(null);
  const shadowDragRef = useRef<{ x: number; y: number } | null>(null);
  const fitInputRef = useRef<{ size: { width: number; height: number }; bounds: CanvasBounds } | null>(null);
  const fittedOpenKey = useRef<string | null>(null);
  const [image] = useImage(
    imageMode === "original" && originalFrameUrl ? originalFrameUrl : frameUrl ?? "",
    "anonymous",
  );
  const [previousImage] = useImage(previousFrameUrl ?? "", "anonymous");
  const [nextImage] = useImage(nextFrameUrl ?? "", "anonymous");
  const renderedImage = useMemo(() => tintedImage(image, frame?.transform.color), [frame?.transform.color, image]);
  const calibration = character.calibration ?? { sizeProfiles: [{ id: "default", name: "默认", unitMode: "pixels" as const, width: 512, height: 512 }], activeSizeProfileId: "default", pixelsPerUnit: CANONICAL_PIXELS_PER_UNIT, sizeGuideCenterX: 0, sizeGuideBottomY: 0, alignmentHorizonY: 0, shadowStandardY: 0, coreReference: null };
  const coreReference = calibration.coreReference;
  const [coreImage] = useImage(coreReference ? api.domainCoreReferenceUrl(character.id, coreReference.sha256) : "", "anonymous");
  const coreRect = coreReference ? coreReferenceRenderRect({ width: coreReference.width, height: coreReference.height, originX: coreReference.origin.x, originY: coreReference.origin.y, scale: coreReference.scale }) : null;
  const previousOnionImage = useMemo(() => tintedImage(previousImage, "#ff7897"), [previousImage]);
  const nextOnionImage = useMemo(() => tintedImage(nextImage, "#65d9ff"), [nextImage]);
  const profile = calibration.sizeProfiles.find((item) => item.id === calibration.activeSizeProfileId) ?? calibration.sizeProfiles[0];
  const profilePixels = sizeProfilePixels(profile, calibration.pixelsPerUnit || CANONICAL_PIXELS_PER_UNIT);
  const globalShadow = character.shadow ?? { enabled: true, color: "#000000", baseOpacity: 0.35, lightAngleDegrees: 135 };
  const resolvedShadow = frame ? {
    enabled: frame.transform.shadow.enabled ?? globalShadow.enabled,
    color: frame.transform.shadow.color ?? globalShadow.color,
    opacity: frame.transform.shadow.opacity ?? globalShadow.baseOpacity,
  } : { enabled: false, color: "#000000", opacity: 0 };

  useEffect(() => {
    if (!surface.current) return; const observer = new ResizeObserver(([entry]) => { setSize({ width: Math.max(320, entry.contentRect.width), height: Math.max(260, entry.contentRect.height) }); setSurfaceMeasured(true); });
    observer.observe(surface.current); return () => observer.disconnect();
  }, []);
  useEffect(() => {
    const down = (event: globalThis.KeyboardEvent) => {
      if (editable(event.target)) return;
      if (event.code === "Digit1" || event.code === "Numpad1") setMode("canvas");
      if (event.code === "Digit2" || event.code === "Numpad2") setMode("frame");
      if (event.code === "Digit3" || event.code === "Numpad3") setMode("shadow-x");
    };
    window.addEventListener("keydown", down); return () => window.removeEventListener("keydown", down);
  }, []);

  const imageWidth = image?.naturalWidth ?? 0; const imageHeight = image?.naturalHeight ?? 0;
  const sizeLeft = calibration.sizeGuideCenterX - profilePixels.width / 2; const sizeTop = calibration.sizeGuideBottomY - profilePixels.height;
  const guideCollisionTolerance = 0.5;
  const horizonCollides = Math.abs(calibration.alignmentHorizonY - calibration.shadowStandardY) <= guideCollisionTolerance || Math.abs(calibration.alignmentHorizonY - calibration.sizeGuideBottomY) <= guideCollisionTolerance;
  const shadowCollides = Math.abs(calibration.shadowStandardY - calibration.alignmentHorizonY) <= guideCollisionTolerance || Math.abs(calibration.shadowStandardY - calibration.sizeGuideBottomY) <= guideCollisionTolerance;
  const horizonDisplayY = calibration.alignmentHorizonY - (horizonCollides ? 3 / viewport.scale : 0);
  const shadowDisplayY = calibration.shadowStandardY + (shadowCollides ? 3 / viewport.scale : 0);
  const guideRight = sizeLeft + profilePixels.width;
  const guideLabelSize = 11 / viewport.scale;
  const guideStroke = CANVAS_GUIDE_WIDTHS.normal / viewport.scale;
  const haloStroke = CANVAS_GUIDE_WIDTHS.halo / viewport.scale;
  const contentBounds = useMemo(() => {
    const bounds: CanvasBounds[] = [{ left: sizeLeft, top: sizeTop, right: guideRight, bottom: calibration.sizeGuideBottomY }];
    if (frame && imageWidth && imageHeight) bounds.push(rotatedFrameBounds(imageWidth, imageHeight, frame.transform.position, frame.transform.scale, frame.transform.rotationDegrees));
    if (references.previous && previousFrame && previousImage) bounds.push(rotatedFrameBounds(previousImage.naturalWidth, previousImage.naturalHeight, previousFrame.transform.position, previousFrame.transform.scale, previousFrame.transform.rotationDegrees));
    if (references.next && nextFrame && nextImage) bounds.push(rotatedFrameBounds(nextImage.naturalWidth, nextImage.naturalHeight, nextFrame.transform.position, nextFrame.transform.scale, nextFrame.transform.rotationDegrees));
    if (references.core && coreRect) bounds.push({ left: coreRect.x, top: coreRect.y, right: coreRect.x + coreRect.width, bottom: coreRect.y + coreRect.height });
    return { left: Math.min(...bounds.map((item) => item.left)), top: Math.min(...bounds.map((item) => item.top)), right: Math.max(...bounds.map((item) => item.right)), bottom: Math.max(...bounds.map((item) => item.bottom)) };
  }, [calibration.sizeGuideBottomY, coreRect, frame, guideRight, imageHeight, imageWidth, nextFrame, nextImage, previousFrame, previousImage, references.core, references.next, references.previous, sizeLeft, sizeTop]);
  useEffect(() => {
    fitInputRef.current = { size, bounds: contentBounds };
  }, [contentBounds, size]);
  const fitViewport = useCallback(() => {
    const input = fitInputRef.current;
    if (input) setViewport(fitActionCanvasViewport(input.size, input.bounds));
  }, []);
  useEffect(() => {
    if (!surfaceMeasured || !openKey || fittedOpenKey.current === openKey) return;
    if (frame && (!imageWidth || !imageHeight)) return;
    const input = fitInputRef.current;
    if (!input) return;
    setViewport(fitActionCanvasViewport(input.size, input.bounds));
    fittedOpenKey.current = openKey;
  }, [contentBounds, frame, imageHeight, imageWidth, openKey, size, surfaceMeasured]);
  const changeMode = useCallback((nextMode: ActionCanvasModeV4) => setMode(nextMode), []);
  const changeImageMode = useCallback((nextMode: "original" | "result") => setImageMode(nextMode), []);
  const toggleReference = useCallback((key: "core" | "previous" | "next") => setReferences((value) => ({ ...value, [key]: !value[key] })), []);
  const toggleGuide = useCallback((key: "size" | "center" | "horizon" | "shadow") => setGuides((value) => ({ ...value, [key]: !value[key] })), []);
  const previousReferenceAvailable = playing ? playableCount > 1 : Boolean(previousFrame);
  const nextReferenceAvailable = playing ? playableCount > 1 : Boolean(nextFrame);
  const stableReferenceAvailability = useMemo(() => ({
    core: Boolean(coreReference),
    previous: previousReferenceAvailable,
    next: nextReferenceAvailable,
  }), [coreReference, nextReferenceAvailable, previousReferenceAvailable]);
  return <div className="action-canvas-v4" tabIndex={0} data-mode={mode} data-space-pan={navigation.spacePressed || undefined} onPointerCancel={navigation.cancelSpacePan}>
    <ActionCanvasToolbar mode={mode} selectedCount={selectedCount} viewportScale={viewport.scale} imageMode={imageMode} originalAvailable={playing ? hasPlayableOriginalFrame : Boolean(originalFrameUrl)} references={references} referenceAvailability={stableReferenceAvailability} guides={guides} onModeChange={changeMode} onFit={fitViewport} onImageModeChange={changeImageMode} onToggleReference={toggleReference} onToggleGuide={toggleGuide} />
    <div className="action-canvas-v4-surface" ref={surface}><Stage width={size.width} height={size.height} x={viewport.x} y={viewport.y} scaleX={viewport.scale} scaleY={viewport.scale}
      onMouseEnter={navigation.onCanvasEnter} onMouseLeave={navigation.onCanvasLeave}
      draggable={mode === "canvas" || navigation.spacePressed}
      onDragStart={(event) => { if (event.target !== event.target.getStage()) return; viewportDragRef.current = { origin: { x: event.target.x(), y: event.target.y() }, axis: null }; }}
      onDragMove={(event) => { if (event.target !== event.target.getStage()) return; const drag = viewportDragRef.current; if (!drag) return; const constrained = constrainDragPoint(drag.origin, { x: event.target.x(), y: event.target.y() }, event.evt.shiftKey, drag.axis); drag.axis = constrained.axis; event.target.position(constrained.point); }}
      onDragEnd={(event) => { if (event.target !== event.target.getStage()) return; const drag = viewportDragRef.current; const point = drag ? constrainDragPoint(drag.origin, { x: event.target.x(), y: event.target.y() }, event.evt.shiftKey, drag.axis).point : { x: event.target.x(), y: event.target.y() }; event.target.position(point); viewportDragRef.current = null; setViewport((current) => ({ ...current, x: point.x, y: point.y })); }}
      onWheel={(event) => { event.evt.preventDefault(); const pointer = event.target.getStage()?.getPointerPosition(); if (!pointer) return; const factor = event.evt.deltaY < 0 ? 1.1 : 1 / 1.1; setViewport((current) => zoomCanvasViewportAtPoint(current, pointer, factor)); }}>
      <Layer listening={false}>
        {references.core && coreImage && coreRect ? <KonvaImage image={coreImage} x={coreRect.x} y={coreRect.y} width={coreRect.width} height={coreRect.height} opacity={0.34} /> : null}
        {references.previous && previousFrame && previousOnionImage ? <Group x={previousFrame.transform.position.x} y={-previousFrame.transform.position.y} rotation={previousFrame.transform.rotationDegrees} scaleX={previousFrame.transform.scale.x} scaleY={previousFrame.transform.scale.y}><KonvaImage image={previousOnionImage} x={-previousImage!.naturalWidth / 2} y={-previousImage!.naturalHeight} width={previousImage!.naturalWidth} height={previousImage!.naturalHeight} opacity={0.3} /></Group> : null}
        {references.next && nextFrame && nextOnionImage ? <Group x={nextFrame.transform.position.x} y={-nextFrame.transform.position.y} rotation={nextFrame.transform.rotationDegrees} scaleX={nextFrame.transform.scale.x} scaleY={nextFrame.transform.scale.y}><KonvaImage image={nextOnionImage} x={-nextImage!.naturalWidth / 2} y={-nextImage!.naturalHeight} width={nextImage!.naturalWidth} height={nextImage!.naturalHeight} opacity={0.3} /></Group> : null}
      </Layer>
      <Layer listening={false}>
        {guides.size ? <><Rect x={sizeLeft} y={sizeTop} width={profilePixels.width} height={profilePixels.height} stroke={CANVAS_GUIDE_COLORS.halo} strokeWidth={haloStroke} opacity={.9} /><Rect x={sizeLeft} y={sizeTop} width={profilePixels.width} height={profilePixels.height} stroke={CANVAS_GUIDE_COLORS.size} dash={CANVAS_GUIDE_DASHES.size.map((value) => value / viewport.scale)} strokeWidth={guideStroke} fill="rgba(0,230,173,.035)" /><Text x={sizeLeft + 8 / viewport.scale} y={sizeTop + 8 / viewport.scale} text={`${profile.name} · ${Math.round(profilePixels.width)}×${Math.round(profilePixels.height)} px`} fill={CANVAS_GUIDE_COLORS.size} stroke={CANVAS_GUIDE_COLORS.halo} strokeWidth={2 / viewport.scale} fontSize={13 / viewport.scale} /></> : null}
        {guides.center ? <><Line points={[calibration.sizeGuideCenterX, sizeTop - 2000, calibration.sizeGuideCenterX, calibration.sizeGuideBottomY + 2000]} stroke={CANVAS_GUIDE_COLORS.halo} strokeWidth={haloStroke} opacity={.9} /><Line points={[calibration.sizeGuideCenterX, sizeTop - 2000, calibration.sizeGuideCenterX, calibration.sizeGuideBottomY + 2000]} stroke={CANVAS_GUIDE_COLORS.center} dash={CANVAS_GUIDE_DASHES.center.map((value) => value / viewport.scale)} strokeWidth={guideStroke} /><Text x={calibration.sizeGuideCenterX + 8 / viewport.scale} y={sizeTop - 24 / viewport.scale} text={`中轴 X ${calibration.sizeGuideCenterX}`} fill={CANVAS_GUIDE_COLORS.center} stroke={CANVAS_GUIDE_COLORS.halo} strokeWidth={2 / viewport.scale} fontSize={guideLabelSize} /></> : null}
        {guides.horizon ? <><Line points={[sizeLeft - 2000, horizonDisplayY, guideRight + 2000, horizonDisplayY]} stroke={CANVAS_GUIDE_COLORS.halo} strokeWidth={haloStroke} opacity={.9} /><Line points={[sizeLeft - 2000, horizonDisplayY, guideRight + 2000, horizonDisplayY]} stroke={CANVAS_GUIDE_COLORS.horizon} dash={CANVAS_GUIDE_DASHES.horizon.map((value) => value / viewport.scale)} strokeWidth={guideStroke} /><Text x={guideRight + 12 / viewport.scale} y={horizonDisplayY - 16 / viewport.scale} text={`地平线 Y ${calibration.alignmentHorizonY}`} fill={CANVAS_GUIDE_COLORS.horizon} stroke={CANVAS_GUIDE_COLORS.halo} strokeWidth={2 / viewport.scale} fontSize={guideLabelSize} /></> : null}
        {guides.shadow ? <><Line points={[sizeLeft - 2000, shadowDisplayY, guideRight + 2000, shadowDisplayY]} stroke={CANVAS_GUIDE_COLORS.halo} strokeWidth={haloStroke} opacity={.9} /><Line points={[sizeLeft - 2000, shadowDisplayY, guideRight + 2000, shadowDisplayY]} stroke={CANVAS_GUIDE_COLORS.shadowY} dash={CANVAS_GUIDE_DASHES.shadowY.map((value) => value / viewport.scale)} strokeWidth={guideStroke} /><Text x={guideRight + 12 / viewport.scale} y={shadowDisplayY + 5 / viewport.scale} text={`阴影线 Y ${calibration.shadowStandardY}`} fill={CANVAS_GUIDE_COLORS.shadowY} stroke={CANVAS_GUIDE_COLORS.halo} strokeWidth={2 / viewport.scale} fontSize={guideLabelSize} /></> : null}
      </Layer>
      {frame && renderedImage ? <Layer>
        {resolvedShadow.enabled && shadowPreview ? <Ellipse x={shadowPreview.positionPx[0]} y={-shadowPreview.positionPx[1]} radiusX={shadowPreview.widthPx / 2} radiusY={shadowPreview.depthPx / 2} rotation={-shadowPreview.rotationDegrees} fillRadialGradientStartPoint={{ x: 0, y: 0 }} fillRadialGradientStartRadius={0} fillRadialGradientEndPoint={{ x: 0, y: 0 }} fillRadialGradientEndRadius={Math.max(1, shadowPreview.widthPx / 2)} fillRadialGradientColorStops={[0, resolvedShadow.color, 1, `${resolvedShadow.color}00`]} opacity={shadowPreview.alpha} listening={!navigation.spacePressed && mode === "shadow-x"} draggable={!navigation.spacePressed && mode === "shadow-x"} onDragStart={(event) => { shadowDragRef.current = { x: event.target.x(), y: event.target.y() }; }} onDragMove={(event) => { const origin = shadowDragRef.current; if (origin) event.target.y(origin.y); }} onDragEnd={(event) => { const origin = shadowDragRef.current; if (!origin) return; const dx = event.target.x() - origin.x; event.target.position(origin); shadowDragRef.current = null; onShadowX(dx); }} /> : null}
        <Group x={frame.transform.position.x} y={-frame.transform.position.y} rotation={frame.transform.rotationDegrees} scaleX={frame.transform.scale.x} scaleY={frame.transform.scale.y}
          listening={!navigation.spacePressed && mode === "frame"} draggable={!navigation.spacePressed && mode === "frame"} onDragStart={(event) => { frameDragRef.current = { origin: { x: event.target.x(), y: event.target.y() }, axis: null }; }} onDragMove={(event) => { const drag = frameDragRef.current; if (!drag) return; const constrained = constrainDragPoint(drag.origin, { x: event.target.x(), y: event.target.y() }, event.evt.shiftKey, drag.axis); drag.axis = constrained.axis; event.target.position(constrained.point); }} onDragEnd={(event) => { const drag = frameDragRef.current; if (!drag) return; const point = constrainDragPoint(drag.origin, { x: event.target.x(), y: event.target.y() }, event.evt.shiftKey, drag.axis).point; const dx = point.x - drag.origin.x; const dy = point.y - drag.origin.y; event.target.position(drag.origin); frameDragRef.current = null; onMove(dx, -dy); }}>
          <KonvaImage image={renderedImage} x={-imageWidth / 2} y={-imageHeight} width={imageWidth} height={imageHeight} opacity={frame.transform.opacity} />
        </Group>
      </Layer> : null}
    </Stage></div>
    {!frame ? <div className="action-canvas-v4-empty">把素材帧拖入时间轴</div> : null}
  </div>;
}
