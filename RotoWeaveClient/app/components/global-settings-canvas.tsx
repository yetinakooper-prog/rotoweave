import { useEffect, useMemo, useRef, useState } from "react";
import {
  Circle,
  Group,
  Image as KonvaImage,
  Layer,
  Line,
  Rect,
  Stage,
  Text,
} from "react-konva";
import useImage from "use-image";

import { mediaUrl } from "../lib/api";
import {
  CANVAS_GUIDE_COLORS,
  CANVAS_GUIDE_DASHES,
  CANVAS_GUIDE_WIDTHS,
} from "../lib/canvas-guide-style";
import {
  constrainDragPoint,
  type DragAxis,
} from "../lib/drag-constraint";
import { zoomCanvasViewportAtPoint } from "../lib/canvas-viewport";
import {
  coreReferenceOriginFromRender,
  coreReferenceRenderRect,
  resolveCoreReferenceScale,
} from "../lib/core-reference-scale.mjs";
import type { CoreReference, ShadowPreview, SizeProfile } from "../lib/types";
import { useCanvasSpacePan } from "../lib/use-canvas-navigation";
import { useSessionViewport } from "../lib/use-session-viewport";

type Point = { x: number; y: number };
type Bounds = { left: number; top: number; right: number; bottom: number };
export type GlobalCanvasDragMode =
  | "size"
  | "horizon"
  | "image"
  | "shadow"
  | "viewport";

type GlobalSettingsCanvasProps = {
  sessionKey: string;
  alignmentHorizonY: number;
  shadowStandardY: number | null;
  sizeGuideCenterX: number;
  sizeGuideBottomY: number;
  sizeProfile?: SizeProfile | null;
  coreReference?: CoreReference | null;
  coreScale?: number;
  coreOrigin?: Point;
  shadow?: ShadowPreview | null;
  shadowColor?: string;
  dragMode: GlobalCanvasDragMode;
  guideVisibility?: { size: boolean; center: boolean; horizon: boolean; shadow: boolean };
  onAlignmentHorizonChange: (y: number) => void;
  onShadowStandardYChange: (y: number) => void;
  onSizeGuidePositionChange: (point: Point) => void;
  onCoreOriginChange: (point: Point) => void;
  onCoreOriginCommit: (point: Point) => void;
};

function clamp(value: number, minimum: number, maximum: number): number {
  return Math.max(minimum, Math.min(maximum, value));
}

function union(bounds: Bounds[]): Bounds {
  if (!bounds.length) return { left: -180, top: -260, right: 180, bottom: 80 };
  return {
    left: Math.min(...bounds.map((item) => item.left)),
    top: Math.min(...bounds.map((item) => item.top)),
    right: Math.max(...bounds.map((item) => item.right)),
    bottom: Math.max(...bounds.map((item) => item.bottom)),
  };
}

function fitBounds(stageSize: { width: number; height: number }, bounds: Bounds) {
  const padding = 72;
  const width = Math.max(1, bounds.right - bounds.left);
  const height = Math.max(1, bounds.bottom - bounds.top);
  const scale = clamp(
    Math.min(
      (stageSize.width - padding) / width,
      (stageSize.height - padding) / height,
    ),
    0.02,
    8,
  );
  return {
    scale,
    x: stageSize.width / 2 - ((bounds.left + bounds.right) / 2) * scale,
    y: stageSize.height / 2 - ((bounds.top + bounds.bottom) / 2) * scale,
  };
}

function rgba(color: string, alpha: number): string {
  const normalized = color.trim().replace("#", "");
  const expanded =
    normalized.length === 3
      ? normalized
          .split("")
          .map((part) => part + part)
          .join("")
      : normalized;
  const value = Number.parseInt(expanded, 16);
  if (!Number.isFinite(value)) return `rgba(0,0,0,${alpha})`;
  return `rgba(${(value >> 16) & 255},${(value >> 8) & 255},${value & 255},${alpha})`;
}

function rounded(value: number): number {
  return Math.round(value * 10) / 10;
}

