import {
  ChevronDown,
  ChevronRight,
  Copy,
  Film,
  Layers3,
  LocateFixed,
  Loader2,
  Pause,
  Play,
  Plus,
  Repeat2,
  RotateCcw,
  Save,
  Trash2,
  X,
} from "lucide-react";
import { memo, useCallback, useEffect, useMemo, useRef, useState, type DragEvent, type KeyboardEvent, type MouseEvent } from "react";
/* eslint-disable react-hooks/set-state-in-effect -- editor drafts intentionally reset when the selected saved action changes. */
import { useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "../lib/api";
import { ActionCanvasV4 } from "../components/action-canvas-v4";
import { NumericDraftInput } from "../components/numeric-draft-input";
import { PercentDraftInputV4 } from "../components/percent-draft-input-v4";
import {
  adjacentEnabledFrameIndices,
  buildShadowPreviewRequest,
  countCharacterFrameUsage,
  createPlaybackTimeline,
  distributeEnabledFrameDurations,
  frameIndexAtTimelineTime,
  inheritAllFrameShadows,
  moveFrameBlockAtBoundary,
  normalizeFrameDurations,
  resolveCanvasFocusIndex,
  selectFrameIndex,
  setFramesEnabled,
  shadowEnabledMode,
  shadowEnabledOverride,
  shouldRequestShadowPreview,
  type ShadowPreviewRequest,
} from "../lib/action-editor-v4";
import { projectLatestMaterialFrames } from "../lib/material-manager-v4";
import { usePageSaveCommandV4 } from "../lib/page-save-command-v4";
import { mergeDomainActionV4 } from "../lib/domain-cache-v4";
import { timelineEdgeVelocityV4 } from "../lib/timeline-edge-autoscroll-v4";
import type {
  ActionFrameRefV4,
  ActionFrameTransformV4,
  DomainActionV4,
  MaterialVariantV4,
  ShadowPreview,
} from "../lib/types";
import { useAutoDismissNoticeV4 } from "../lib/use-auto-dismiss-notice-v4";


const defaultTransform = (): ActionFrameTransformV4 => ({
  position: { x: 0, y: 0 },
  scale: { x: 1, y: 1 },
  rotationDegrees: 0,
  color: "#ffffff",
  opacity: 1,
  shadow: {
    enabled: null,
    color: null,
    opacity: null,
    offset: { x: 0, y: 0 },
    scale: { x: 1, y: 1 },
  },
});

const TIMELINE_DRAG_TYPE = "application/x-rotoweave-timeline-indices";

function cloneFrames(frames: ActionFrameRefV4[]): ActionFrameRefV4[] {
  return structuredClone(frames);
}

function draftId(): string {
  return `afrm-draft-${crypto.randomUUID()}`;
}

type LibraryFrame = {
  key: string;
  variant: MaterialVariantV4;
  frameIndex: number;
  frameId: string;
  sourceIndex: number;
  sourceName: string;
};

type LibraryGroup = {
  key: string;
  sourceId: string;
  sourceName: string;
  frames: LibraryFrame[];
  historical: boolean;
};

type ActionEditorV4Props = {
  mode?: "overlay" | "page";
  onClose?: () => void;
  characterId?: string | null;
  actionId?: string | null;
  onActionChange?: (actionId: string | null) => void;
  onDirtyChange?: (dirty: boolean) => void;
};

type TimelineFrameListProps = {
  draft: ActionFrameRefV4[];
  selection: Set<number>;
  canvasFocusedFrameId: string | null;
  playing: boolean;
  variantById: Map<string, MaterialVariantV4>;
  timelineInsertionBoundary: number | null;
  onDragStart: (event: DragEvent<HTMLElement>, index: number) => void;
  onDragEnd: () => void;
  onSelect: (index: number, event: MouseEvent) => void;
  onLocate: (frame: ActionFrameRefV4) => void;
};

const TimelineFrameList = memo(function TimelineFrameList({ draft, selection, canvasFocusedFrameId, playing, variantById, timelineInsertionBoundary, onDragStart, onDragEnd, onSelect, onLocate }: TimelineFrameListProps) {
  return <>{draft.map((frame, index) => {
    const variant = variantById.get(frame.variantId);
    const variantIndex = variant?.frames.findIndex((item) => item.id === frame.frameId) ?? -1;
    return <article
      key={frame.id}
      data-timeline-index={index}
      draggable
      className={`${selection.has(index) ? "selected" : ""} ${!playing && canvasFocusedFrameId === frame.id ? "canvas-focused" : ""} ${frame.enabled === false ? "disabled-frame" : "enabled-frame"} ${timelineInsertionBoundary === index ? "timeline-drop-before" : ""}`}
      onDragStart={(event) => onDragStart(event, index)}
      onDragEnd={onDragEnd}
    ><button type="button" className="timeline-frame-select" onClick={(event) => onSelect(index, event)} aria-pressed={selection.has(index)} aria-current={!playing && canvasFocusedFrameId === frame.id ? "true" : undefined}>{variant && variantIndex >= 0 ? <img src={api.materialVariantFrameUrl(variant.id, variantIndex)} alt="" /> : null}<span className="timeline-frame-summary"><strong>时间轴 {String(index + 1).padStart(3, "0")}</strong><em>{Math.round(frame.durationSeconds * 1000)} ms</em></span></button><button type="button" className="timeline-locate-button" aria-label="定位到素材帧" disabled={!variant || variantIndex < 0} draggable={false} onPointerDown={(event) => event.stopPropagation()} onClick={(event) => { event.stopPropagation(); onLocate(frame); }} title="在当前动作页素材帧库中展开并选中此帧"><LocateFixed size={14} /></button></article>;
  })}<div className={`action-editor-timeline-drop ${timelineInsertionBoundary === draft.length ? "timeline-drop-before" : ""}`}>拖入素材帧</div></>;
}, (previous, next) => (
  previous.draft === next.draft
  && previous.selection === next.selection
  && previous.canvasFocusedFrameId === next.canvasFocusedFrameId
  && previous.playing === next.playing
  && previous.variantById === next.variantById
  && previous.timelineInsertionBoundary === next.timelineInsertionBoundary
));

export function ActionEditorV4({
  mode = "overlay",
  onClose,
  characterId: controlledCharacterId,
  actionId: controlledActionId,
  onActionChange,
  onDirtyChange,
}: ActionEditorV4Props) {
  const domainQuery = useQuery({ queryKey: ["domain-v4"], queryFn: api.domainV4 });
  const queryClient = useQueryClient();
  const domain = domainQuery.data;
  const [localCharacterId, setLocalCharacterId] = useState<string | null>(null);
  const [localActionId, setLocalActionId] = useState<string | null>(null);
  const [draft, setDraft] = useState<ActionFrameRefV4[]>([]);
  const [serverBaseline, setServerBaseline] = useState<ActionFrameRefV4[]>([]);
  const [selection, setSelection] = useState(new Set<number>());
  const [selectionAnchor, setSelectionAnchor] = useState<number | null>(null);
  const [focusedFrameId, setFocusedFrameId] = useState<string | null>(null);
  const [timelineInsertionBoundary, setTimelineInsertionBoundary] = useState<number | null>(null);
  const [librarySelection, setLibrarySelection] = useState(new Set<number>());
  const [libraryAnchor, setLibraryAnchor] = useState<number | null>(null);
  const [collapsedSources, setCollapsedSources] = useState(new Set<string>());
  const [locatedHistoricalFrame, setLocatedHistoricalFrame] = useState<{ variantId: string; frameId: string } | null>(null);
  const [pendingLocateKey, setPendingLocateKey] = useState<string | null>(null);
  const [inspectorTab, setInspectorTab] = useState<"frame" | "shadow">("frame");
  const [playing, setPlaying] = useState(false);
  const [playIndex, setPlayIndex] = useState(0);
  const [speed, setSpeed] = useState(1);
  const [totalDurationInput, setTotalDurationInput] = useState("");
  const [totalDurationEditing, setTotalDurationEditing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [shadowPreviews, setShadowPreviews] = useState(new Map<string, ShadowPreview>());
  const shadowPreviewGeneration = useRef(0);
  const shadowPreviewCache = useRef(new Map<string, ShadowPreview>());
  const pendingShadowPreviewKeys = useRef(new Set<string>());
  const latestLargeShadowRequest = useRef<{
    characterId: string;
    focusedFrameId: string;
    generation: number;
    loop: boolean;
    request: ShadowPreviewRequest<ActionFrameRefV4>;
  } | null>(null);
  const largeShadowRequestRunning = useRef(false);
  const startedAt = useRef(0);
  const timelineTrackRef = useRef<HTMLDivElement>(null);
  const timelineDragPointerX = useRef<number | null>(null);
  const timelineAutoScrollFrame = useRef<number | null>(null);
  const timelineAutoScrollTime = useRef<number | null>(null);
  const libraryFrameElements = useRef(new Map<string, HTMLButtonElement>());
  const loadedActionId = useRef<string | null>(null);
  const previousSavedFrames = useRef<ActionFrameRefV4[]>([]);
  const draftSnapshot = useRef<ActionFrameRefV4[]>([]);
  useAutoDismissNoticeV4(notice, () => setNotice(null));

  const characters = useMemo(() => domain?.characters ?? [], [domain?.characters]);
  const characterId = controlledCharacterId ?? localCharacterId;
  const actionId = controlledActionId ?? localActionId;
  const character = characters.find((item) => item.id === characterId) ?? characters[0];
  const actions = (domain?.actions ?? []).filter((item) => item.characterId === character?.id);
  const savedAction = actions.find((item) => item.id === actionId) ?? actions[0];

  useEffect(() => {
    if (!characterId && characters[0]) setLocalCharacterId(characters[0].id);
  }, [characterId, characters]);

  useEffect(() => {
    if (character && !actions.some((item) => item.id === actionId)) {
      const nextActionId = actions[0]?.id ?? null;
      setLocalActionId(nextActionId);
      onActionChange?.(nextActionId);
    }
  }, [actionId, actions, character]);

  useEffect(() => { draftSnapshot.current = draft; }, [draft]);

  function updateDraft(
    nextValue: ActionFrameRefV4[] | ((current: ActionFrameRefV4[]) => ActionFrameRefV4[]),
    _change?: { name: string; mergeKey?: string },
  ) {
    const previous = draftSnapshot.current;
    const next = typeof nextValue === "function"
      ? nextValue(previous)
      : nextValue;
    if (JSON.stringify(previous) === JSON.stringify(next)) return;
    draftSnapshot.current = next;
    setDraft(next);
  }

  useEffect(() => {
    const nextSaved = cloneFrames(savedAction?.frameRefs ?? []);
    if (loadedActionId.current !== (savedAction?.id ?? null)) {
      loadedActionId.current = savedAction?.id ?? null;
      previousSavedFrames.current = nextSaved;
      setServerBaseline(nextSaved);
      updateDraft(nextSaved);
      setSelection(nextSaved.length ? new Set([0]) : new Set());
      setSelectionAnchor(nextSaved.length ? 0 : null);
      setFocusedFrameId(nextSaved[0]?.id ?? null);
      setPlaying(false);
      setPlayIndex(0);
      return;
    }
    if (JSON.stringify(previousSavedFrames.current) === JSON.stringify(nextSaved)) return;
    const local = draftSnapshot.current;
    const wasDirty = JSON.stringify(local) !== JSON.stringify(previousSavedFrames.current);
    previousSavedFrames.current = nextSaved;
    setServerBaseline(nextSaved);
    if (!wasDirty) {
      updateDraft(nextSaved);
      return;
    }
    const localById = new Map(local.map((frame) => [frame.id, frame]));
    let migrated = 0;
    const merged = nextSaved.map((serverFrame) => {
      const localFrame = localById.get(serverFrame.id);
      if (!localFrame) return serverFrame;
      if (localFrame.variantId !== serverFrame.variantId || localFrame.frameId !== serverFrame.frameId) migrated += 1;
      return { ...localFrame, variantId: serverFrame.variantId, frameId: serverFrame.frameId };
    });
    updateDraft(merged);
    if (migrated) setNotice(`最新处理结果已发布；已合并 ${migrated} 个帧身份，未保存参数仍保留。`);
  }, [savedAction?.id, savedAction?.frameRefs]);

  const dirty = Boolean(savedAction) && JSON.stringify(draft) !== JSON.stringify(serverBaseline);
  useEffect(() => onDirtyChange?.(dirty), [dirty, onDirtyChange]);
  useEffect(() => {
    const guard = (event: BeforeUnloadEvent) => {
      if (!dirty) return;
      event.preventDefault();
    };
    window.addEventListener("beforeunload", guard);
    return () => window.removeEventListener("beforeunload", guard);
  }, [dirty]);

  const sourceById = useMemo(
    () => new Map((domain?.materialSources ?? []).map((item) => [item.id, item])),
    [domain?.materialSources],
  );
  const allVariants = useMemo(
    () => (domain?.materialVariants ?? []).filter(
      (variant) => sourceById.get(variant.sourceId)?.characterId === character?.id,
    ),
    [character?.id, domain?.materialVariants, sourceById],
  );
  const variantById = useMemo(
    () => new Map(allVariants.map((variant) => [variant.id, variant])),
    [allVariants],
  );
  const projectedLibraryGroups = useMemo<LibraryGroup[]>(() => (
    (domain?.materialSources ?? [])
      .filter((source) => source.characterId === character?.id)
      .flatMap((source) => {
        const frames = projectLatestMaterialFrames(
          source.frames.map((frame) => frame.id),
          source.variantIds,
          allVariants,
        ).flatMap<LibraryFrame>((entry) => {
          const variant = variantById.get(entry.variantId);
          const frame = variant?.frames[entry.variantIndex];
          return variant && frame ? [{
            key: `${variant.id}:${frame.id}`,
            variant,
            frameIndex: entry.variantIndex,
            frameId: frame.id,
            sourceIndex: entry.sourceIndex,
            sourceName: source.displayName,
          }] : [];
        });
        return frames.length ? [{
          key: source.id,
          sourceId: source.id,
          sourceName: source.displayName,
          frames,
          historical: false,
        }] : [];
      })
  ), [allVariants, character?.id, domain?.materialSources, variantById]);
  const latestFrameKeys = useMemo(
    () => new Set(projectedLibraryGroups.flatMap((group) => group.frames.map((frame) => frame.key))),
    [projectedLibraryGroups],
  );
  const libraryGroups = useMemo<LibraryGroup[]>(() => {
    const groups = [...projectedLibraryGroups];
    const locatedKey = locatedHistoricalFrame
      ? `${locatedHistoricalFrame.variantId}:${locatedHistoricalFrame.frameId}`
      : null;
    if (locatedHistoricalFrame && locatedKey && !latestFrameKeys.has(locatedKey)) {
      const variant = variantById.get(locatedHistoricalFrame.variantId);
      const frameIndex = variant?.frames.findIndex((item) => item.id === locatedHistoricalFrame.frameId) ?? -1;
      if (variant && frameIndex >= 0) {
        const source = sourceById.get(variant.sourceId);
        const frame = variant.frames[frameIndex];
        groups.push({
          key: `historical:${locatedKey}`,
          sourceId: variant.sourceId,
          sourceName: source?.displayName ?? "素材",
          frames: [{
            key: locatedKey,
            variant,
            frameIndex,
            frameId: frame.id,
            sourceIndex: source?.frames.findIndex((item) => item.id === frame.sourceFrameId) ?? -1,
            sourceName: source?.displayName ?? "素材",
          }],
          historical: true,
        });
      }
    }
    return groups;
  }, [latestFrameKeys, locatedHistoricalFrame, projectedLibraryGroups, sourceById, variantById]);
  const libraryFrames = useMemo<LibraryFrame[]>(
    () => libraryGroups.flatMap((group) => group.frames),
    [libraryGroups],
  );
  const frameUsageCounts = useMemo(
    () => countCharacterFrameUsage(actions, savedAction?.id, draft),
    [actions, draft, savedAction?.id],
  );

  const selectedIndices = [...selection].filter((index) => index >= 0 && index < draft.length).sort((a, b) => a - b);
  const canvasFocusedIndex = resolveCanvasFocusIndex(draft, focusedFrameId, selectedIndices);
  const focusedIndex = playing ? playIndex : canvasFocusedIndex;
  const focused = draft[focusedIndex];
  const focusedVariant = allVariants.find((item) => item.id === focused?.variantId);
  const focusedVariantIndex = focusedVariant?.frames.findIndex((item) => item.id === focused?.frameId) ?? -1;
  const focusedVariantFrame = focusedVariant && focusedVariantIndex >= 0
    ? focusedVariant.frames[focusedVariantIndex]
    : null;
  const focusedSource = focusedVariant ? sourceById.get(focusedVariant.sourceId) : undefined;
  const focusedSourceIndex = focusedSource?.frames.findIndex(
    (item) => item.id === focusedVariantFrame?.sourceFrameId,
  ) ?? -1;
  const focusedUrl = focusedVariant && focusedVariantIndex >= 0
    ? api.materialVariantFrameUrl(focusedVariant.id, focusedVariantIndex)
    : null;
  const focusedOriginalUrl = focusedSource && focusedSourceIndex >= 0
    ? api.materialSourceFrameUrl(focusedSource.id, focusedSourceIndex)
    : null;
  const onionIndices = adjacentEnabledFrameIndices(draft, focusedIndex);
  const previousOnionFrame = onionIndices.previous === null ? undefined : draft[onionIndices.previous];
  const nextOnionFrame = onionIndices.next === null ? undefined : draft[onionIndices.next];
  const onionFrameUrl = (frame: ActionFrameRefV4 | undefined): string | null => {
    if (!frame) return null;
    const variant = variantById.get(frame.variantId);
    const frameIndex = variant?.frames.findIndex((item) => item.id === frame.frameId) ?? -1;
    return variant && frameIndex >= 0 ? api.materialVariantFrameUrl(variant.id, frameIndex) : null;
  };
  const previousOnionUrl = onionFrameUrl(previousOnionFrame);
  const nextOnionUrl = onionFrameUrl(nextOnionFrame);
  const hasPlayableOriginalFrame = draft.some((frame) => {
    if (frame.enabled === false) return false;
    const variant = variantById.get(frame.variantId);
    const variantFrame = variant?.frames.find((item) => item.id === frame.frameId);
    const source = variant ? sourceById.get(variant.sourceId) : undefined;
    return Boolean(source?.frames.some((item) => item.id === variantFrame?.sourceFrameId));
  });

  useEffect(() => {
    if (!pendingLocateKey) return;
    const libraryIndex = libraryFrames.findIndex((item) => item.key === pendingLocateKey);
    const libraryFrame = libraryFrames[libraryIndex];
    if (libraryIndex < 0 || !libraryFrame) return;
    setLibrarySelection(new Set([libraryIndex]));
    setLibraryAnchor(libraryIndex);
    setNotice(`已在左侧素材帧库选中 ${libraryFrame.sourceName} · ${String(Math.max(0, libraryFrame.sourceIndex) + 1).padStart(3, "0")}`);
    setPendingLocateKey(null);
    requestAnimationFrame(() => requestAnimationFrame(() => {
      const target = libraryFrameElements.current.get(pendingLocateKey);
      target?.focus({ preventScroll: true });
      target?.scrollIntoView({ behavior: "smooth", block: "center", inline: "nearest" });
    }));
  }, [libraryFrames, pendingLocateKey]);
  const previewLoop = savedAction?.previewLoop ?? savedAction?.loop ?? true;
  useEffect(() => {
    const generation = ++shadowPreviewGeneration.current;
    shadowPreviewCache.current.clear();
    latestLargeShadowRequest.current = null;
    const pendingKeys = new Set<string>();
    pendingShadowPreviewKeys.current = pendingKeys;
    setShadowPreviews(new Map());
    const firstEnabled = draft.find((frame) => frame.enabled !== false);
    if (!character || !firstEnabled) return undefined;
    const request = buildShadowPreviewRequest(draft, firstEnabled.id, previewLoop);
    if (!request?.cacheAll) return undefined;
    const timer = window.setTimeout(() => {
      pendingKeys.add(request.key);
      void api.previewDomainShadow(character.id, { frameRefs: request.frames, loop: previewLoop }).then((result) => {
        if (shadowPreviewGeneration.current !== generation) return;
        request.frames.forEach((frame, index) => {
          const preview = result.frames[index];
          if (preview) shadowPreviewCache.current.set(frame.id, preview);
        });
        shadowPreviewCache.current.set(request.key, result.frames[request.previewIndex]);
        setShadowPreviews(new Map(shadowPreviewCache.current));
      }).catch((error) => {
        if (shadowPreviewGeneration.current === generation) setNotice(error instanceof Error ? `阴影预览失败：${error.message}` : "阴影预览失败。");
      }).finally(() => pendingKeys.delete(request.key));
    }, 120);
    return () => window.clearTimeout(timer);
  }, [character, draft, previewLoop]);

  useEffect(() => {
    if (!character || !focused || focused.enabled === false) return undefined;
    const request = buildShadowPreviewRequest(draft, focused.id, previewLoop);
    if (!request || request.cacheAll || !shouldRequestShadowPreview(shadowPreviewCache.current, pendingShadowPreviewKeys.current, request.key)) return undefined;
    latestLargeShadowRequest.current = {
      characterId: character.id,
      focusedFrameId: focused.id,
      generation: shadowPreviewGeneration.current,
      loop: previewLoop,
      request,
    };
    if (largeShadowRequestRunning.current) return undefined;
    largeShadowRequestRunning.current = true;
    void (async () => {
      try {
        while (latestLargeShadowRequest.current) {
          const work = latestLargeShadowRequest.current;
          latestLargeShadowRequest.current = null;
          const pendingKeys = pendingShadowPreviewKeys.current;
          if (!shouldRequestShadowPreview(shadowPreviewCache.current, pendingKeys, work.request.key)) continue;
          pendingKeys.add(work.request.key);
          try {
            const result = await api.previewDomainShadow(work.characterId, { frameRefs: work.request.frames, loop: work.loop });
            if (shadowPreviewGeneration.current !== work.generation) continue;
            const preview = result.frames[work.request.previewIndex];
            if (preview) shadowPreviewCache.current.set(work.focusedFrameId, preview);
            setShadowPreviews(new Map(shadowPreviewCache.current));
          } catch (error) {
            if (shadowPreviewGeneration.current === work.generation) setNotice(error instanceof Error ? `阴影预览失败：${error.message}` : "阴影预览失败。");
          } finally {
            pendingKeys.delete(work.request.key);
          }
        }
      } finally {
        largeShadowRequestRunning.current = false;
      }
    })();
    return undefined;
  }, [character, draft, focused, previewLoop]);
  const shadowPreview = focused ? shadowPreviews.get(focused.id) ?? null : null;
  const playbackTimeline = useMemo(() => createPlaybackTimeline(draft), [draft]);
  const playableCount = playbackTimeline.indices.length;
  const playableDuration = playbackTimeline.totalSeconds;

  useEffect(() => {
    if (!totalDurationEditing) {
      setTotalDurationInput(playableCount ? String(Number(playableDuration.toFixed(6))) : "");
    }
  }, [playableCount, playableDuration, totalDurationEditing]);

  useEffect(() => {
    if (!playing || !playableCount) return undefined;
    startedAt.current = performance.now();
    let requestId = 0;
    const tick = (now: number) => {
      const elapsed = (now - startedAt.current) / 1000 * speed;
      const nextIndex = frameIndexAtTimelineTime(playbackTimeline, elapsed, previewLoop);
      setPlayIndex((current) => current === nextIndex ? current : nextIndex);
      if (!previewLoop && elapsed >= playableDuration) {
        setPlaying(false);
        return;
      }
      requestId = requestAnimationFrame(tick);
    };
    requestId = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(requestId);
  }, [playbackTimeline, playableCount, playableDuration, playing, previewLoop, speed]);

  useEffect(() => {
    if (playing && !playableCount) setPlaying(false);
  }, [playableCount, playing]);

  useEffect(() => {
    const track = timelineTrackRef.current;
    if (!track) return;
    track.querySelector("article.playing")?.classList.remove("playing");
    if (playing && playIndex >= 0) {
      track.querySelector<HTMLElement>(`article[data-timeline-index="${playIndex}"]`)?.classList.add("playing");
    }
  }, [playIndex, playing]);

  function confirmDiscard(): boolean {
    return !dirty || window.confirm("当前动作有未保存修改，确定放弃吗？");
  }

  function applyTotalDuration() {
    const totalSeconds = Number(totalDurationInput.trim());
    if (!Number.isFinite(totalSeconds) || totalSeconds <= 0 || totalSeconds > 3600) {
      setNotice("总时长必须大于 0 秒且不超过 3600 秒。");
      return;
    }
    if (!playableCount) {
      setNotice("当前没有启用帧，无法均分总时长。");
      return;
    }
    setPlaying(false);
    updateDraft(
      (current) => distributeEnabledFrameDurations(current, totalSeconds),
      { name: "均分动作时长" },
    );
    setTotalDurationEditing(false);
    setNotice(`已将 ${totalSeconds} 秒均分到 ${playableCount} 个启用帧；请保存动作以写入角色包。`);
  }

  function chooseAction(nextId: string) {
    if (!confirmDiscard()) return;
    setLocalActionId(nextId);
    onActionChange?.(nextId);
  }

  function closeEditor() {
    if (confirmDiscard()) onClose?.();
  }

  async function refresh() {
    await domainQuery.refetch();
  }

  async function createAction() {
    if (!character || !domain || !confirmDiscard()) return;
    const name = window.prompt("动作名称", `动作 ${actions.length + 1}`)?.trim();
    if (!name) return;
    setBusy(true);
    try {
      const result = await api.createDomainAction(character.id, name, domain.revisionId);
      if (!mergeDomainActionV4(queryClient, result.action, result.revisionId)) await refresh();
      setLocalActionId(result.action.id);
      onActionChange?.(result.action.id);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "创建动作失败。");
    } finally {
      setBusy(false);
    }
  }

  async function renameAction(action: DomainActionV4) {
    if (!domain || !confirmDiscard()) return;
    const name = window.prompt("重命名动作", action.name)?.trim();
    if (!name || name === action.name) return;
    setBusy(true);
    try {
      await api.updateDomainAction(action.id, { name }, domain.revisionId);
      await refresh();
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "重命名失败。");
    } finally {
      setBusy(false);
    }
  }

  async function deleteAction(action: DomainActionV4) {
    if (!domain || !confirmDiscard() || !window.confirm(`删除动作“${action.name}”？`)) return;
    setBusy(true);
    try {
      await api.deleteDomainAction(action.id, domain.revisionId);
      setLocalActionId(null);
      onActionChange?.(null);
      await refresh();
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "删除动作失败。");
    } finally {
      setBusy(false);
    }
  }

  async function toggleLoop() {
    if (!domain || !savedAction) return;
    if (dirty) {
      setNotice("请先保存当前帧修改，再切换循环播放。");
      return;
    }
    setBusy(true);
    try {
      await api.updateDomainAction(savedAction.id, { previewLoop: !(savedAction.previewLoop ?? savedAction.loop ?? true) }, domain.revisionId);
      await refresh();
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "循环设置保存失败。");
    } finally {
      setBusy(false);
    }
  }

  async function save(): Promise<boolean> {
    if (!domain || !savedAction || busy) return false;
    setBusy(true);
    try {
      const latestDraft = cloneFrames(draftSnapshot.current);
      const result = await api.saveDomainActionFrames(savedAction.id, latestDraft, domain.revisionId);
      const confirmed = cloneFrames(result.action.frameRefs);
      previousSavedFrames.current = confirmed;
      setServerBaseline(confirmed);
      updateDraft(confirmed);
      onDirtyChange?.(false);
      if (!mergeDomainActionV4(queryClient, result.action, result.revisionId)) await refresh();
      setNotice("动作已保存。");
      return true;
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "动作保存失败。");
      return false;
    } finally {
      setBusy(false);
    }
  }
  usePageSaveCommandV4(save);

  async function reset() {
    if (!savedAction || (!dirty || window.confirm("重置到上一次保存的状态？"))) {
      if (!savedAction) return;
      setBusy(true);
      try {
        const result = await api.resetDomainAction(savedAction.id);
        const confirmed = cloneFrames(result.action.frameRefs);
        previousSavedFrames.current = confirmed;
        setServerBaseline(confirmed);
        updateDraft(confirmed);
        onDirtyChange?.(false);
        setSelection(result.action.frameRefs.length ? new Set([0]) : new Set());
        setSelectionAnchor(result.action.frameRefs.length ? 0 : null);
        setFocusedFrameId(result.action.frameRefs[0]?.id ?? null);
        setNotice("已重置到上一次保存状态。");
      } catch (error) {
        setNotice(error instanceof Error ? error.message : "重置失败。");
      } finally {
        setBusy(false);
      }
    }
  }

  function makeRef(item: LibraryFrame): ActionFrameRefV4 {
    return {
      id: draftId(),
      variantId: item.variant.id,
      frameId: item.frameId,
      durationSeconds: 1 / 24,
      enabled: true,
      transform: defaultTransform(),
    };
  }

  function addLibraryFrames(items: LibraryFrame[]) {
    if (!items.length) return;
    const start = draft.length;
    const refs = items.map(makeRef);
    updateDraft((current) => [...current, ...refs], { name: "添加动作帧" });
    setSelection(new Set(refs.map((_, index) => start + index)));
    setSelectionAnchor(start);
    setFocusedFrameId(refs[0]?.id ?? null);
  }

  function selectLibrary(index: number, event: MouseEvent) {
    const result = selectFrameIndex(librarySelection, index, libraryAnchor, {
      shift: event.shiftKey,
      additive: event.ctrlKey || event.metaKey,
    });
    setLibrarySelection(result.selected);
    setLibraryAnchor(result.anchor);
  }

  function selectTimeline(index: number, event: MouseEvent) {
    if (event.altKey && selectedIndices.length > 1) {
      setFocusedFrameId(draft[index]?.id ?? null);
      setPlaying(false);
      return;
    }
    const result = selectFrameIndex(selection, index, selectionAnchor, {
      shift: event.shiftKey,
      additive: event.ctrlKey || event.metaKey,
    });
    setSelection(result.selected);
    setSelectionAnchor(result.anchor);
    const nextFocusedIndex = result.selected.has(index)
      ? index
      : [...result.selected].sort((left, right) => left - right)[0];
    setFocusedFrameId(nextFocusedIndex === undefined ? null : draft[nextFocusedIndex]?.id ?? null);
    setPlaying(false);
  }

  function updateSelected(update: (transform: ActionFrameTransformV4, frame: ActionFrameRefV4) => void) {
    const targets = new Set(selectedIndices.length ? selectedIndices : [focusedIndex]);
    updateDraft((current) => current.map((frame, index) => {
      if (!targets.has(index)) return frame;
      const next = structuredClone(frame);
      update(next.transform, next);
      return next;
    }), { name: "调整动作帧", mergeKey: "frame-adjust" });
  }

  function duplicateSelected() {
    if (!selectedIndices.length) return;
    const copies = selectedIndices.map((index) => ({ ...structuredClone(draft[index]), id: draftId() }));
    const insertAt = selectedIndices[selectedIndices.length - 1] + 1;
    const next = [...draft.slice(0, insertAt), ...copies, ...draft.slice(insertAt)];
    updateDraft(next, { name: "复制动作帧" });
    setSelection(new Set(copies.map((_, index) => insertAt + index)));
    setSelectionAnchor(insertAt);
    setFocusedFrameId(copies[0]?.id ?? null);
  }

  function deleteSelected() {
    if (!selectedIndices.length) return;
    const selected = new Set(selectedIndices);
    const next = draft.filter((_, index) => !selected.has(index));
    updateDraft(next, { name: "删除动作帧" });
    const target = Math.min(selectedIndices[0], Math.max(0, next.length - 1));
    setSelection(next.length ? new Set([target]) : new Set());
    setSelectionAnchor(next.length ? target : null);
    setFocusedFrameId(next[target]?.id ?? null);
  }

  function selectAllTimeline() {
    setSelection(new Set(draft.map((_, index) => index)));
    setSelectionAnchor(draft.length ? 0 : null);
    setPlaying(false);
  }

  function changeSelectedEnabled(enabled: boolean) {
    if (!selectedIndices.length) return;
    updateDraft(
      (current) => setFramesEnabled(current, selectedIndices, enabled),
      { name: enabled ? "启用动作帧" : "禁用动作帧" },
    );
    setPlaying(false);
  }

  function locateTimelineSource(frame: ActionFrameRefV4) {
    const variant = allVariants.find((item) => item.id === frame.variantId);
    const libraryKey = `${frame.variantId}:${frame.frameId}`;
    const variantFrame = variant?.frames.find((item) => item.id === frame.frameId);
    if (!variant || !variantFrame) {
      setNotice("无法在当前动作的素材帧库中定位该帧；素材引用可能已损坏。");
      return;
    }
    setLocatedHistoricalFrame(
      latestFrameKeys.has(libraryKey) ? null : { variantId: variant.id, frameId: frame.frameId },
    );
    const groupKey = latestFrameKeys.has(libraryKey)
      ? variant.sourceId
      : `historical:${libraryKey}`;
    setCollapsedSources((current) => {
      const next = new Set(current);
      next.delete(groupKey);
      return next;
    });
    setPendingLocateKey(libraryKey);
  }

  function canvasShortcut(event: KeyboardEvent<HTMLDivElement>) {
    if (!focused) return;
    const distance = event.shiftKey ? 10 : 1;
    if (["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "+", "=", "-", "[", "]"].includes(event.key)) {
      event.preventDefault();
    }
    if (event.key === "ArrowLeft") updateSelected((transform) => { transform.position.x -= distance; });
    if (event.key === "ArrowRight") updateSelected((transform) => { transform.position.x += distance; });
    if (event.key === "ArrowUp") updateSelected((transform) => { transform.position.y += distance; });
    if (event.key === "ArrowDown") updateSelected((transform) => { transform.position.y -= distance; });
    if (event.key === "+" || event.key === "=") updateSelected((transform) => {
      transform.scale.x *= 1.05; transform.scale.y *= 1.05;
    });
    if (event.key === "-") updateSelected((transform) => {
      transform.scale.x = Math.max(0.01, transform.scale.x / 1.05);
      transform.scale.y = Math.max(0.01, transform.scale.y / 1.05);
    });
    if (event.key === "[") updateSelected((transform) => { transform.rotationDegrees -= 1; });
    if (event.key === "]") updateSelected((transform) => { transform.rotationDegrees += 1; });
  }

  const stopTimelineAutoScroll = useCallback(() => {
    if (timelineAutoScrollFrame.current !== null) {
      window.cancelAnimationFrame(timelineAutoScrollFrame.current);
      timelineAutoScrollFrame.current = null;
    }
    timelineAutoScrollTime.current = null;
    timelineDragPointerX.current = null;
  }, []);

  function timelineBoundaryAtClientX(track: HTMLDivElement, clientX: number): number {
    const cards = track.querySelectorAll<HTMLElement>("[data-timeline-index]");
    for (const card of cards) {
      const bounds = card.getBoundingClientRect();
      if (clientX < bounds.left + bounds.width / 2) {
        return Number(card.dataset.timelineIndex);
      }
    }
    return draft.length;
  }

  const runTimelineAutoScroll = useCallback((time: number) => {
    timelineAutoScrollFrame.current = null;
    const track = timelineTrackRef.current;
    const pointerX = timelineDragPointerX.current;
    if (!track || pointerX === null) return;
    const bounds = track.getBoundingClientRect();
    const velocity = timelineEdgeVelocityV4({
      pointerX,
      left: bounds.left,
      width: bounds.width,
      scrollLeft: track.scrollLeft,
      scrollWidth: track.scrollWidth,
    });
    if (!velocity) {
      timelineAutoScrollTime.current = null;
      return;
    }
    const previousTime = timelineAutoScrollTime.current ?? time;
    const elapsedSeconds = Math.min(0.05, Math.max(0, time - previousTime) / 1000);
    const previousScrollLeft = track.scrollLeft;
    track.scrollLeft += velocity * elapsedSeconds;
    timelineAutoScrollTime.current = time;
    setTimelineInsertionBoundary(timelineBoundaryAtClientX(track, pointerX));
    if (track.scrollLeft === previousScrollLeft) {
      timelineAutoScrollTime.current = null;
      return;
    }
    timelineAutoScrollFrame.current = window.requestAnimationFrame(runTimelineAutoScroll);
  }, [draft.length]);

  function timelineDragOver(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    const track = event.currentTarget;
    timelineDragPointerX.current = event.clientX;
    setTimelineInsertionBoundary(timelineBoundaryAtClientX(track, event.clientX));
    const bounds = track.getBoundingClientRect();
    const velocity = timelineEdgeVelocityV4({
      pointerX: event.clientX,
      left: bounds.left,
      width: bounds.width,
      scrollLeft: track.scrollLeft,
      scrollWidth: track.scrollWidth,
    });
    if (!velocity) {
      if (timelineAutoScrollFrame.current !== null) window.cancelAnimationFrame(timelineAutoScrollFrame.current);
      timelineAutoScrollFrame.current = null;
      timelineAutoScrollTime.current = null;
      return;
    }
    if (timelineAutoScrollFrame.current === null) {
      timelineAutoScrollTime.current = null;
      timelineAutoScrollFrame.current = window.requestAnimationFrame(runTimelineAutoScroll);
    }
  }

  useEffect(() => stopTimelineAutoScroll, [stopTimelineAutoScroll]);

  function timelineDragStart(event: DragEvent<HTMLElement>, index: number) {
    const indices = selection.has(index) ? selectedIndices : [index];
    if (!selection.has(index)) {
      setSelection(new Set([index]));
      setSelectionAnchor(index);
      setFocusedFrameId(draft[index]?.id ?? null);
      setPlaying(false);
    }
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData(TIMELINE_DRAG_TYPE, JSON.stringify(indices));
  }

  function timelineDrop(event: DragEvent<HTMLDivElement>, boundary: number) {
    event.preventDefault();
    stopTimelineAutoScroll();
    setTimelineInsertionBoundary(null);
    const libraryKey = event.dataTransfer.getData("application/x-rotoweave-library-frame");
    if (libraryKey) {
      const item = libraryFrames.find((frame) => frame.key === libraryKey);
      if (item) {
        const next = [...draft];
        const insertAt = Math.max(0, Math.min(draft.length, boundary));
        next.splice(insertAt, 0, makeRef(item));
        updateDraft(next, { name: "添加动作帧" });
        setSelection(new Set([insertAt]));
        setSelectionAnchor(insertAt);
        setFocusedFrameId(next[insertAt]?.id ?? null);
      }
      return;
    }
    const payload = event.dataTransfer.getData(TIMELINE_DRAG_TYPE);
    if (!payload) return;
    let indices: number[];
    try {
      const parsed: unknown = JSON.parse(payload);
      if (!Array.isArray(parsed)) return;
      indices = parsed.filter((value): value is number => Number.isInteger(value));
    } catch {
      return;
    }
    const result = moveFrameBlockAtBoundary(draft, indices, boundary);
    if (!result.selectedIndices.length) return;
    updateDraft(result.frames, { name: "排序动作帧" });
    setSelection(new Set(result.selectedIndices));
    setSelectionAnchor(result.selectedIndices[0]);
    setPlaying(false);
  }

  if (domainQuery.isLoading) {
    return <div className={mode === "page" ? "action-editor-page" : "action-editor-overlay"}><div className="action-editor-loading"><Loader2 className="spin" />正在加载 4.0 动作工作区</div></div>;
  }

  return <div className={mode === "page" ? "action-editor-page" : "action-editor-overlay"} {...(mode === "overlay" ? { role: "dialog", "aria-modal": true } : { role: "region" })} aria-label="4.0 动作编辑器">
    <header className="action-editor-header">
      <div><small>RotoWeave 4.0</small><strong>{savedAction?.name ?? "动作编辑"}</strong></div>
      <span>{dirty ? "有未保存修改" : "已保存"}</span>
      <div className="action-editor-header-actions">
        <button type="button" disabled={!dirty || busy} onClick={() => void reset()}><RotateCcw size={15} />重置</button>
        <button type="button" className="primary" disabled={!savedAction || !dirty || busy} onClick={() => void save()}>{busy ? <Loader2 className="spin" size={15} /> : <Save size={15} />}保存</button>
        {mode === "overlay" ? <button type="button" aria-label="关闭动作编辑器" onClick={closeEditor}><X size={18} /></button> : null}
      </div>
    </header>

    {mode === "overlay" ? <aside className="action-editor-roles">
      <div className="action-editor-role-title"><strong>角色与动作</strong><button type="button" onClick={() => void createAction()} disabled={!character || busy}><Plus size={14} /></button></div>
      {characters.map((item) => <section key={item.id}>
        <button type="button" className={character?.id === item.id ? "active" : ""} onClick={() => {
          if (!confirmDiscard()) return;
          setLocalCharacterId(item.id); setLocalActionId(null); onActionChange?.(null);
        }}><Layers3 size={15} /><strong>{item.name}</strong></button>
        {character?.id === item.id ? <nav>{actions.map((action, index) => <div key={action.id} className={savedAction?.id === action.id ? "active" : ""}>
          <button type="button" onClick={() => chooseAction(action.id)} onDoubleClick={() => void renameAction(action)}><small>{String(index + 1).padStart(2, "0")}</small><span>{action.name}</span><em>{action.frameRefs.length} 帧</em></button>
          <button type="button" aria-label={`删除动作 ${action.name}`} onClick={() => void deleteAction(action)}><Trash2 size={12} /></button>
        </div>)}</nav> : null}
      </section>)}
      {!characters.length ? <p>尚无格式 3 角色。请先在素材管理中创建角色并导入视频。</p> : null}
    </aside> : null}

    <main className="action-editor-main">
      <section className="action-editor-library">
        <header><div><small>FRAME LIBRARY</small><strong>素材帧库</strong></div><button type="button" disabled={!librarySelection.size} onClick={() => addLibraryFrames([...librarySelection].sort((a, b) => a - b).map((index) => libraryFrames[index]).filter(Boolean))}><Plus size={13} />加入时间轴</button></header>
        {libraryGroups.map((group) => {
          const collapsed = collapsedSources.has(group.key);
          return <article key={group.key}>
            <button type="button" className="action-editor-library-group" onClick={() => setCollapsedSources((current) => {
              const next = new Set(current); if (next.has(group.key)) next.delete(group.key); else next.add(group.key); return next;
            })}>{collapsed ? <ChevronRight size={13} /> : <ChevronDown size={13} />}<strong>{group.sourceName}{group.historical ? " · 历史定位" : ""}</strong><span>{group.historical ? group.frames[0]?.variant.kind.toUpperCase() : "累计"} · {group.frames.length}</span></button>
            {!collapsed ? <div className="action-editor-library-grid">{group.frames.map((item) => {
              const { variant, frameIndex } = item;
              const globalIndex = libraryFrames.findIndex((candidate) => candidate.key === item.key);
              return <button
                type="button"
                key={item.key}
                ref={(element) => {
                  if (element) libraryFrameElements.current.set(item.key, element);
                  else libraryFrameElements.current.delete(item.key);
                }}
                className={librarySelection.has(globalIndex) ? "selected" : ""}
                draggable
                onDragStart={(event) => { event.dataTransfer.effectAllowed = "copy"; event.dataTransfer.setData("application/x-rotoweave-library-frame", item.key); }}
                onDragEnd={() => { stopTimelineAutoScroll(); setTimelineInsertionBoundary(null); }}
                onDoubleClick={() => addLibraryFrames([libraryFrames[globalIndex]])}
                onClick={(event) => selectLibrary(globalIndex, event)}
              ><img src={api.materialVariantFrameUrl(variant.id, frameIndex)} alt="" />{frameUsageCounts.has(item.key) ? <span className="action-editor-library-usage-mark" title={`当前角色动作共引用 ${frameUsageCounts.get(item.key)} 次`}>已使用</span> : null}<small>{String(Math.max(0, item.sourceIndex) + 1).padStart(3, "0")}</small></button>;
            })}</div> : null}
          </article>;
        })}
        {!libraryGroups.length ? <div className="action-editor-empty">素材处理完成后，每个源素材按帧累计的最新“处理后”结果会显示在这里。</div> : null}
      </section>

      <section className="action-editor-stage">
        <div className="action-editor-canvas" tabIndex={0} onKeyDown={canvasShortcut} aria-label="动作画布">
          {character ? <ActionCanvasV4 openKey={`${character.id}:${savedAction?.id ?? "none"}`} character={character} frame={focused} frameUrl={focusedUrl} originalFrameUrl={focusedOriginalUrl} previousFrame={previousOnionFrame} previousFrameUrl={previousOnionUrl} nextFrame={nextOnionFrame} nextFrameUrl={nextOnionUrl} shadowPreview={shadowPreview} selectedCount={selectedIndices.length || (focused ? 1 : 0)} playing={playing} playableCount={playableCount} hasPlayableOriginalFrame={hasPlayableOriginalFrame} onMove={(dx, dy) => updateSelected((transform) => { transform.position.x += dx; transform.position.y += dy; })} onShadowX={(dx) => updateSelected((transform) => { transform.shadow.offset.x += dx; })} /> : <div className="action-editor-empty"><Film size={38} /><strong>把素材帧拖入时间轴</strong></div>}
        </div>
        <div className="action-editor-transport">
          <button type="button" disabled={!playableCount} onClick={() => setPlaying((value) => !value)}>{playing ? <Pause size={15} /> : <Play size={15} />}{playing ? "暂停" : "播放"}</button>
          <label className="action-speed-control">速度<input type="range" min="0.25" max="2" step="any" value={speed} onChange={(event) => setSpeed(Number(event.target.value))} /><output>{speed.toFixed(2)}×</output></label>
          <form className="action-duration-control" onSubmit={(event) => { event.preventDefault(); applyTotalDuration(); }} onBlur={(event) => { if (!event.currentTarget.contains(event.relatedTarget)) setTotalDurationEditing(false); }}>
            <label>总时长（秒）<input type="text" inputMode="decimal" value={totalDurationInput} disabled={!draft.length} onFocus={() => setTotalDurationEditing(true)} onChange={(event) => setTotalDurationInput(event.target.value)} onKeyDown={(event) => { if (event.key === "Escape") { setTotalDurationEditing(false); event.currentTarget.blur(); } }} /></label>
            <button type="submit" disabled={!draft.length}>均分到启用帧</button>
          </form>
          <button type="button" className={savedAction?.previewLoop ?? savedAction?.loop ?? true ? "active" : ""} disabled={!savedAction || busy} onClick={() => void toggleLoop()}><Repeat2 size={14} />预览循环</button>
          <span>{draft.length ? `${focusedIndex >= 0 ? focusedIndex + 1 : "—"} / ${draft.length}` : "0 / 0"}</span>
          <small>1 画布 · 2 帧 · 3 阴影 X · 空格拖动临时平移</small>
        </div>
      </section>

      <aside className="action-editor-inspector">
        <nav><button type="button" className={inspectorTab === "frame" ? "active" : ""} onClick={() => setInspectorTab("frame")}>帧调整</button><button type="button" className={inspectorTab === "shadow" ? "active" : ""} onClick={() => setInspectorTab("shadow")}>阴影调整</button></nav>
        {!focused ? <div className="action-editor-empty">选择一个或多个时间轴帧进行调整。</div> : inspectorTab === "frame" ? <div className="action-editor-fields">
          <p>{selectedIndices.length > 1 ? `批量调整 ${selectedIndices.length} 帧 · 画布显示第 ${focusedIndex + 1} 帧` : `第 ${focusedIndex + 1} 帧`}</p>
          <label>时长（秒）<NumericDraftInput value={focused.durationSeconds} maximum={3600} strictlyPositive onCommit={(value) => updateSelected((_transform, frame) => { frame.durationSeconds = value; })} /></label>
          <div><label>位置 X<NumericDraftInput value={focused.transform.position.x} minimum={-65536} maximum={65536} onCommit={(value) => updateSelected((transform) => { transform.position.x = value; })} /></label><label>位置 Y<NumericDraftInput value={focused.transform.position.y} minimum={-65536} maximum={65536} onCommit={(value) => updateSelected((transform) => { transform.position.y = value; })} /></label></div>
          <div><label>Unity 显示缩放 X<PercentDraftInputV4 value={focused.transform.scale.x} maximum={8} strictlyPositive onCommit={(value) => updateSelected((transform) => { transform.scale.x = value; })} /></label><label>Unity 显示缩放 Y<PercentDraftInputV4 value={focused.transform.scale.y} maximum={8} strictlyPositive onCommit={(value) => updateSelected((transform) => { transform.scale.y = value; })} /></label></div>
          <label>旋转<NumericDraftInput value={focused.transform.rotationDegrees} onCommit={(value) => updateSelected((transform) => { transform.rotationDegrees = value; })} /></label>
          <label>覆盖颜色<input type="color" value={focused.transform.color} onChange={(event) => updateSelected((transform) => { transform.color = event.target.value; })} /></label>
          <label>透明度<input type="range" min="0" max="1" step="0.01" value={focused.transform.opacity} onChange={(event) => updateSelected((transform) => { transform.opacity = Number(event.target.value); })} /><output>{Math.round(focused.transform.opacity * 100)}%</output></label>
        </div> : <div className="action-editor-fields">
          <label>阴影启用方式<select value={shadowEnabledMode(focused.transform.shadow.enabled)} onChange={(event) => updateSelected((transform) => { transform.shadow.enabled = shadowEnabledOverride(event.target.value as "inherit" | "enabled" | "disabled"); })}><option value="inherit">继承全局（当前{character?.shadow?.enabled ? "启用" : "关闭"}）</option><option value="enabled">强制启用</option><option value="disabled">强制关闭</option></select></label>
          <label>阴影颜色<input type="color" value={focused.transform.shadow.color ?? character?.shadow?.color ?? "#000000"} onChange={(event) => updateSelected((transform) => { transform.shadow.color = event.target.value; })} /></label>
          <label>阴影透明度<input type="range" min="0" max="1" step="0.01" value={focused.transform.shadow.opacity ?? character?.shadow?.baseOpacity ?? .35} onChange={(event) => updateSelected((transform) => { transform.shadow.opacity = Number(event.target.value); })} /><output>{Math.round((focused.transform.shadow.opacity ?? character?.shadow?.baseOpacity ?? .35) * 100)}%</output></label>
          <button type="button" onClick={() => updateSelected((transform) => { transform.shadow.enabled = null; transform.shadow.color = null; transform.shadow.opacity = null; })}>所选帧恢复继承全局</button>
          <button type="button" onClick={() => updateDraft((current) => inheritAllFrameShadows(current), { name: "恢复动作阴影继承" })}>当前动作全部继承全局</button>
          <div><label>偏移 X<NumericDraftInput value={focused.transform.shadow.offset.x} minimum={-65536} maximum={65536} onCommit={(value) => updateSelected((transform) => { transform.shadow.offset.x = value; })} /></label><label>偏移 Y<NumericDraftInput value={focused.transform.shadow.offset.y} minimum={-65536} maximum={65536} onCommit={(value) => updateSelected((transform) => { transform.shadow.offset.y = value; })} /></label></div>
          <div><label>宽度<PercentDraftInputV4 value={focused.transform.shadow.scale.x} maximum={8} strictlyPositive onCommit={(value) => updateSelected((transform) => { transform.shadow.scale.x = value; })} /></label><label>深度<PercentDraftInputV4 value={focused.transform.shadow.scale.y} maximum={8} strictlyPositive onCommit={(value) => updateSelected((transform) => { transform.shadow.scale.y = value; })} /></label></div>
        </div>}
      </aside>

      <section className="action-editor-timeline">
        <header><strong>时间轴 / 导演台</strong><span>{playableCount} / {draft.length} 帧启用 · {playableDuration.toFixed(3)} 秒</span><div><button type="button" disabled={!draft.length} onClick={selectAllTimeline}>全选</button><button type="button" disabled={!selectedIndices.length} onClick={() => changeSelectedEnabled(true)}>启用</button><button type="button" disabled={!selectedIndices.length} onClick={() => changeSelectedEnabled(false)}>禁用</button><button type="button" disabled={!selectedIndices.length} onClick={duplicateSelected}><Copy size={13} />复制</button><button type="button" disabled={!selectedIndices.length} onClick={deleteSelected}><Trash2 size={13} />删除</button><button type="button" disabled={!draft.length} onClick={() => updateDraft(normalizeFrameDurations(draft), { name: "归一动作帧时长" })}>归一 1/24 秒</button></div></header>
        <div ref={timelineTrackRef} className="action-editor-timeline-track" onDragOver={timelineDragOver} onDragLeave={(event) => { if (!event.currentTarget.contains(event.relatedTarget as Node | null)) { stopTimelineAutoScroll(); setTimelineInsertionBoundary(null); } }} onDrop={(event) => timelineDrop(event, timelineInsertionBoundary ?? timelineBoundaryAtClientX(event.currentTarget, event.clientX))}><TimelineFrameList draft={draft} selection={selection} canvasFocusedFrameId={draft[canvasFocusedIndex]?.id ?? null} playing={playing} variantById={variantById} timelineInsertionBoundary={timelineInsertionBoundary} onDragStart={timelineDragStart} onDragEnd={() => { stopTimelineAutoScroll(); setTimelineInsertionBoundary(null); }} onSelect={selectTimeline} onLocate={locateTimelineSource} /></div>
      </section>
    </main>
    {notice ? <button type="button" className="action-editor-notice" onClick={() => setNotice(null)}>{notice}</button> : null}
  </div>;
}