export function GlobalSettingsCanvas({
  sessionKey,
  alignmentHorizonY,
  shadowStandardY,
  sizeGuideCenterX,
  sizeGuideBottomY,
  sizeProfile,
  coreReference,
  coreScale,
  coreOrigin,
  shadow,
  shadowColor = "#000000",
  dragMode,
  guideVisibility = { size: true, center: true, horizon: true, shadow: true },
  onAlignmentHorizonChange,
  onShadowStandardYChange,
  onSizeGuidePositionChange,
  onCoreOriginChange,
  onCoreOriginCommit,
}: GlobalSettingsCanvasProps) {
  const hostRef = useRef<HTMLDivElement>(null);
  const navigation = useCanvasSpacePan(true);
  const viewportDragRef = useRef<{ origin: Point; axis: DragAxis } | null>(null);
  const coreDragRef = useRef<{ origin: Point; axis: DragAxis } | null>(null);
  const sizeDragRef = useRef<{ origin: Point; axis: DragAxis } | null>(null);
  const [stageSize, setStageSize] = useState({ width: 760, height: 560 });
  const [hostMeasured, setHostMeasured] = useState(false);
  const [coreImage] = useImage(mediaUrl(coreReference?.url), "anonymous");

  const renderedCoreScale = resolveCoreReferenceScale(
    coreScale ?? coreReference?.scale ?? 1,
  );
  const guideWidth = (sizeProfile?.width_world ?? 0) * 100;
  const guideHeight = (sizeProfile?.height_world ?? 0) * 100;
  const resolvedCoreOrigin = coreOrigin ?? {
    x: coreReference?.origin_x ?? 0,
    y: coreReference?.origin_y ?? 0,
  };
  const coreRect = coreReference
    ? coreReferenceRenderRect({
        width: coreReference.width,
        height: coreReference.height,
        originX: resolvedCoreOrigin.x,
        originY: resolvedCoreOrigin.y,
        scale: renderedCoreScale,
      })
    : null;
  const coreX = coreRect?.x ?? 0;
  const coreY = coreRect?.y ?? 0;
  const shadowX = shadow?.positionPx[0] ?? 0;
  const shadowY = shadow ? -shadow.positionPx[1] : 0;

  const contentBounds = useMemo(() => {
    const values: Bounds[] = [
      { left: -16, top: -16, right: 16, bottom: 16 },
      {
        left: -120,
        top: alignmentHorizonY - 1,
        right: 120,
        bottom: alignmentHorizonY + 1,
      },
    ];
    if (shadowStandardY !== null) {
      values.push({
        left: -120,
        top: shadowStandardY - 1,
        right: 120,
        bottom: shadowStandardY + 1,
      });
    }
    if (sizeProfile) {
      values.push({
        left: sizeGuideCenterX - guideWidth / 2,
        top: sizeGuideBottomY - guideHeight,
        right: sizeGuideCenterX + guideWidth / 2,
        bottom: sizeGuideBottomY,
      });
    }
    if (coreReference) {
      values.push({
        left: coreX,
        top: coreY,
        right: coreX + (coreRect?.width ?? 0),
        bottom: coreY + (coreRect?.height ?? 0),
      });
    }
    if (shadow) {
      values.push({
        left: shadowX - shadow.widthPx / 2,
        top: shadowY - shadow.depthPx / 2,
        right: shadowX + shadow.widthPx / 2,
        bottom: shadowY + shadow.depthPx / 2,
      });
    }
    const result = union(values);
    return {
      left: result.left - 40,
      top: result.top - 40,
      right: result.right + 40,
      bottom: result.bottom + 40,
    };
  }, [
    alignmentHorizonY,
    coreReference,
    coreRect?.height,
    coreRect?.width,
    coreX,
    coreY,
    guideHeight,
    guideWidth,
    shadow,
    shadowStandardY,
    shadowX,
    shadowY,
    sizeGuideBottomY,
    sizeGuideCenterX,
    sizeProfile,
  ]);

  const initialViewport = useMemo(
    () => fitBounds({ width: 760, height: 560 }, contentBounds),
    [contentBounds],
  );
  const [viewport, setViewport] = useSessionViewport(
    `global:${sessionKey}`,
    initialViewport,
  );
  const contentBoundsRef = useRef(contentBounds);
  const fittedOpenKey = useRef<string | null>(null);

  useEffect(() => {
    contentBoundsRef.current = contentBounds;
  }, [contentBounds]);

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    const observer = new ResizeObserver(([entry]) => {
      setStageSize({
        width: Math.max(240, Math.floor(entry.contentRect.width)),
        height: Math.max(240, Math.floor(entry.contentRect.height)),
      });
      setHostMeasured(true);
    });
    observer.observe(host);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    if (!hostMeasured || fittedOpenKey.current === sessionKey) return;
    const frame = window.requestAnimationFrame(() => {
      setViewport(
        fitBounds(
          { width: stageSize.width, height: stageSize.height },
          contentBoundsRef.current,
        ),
      );
      fittedOpenKey.current = sessionKey;
    });
    return () => window.cancelAnimationFrame(frame);
  }, [
    hostMeasured,
    sessionKey,
    setViewport,
    stageSize.height,
    stageSize.width,
  ]);

  function zoomAtCenter(multiplier: number) {
    setViewport((current) => {
      const nextScale = clamp(current.scale * multiplier, 0.02, 50);
      const center = { x: stageSize.width / 2, y: stageSize.height / 2 };
      const local = {
        x: (center.x - current.x) / current.scale,
        y: (center.y - current.y) / current.scale,
      };
      return {
        scale: nextScale,
        x: center.x - local.x * nextScale,
        y: center.y - local.y * nextScale,
      };
    });
  }

  const lineLeft = contentBounds.left - 600 / viewport.scale;
  const lineRight = contentBounds.right + 600 / viewport.scale;
  const visibleRight = (stageSize.width - viewport.x) / viewport.scale;
  const guideLabelWidth = 112 / viewport.scale;
  const guideLabelHeight = 20 / viewport.scale;
  const guideLabelX = visibleRight - guideLabelWidth - 10 / viewport.scale;
  const guideStrokeWidth = (active: boolean) =>
    (active ? CANVAS_GUIDE_WIDTHS.active : CANVAS_GUIDE_WIDTHS.normal) /
    viewport.scale;
  const guideCollisionTolerance = 0.5;
  const horizonCollides =
    Math.abs(alignmentHorizonY - sizeGuideBottomY) <= guideCollisionTolerance ||
    (shadowStandardY !== null &&
      Math.abs(alignmentHorizonY - shadowStandardY) <= guideCollisionTolerance);
  const shadowCollides =
    shadowStandardY !== null &&
    (Math.abs(shadowStandardY - sizeGuideBottomY) <= guideCollisionTolerance ||
      Math.abs(shadowStandardY - alignmentHorizonY) <= guideCollisionTolerance);
  const horizonDisplayOffset = horizonCollides ? -3 / viewport.scale : 0;
  const shadowDisplayOffset = shadowCollides ? 3 / viewport.scale : 0;
  const gridStep = 100;
  const gridLeft = Math.floor(contentBounds.left / gridStep) * gridStep - gridStep * 2;
  const gridRight = Math.ceil(contentBounds.right / gridStep) * gridStep + gridStep * 2;
  const gridTop = Math.floor(contentBounds.top / gridStep) * gridStep - gridStep * 2;
  const gridBottom = Math.ceil(contentBounds.bottom / gridStep) * gridStep + gridStep * 2;
  const verticalGrid = Array.from(
    { length: Math.min(160, Math.max(0, Math.floor((gridRight - gridLeft) / gridStep) + 1)) },
    (_, index) => gridLeft + index * gridStep,
  );
  const horizontalGrid = Array.from(
    { length: Math.min(160, Math.max(0, Math.floor((gridBottom - gridTop) / gridStep) + 1)) },
    (_, index) => gridTop + index * gridStep,
  );

  return (
    <div
      ref={hostRef}
      className="global-settings-canvas"
      data-tool={dragMode}
      data-space-pan={navigation.spacePressed || undefined}
      onPointerCancel={navigation.cancelSpacePan}
    >
      <div className="global-settings-canvas-controls" aria-label="角色空间缩放控制">
        <button type="button" aria-label="缩小" onClick={() => zoomAtCenter(0.8)}>
          −
        </button>
        <output>{Math.round(viewport.scale * 100)}%</output>
        <button type="button" aria-label="放大" onClick={() => zoomAtCenter(1.25)}>
          ＋
        </button>
        <button type="button" onClick={() => setViewport(fitBounds(stageSize, contentBounds))}>
          适配
        </button>
      </div>
      <Stage
        width={stageSize.width}
        height={stageSize.height}
        onMouseEnter={navigation.onCanvasEnter}
        onMouseLeave={navigation.onCanvasLeave}
        draggable={dragMode === "viewport" || navigation.spacePressed}
        x={viewport.x}
        y={viewport.y}
        scaleX={viewport.scale}
        scaleY={viewport.scale}
        onDragStart={(event) => {
          if (event.target !== event.target.getStage()) return;
          viewportDragRef.current = {
            origin: { x: event.target.x(), y: event.target.y() },
            axis: null,
          };
        }}
        onDragMove={(event) => {
          if (event.target !== event.target.getStage()) return;
          const drag = viewportDragRef.current;
          if (!drag) return;
          const constrained = constrainDragPoint(
            drag.origin,
            { x: event.target.x(), y: event.target.y() },
            event.evt.shiftKey,
            drag.axis,
          );
          drag.axis = constrained.axis;
          event.target.position(constrained.point);
        }}
        onDragEnd={(event) => {
          if (event.target !== event.target.getStage()) return;
          const drag = viewportDragRef.current;
          const constrained = drag
            ? constrainDragPoint(
                drag.origin,
                { x: event.target.x(), y: event.target.y() },
                event.evt.shiftKey,
                drag.axis,
              ).point
            : { x: event.target.x(), y: event.target.y() };
          event.target.position(constrained);
          viewportDragRef.current = null;
          setViewport((current) => ({
            ...current,
            x: constrained.x,
            y: constrained.y,
          }));
        }}
        onWheel={(event) => {
          event.evt.preventDefault();
          const pointer = event.target.getStage()?.getPointerPosition();
          if (!pointer) return;
          setViewport((current) => zoomCanvasViewportAtPoint(
            current,
            pointer,
            event.evt.deltaY > 0 ? 0.9 : 1.1,
          ));
        }}
      >
        <Layer>
          {verticalGrid.map((x) => (
            <Line
              key={`v-${x}`}
              points={[x, gridTop, x, gridBottom]}
              stroke={x === 0 ? "rgba(255,209,102,.28)" : "rgba(122,161,143,.10)"}
              strokeWidth={(x === 0 ? 1.5 : 1) / viewport.scale}
              listening={false}
            />
          ))}
          {horizontalGrid.map((y) => (
            <Line
              key={`h-${y}`}
              points={[gridLeft, y, gridRight, y]}
              stroke={y === 0 ? "rgba(255,209,102,.28)" : "rgba(122,161,143,.10)"}
              strokeWidth={(y === 0 ? 1.5 : 1) / viewport.scale}
              listening={false}
            />
          ))}

          {shadow ? (
            <Group
              x={shadowX}
              y={shadowY}
              rotation={-shadow.rotationDegrees}
              opacity={shadow.alpha}
              listening={false}
            >
              <Circle
                radius={Math.max(0.5, shadow.widthPx / 2)}
                scaleY={Math.max(0.02, shadow.depthPx / Math.max(shadow.widthPx, 0.001))}
                fillRadialGradientStartPoint={{ x: 0, y: 0 }}
                fillRadialGradientStartRadius={0}
                fillRadialGradientEndPoint={{ x: 0, y: 0 }}
                fillRadialGradientEndRadius={Math.max(0.5, shadow.widthPx / 2)}
                fillRadialGradientColorStops={[
                  0,
                  rgba(shadowColor, 1),
                  0.58,
                  rgba(shadowColor, 0.72),
                  1,
                  rgba(shadowColor, 0),
                ]}
              />
            </Group>
          ) : null}

          {coreReference && coreImage ? (
            <KonvaImage
              image={coreImage}
              x={coreX}
              y={coreY}
              width={coreRect?.width ?? 0}
              height={coreRect?.height ?? 0}
              draggable={dragMode === "image" && !navigation.spacePressed}
              listening={dragMode === "image" && !navigation.spacePressed}
              onDragStart={(event) => {
                coreDragRef.current = {
                  origin: { x: event.target.x(), y: event.target.y() },
                  axis: null,
                };
              }}
              onDragMove={(event) => {
                const drag = coreDragRef.current;
                if (!drag) return;
                const constrained = constrainDragPoint(
                  drag.origin,
                  { x: event.target.x(), y: event.target.y() },
                  event.evt.shiftKey,
                  drag.axis,
                );
                drag.axis = constrained.axis;
                event.target.position(constrained.point);
                const origin = coreReferenceOriginFromRender(
                  constrained.point.x,
                  constrained.point.y,
                  renderedCoreScale,
                );
                onCoreOriginChange({ x: rounded(origin.x), y: rounded(origin.y) });
              }}
              onDragEnd={(event) => {
                const drag = coreDragRef.current;
                const constrained = drag
                  ? constrainDragPoint(
                      drag.origin,
                      { x: event.target.x(), y: event.target.y() },
                      event.evt.shiftKey,
                      drag.axis,
                    ).point
                  : { x: event.target.x(), y: event.target.y() };
                event.target.position(constrained);
                coreDragRef.current = null;
                const origin = coreReferenceOriginFromRender(
                  constrained.x,
                  constrained.y,
                  renderedCoreScale,
                );
                onCoreOriginCommit({ x: rounded(origin.x), y: rounded(origin.y) });
              }}
            />
          ) : null}

          {guideVisibility.center ? (
            <Group listening={false}>
              <Line
                points={[sizeGuideCenterX, gridTop, sizeGuideCenterX, gridBottom]}
                stroke={CANVAS_GUIDE_COLORS.halo}
                strokeWidth={CANVAS_GUIDE_WIDTHS.halo / viewport.scale}
                opacity={0.88}
              />
              <Line
                points={[sizeGuideCenterX, gridTop, sizeGuideCenterX, gridBottom]}
                stroke={CANVAS_GUIDE_COLORS.center}
                dash={CANVAS_GUIDE_DASHES.center.map((value) => value / viewport.scale)}
                strokeWidth={CANVAS_GUIDE_WIDTHS.normal / viewport.scale}
              />
              <Text
                x={sizeGuideCenterX + 8 / viewport.scale}
                y={contentBounds.top + 8 / viewport.scale}
                text={`中轴 X ${sizeGuideCenterX}`}
                fill={CANVAS_GUIDE_COLORS.center}
                stroke={CANVAS_GUIDE_COLORS.halo}
                strokeWidth={2 / viewport.scale}
                fontSize={11 / viewport.scale}
              />
            </Group>
          ) : null}

          {guideVisibility.size && sizeProfile ? (
            <Group
              x={sizeGuideCenterX}
              y={sizeGuideBottomY}
              draggable={dragMode === "size" && !navigation.spacePressed}
              listening={dragMode === "size" && !navigation.spacePressed}
              onDragStart={(event) => {
                sizeDragRef.current = {
                  origin: { x: event.target.x(), y: event.target.y() },
                  axis: null,
                };
              }}
              onDragMove={(event) => {
                const drag = sizeDragRef.current;
                if (!drag) return;
                const constrained = constrainDragPoint(
                  drag.origin,
                  { x: event.target.x(), y: event.target.y() },
                  event.evt.shiftKey,
                  drag.axis,
                );
                drag.axis = constrained.axis;
                event.target.position(constrained.point);
                onSizeGuidePositionChange({
                  x: rounded(constrained.point.x),
                  y: rounded(constrained.point.y),
                });
              }}
              onDragEnd={(event) => {
                const drag = sizeDragRef.current;
                const constrained = drag
                  ? constrainDragPoint(
                      drag.origin,
                      { x: event.target.x(), y: event.target.y() },
                      event.evt.shiftKey,
                      drag.axis,
                    ).point
                  : { x: event.target.x(), y: event.target.y() };
                event.target.position(constrained);
                sizeDragRef.current = null;
                onSizeGuidePositionChange({
                  x: rounded(constrained.x),
                  y: rounded(constrained.y),
                });
              }}
            >
              <Rect
                x={-guideWidth / 2}
                y={-guideHeight}
                width={guideWidth}
                height={guideHeight}
                stroke={CANVAS_GUIDE_COLORS.halo}
                strokeWidth={CANVAS_GUIDE_WIDTHS.halo / viewport.scale}
                opacity={0.88}
              />
              <Rect
                x={-guideWidth / 2}
                y={-guideHeight}
                width={guideWidth}
                height={guideHeight}
                stroke={CANVAS_GUIDE_COLORS.size}
                dash={CANVAS_GUIDE_DASHES.size.map((value) => value / viewport.scale)}
                strokeWidth={guideStrokeWidth(dragMode === "size")}
                fill="rgba(0,230,173,.035)"
              />
              {[
                [-guideWidth / 2, -guideHeight],
                [guideWidth / 2, -guideHeight],
                [-guideWidth / 2, 0],
                [guideWidth / 2, 0],
              ].map(([x, y], index) => (
                <Circle
                  key={`size-corner-${index}`}
                  x={x}
                  y={y}
                  radius={4 / viewport.scale}
                  fill={CANVAS_GUIDE_COLORS.size}
                  stroke={CANVAS_GUIDE_COLORS.halo}
                  strokeWidth={1.75 / viewport.scale}
                />
              ))}
              <Circle
                radius={5 / viewport.scale}
                fill={CANVAS_GUIDE_COLORS.size}
                stroke={CANVAS_GUIDE_COLORS.halo}
                strokeWidth={2 / viewport.scale}
              />
            </Group>
          ) : null}

          {guideVisibility.horizon ? <Group
            x={0}
            y={alignmentHorizonY}
            draggable={dragMode === "horizon" && !navigation.spacePressed}
            listening={dragMode === "horizon" && !navigation.spacePressed}
            onDragMove={(event) => {
              event.target.x(0);
              onAlignmentHorizonChange(rounded(event.target.y()));
            }}
            onDragEnd={(event) => {
              event.target.x(0);
              onAlignmentHorizonChange(rounded(event.target.y()));
            }}
          >
            <Group y={horizonDisplayOffset}>
            <Rect
              x={lineLeft}
              y={-9 / viewport.scale}
              width={lineRight - lineLeft}
              height={18 / viewport.scale}
              fill="rgba(0,0,0,0.001)"
            />
            <Line
              points={[lineLeft, 0, lineRight, 0]}
              stroke={CANVAS_GUIDE_COLORS.halo}
              strokeWidth={CANVAS_GUIDE_WIDTHS.halo / viewport.scale}
              opacity={0.88}
            />
            <Line
              points={[lineLeft, 0, lineRight, 0]}
              stroke={CANVAS_GUIDE_COLORS.horizon}
              dash={CANVAS_GUIDE_DASHES.horizon.map((value) => value / viewport.scale)}
              strokeWidth={guideStrokeWidth(dragMode === "horizon")}
            />
            <Circle
              radius={5 / viewport.scale}
              fill={CANVAS_GUIDE_COLORS.horizon}
              stroke={CANVAS_GUIDE_COLORS.halo}
              strokeWidth={2 / viewport.scale}
            />
            <Rect
              x={guideLabelX}
              y={-guideLabelHeight - 5 / viewport.scale}
              width={guideLabelWidth}
              height={guideLabelHeight}
              cornerRadius={5 / viewport.scale}
              fill="rgba(16,20,24,.9)"
              stroke={CANVAS_GUIDE_COLORS.horizon}
              strokeWidth={1 / viewport.scale}
            />
            <Text
              x={guideLabelX + 8 / viewport.scale}
              y={-guideLabelHeight / 2 - 5 / viewport.scale}
              width={guideLabelWidth - 16 / viewport.scale}
              text="对齐地平线"
              fontSize={10 / viewport.scale}
              fill={CANVAS_GUIDE_COLORS.label}
              align="center"
              verticalAlign="middle"
            />
            </Group>
          </Group> : null}

          {guideVisibility.shadow && shadowStandardY !== null ? (
            <Group
              x={0}
              y={shadowStandardY}
              draggable={dragMode === "shadow" && !navigation.spacePressed}
              listening={dragMode === "shadow" && !navigation.spacePressed}
              onDragMove={(event) => {
                event.target.x(0);
                onShadowStandardYChange(rounded(event.target.y()));
              }}
              onDragEnd={(event) => {
                event.target.x(0);
                onShadowStandardYChange(rounded(event.target.y()));
              }}
            >
              <Group y={shadowDisplayOffset}>
              <Rect
                x={lineLeft}
                y={-9 / viewport.scale}
                width={lineRight - lineLeft}
                height={18 / viewport.scale}
                fill="rgba(0,0,0,0.001)"
              />
              <Line
                points={[lineLeft, 0, lineRight, 0]}
                stroke={CANVAS_GUIDE_COLORS.halo}
                strokeWidth={CANVAS_GUIDE_WIDTHS.halo / viewport.scale}
                opacity={0.88}
              />
              <Line
                points={[lineLeft, 0, lineRight, 0]}
                stroke={CANVAS_GUIDE_COLORS.shadowY}
                dash={CANVAS_GUIDE_DASHES.shadowY.map((value) => value / viewport.scale)}
                strokeWidth={guideStrokeWidth(dragMode === "shadow")}
              />
              <Rect
                x={-5 / viewport.scale}
                y={-5 / viewport.scale}
                width={10 / viewport.scale}
                height={10 / viewport.scale}
                rotation={45}
                offsetX={5 / viewport.scale}
                offsetY={5 / viewport.scale}
                fill={CANVAS_GUIDE_COLORS.shadowY}
                stroke={CANVAS_GUIDE_COLORS.halo}
                strokeWidth={2 / viewport.scale}
              />
              <Rect
                x={guideLabelX}
                y={5 / viewport.scale}
                width={guideLabelWidth}
                height={guideLabelHeight}
                cornerRadius={5 / viewport.scale}
                fill="rgba(16,20,24,.9)"
                stroke={CANVAS_GUIDE_COLORS.shadowY}
                strokeWidth={1 / viewport.scale}
              />
              <Text
                x={guideLabelX + 8 / viewport.scale}
                y={5 / viewport.scale}
                width={guideLabelWidth - 16 / viewport.scale}
                height={guideLabelHeight}
                text="阴影标准 Y"
                fontSize={10 / viewport.scale}
                fill={CANVAS_GUIDE_COLORS.label}
                align="center"
                verticalAlign="middle"
              />
              </Group>
            </Group>
          ) : null}

          <Group listening={false}>
            <Line
              points={[-18 / viewport.scale, 0, 18 / viewport.scale, 0]}
              stroke="#ffd166"
              strokeWidth={2 / viewport.scale}
            />
            <Line
              points={[0, -18 / viewport.scale, 0, 18 / viewport.scale]}
              stroke="#ffd166"
              strokeWidth={2 / viewport.scale}
            />
            <Rect
              x={-3 / viewport.scale}
              y={-3 / viewport.scale}
              width={6 / viewport.scale}
              height={6 / viewport.scale}
              fill="#ffd166"
            />
            <Text
              x={8 / viewport.scale}
              y={8 / viewport.scale}
              text="Unity Pivot (0,0)"
              fontSize={10 / viewport.scale}
              fill="#ffe29a"
            />
          </Group>
        </Layer>
      </Stage>
      <span className="global-settings-canvas-hint">
        滚轮缩放 · Shift 限制水平/垂直 · 当前工具：
        {dragMode === "size"
          ? "拖动全局尺寸框"
          : dragMode === "horizon"
            ? "拖动对齐地平线"
            : dragMode === "image"
              ? "拖动核心形象图"
              : dragMode === "shadow"
                ? "拖动阴影标准 Y"
                : "拖动画布视野"}
      </span>
    </div>
  );
}
