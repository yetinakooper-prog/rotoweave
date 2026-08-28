import {
  AlertTriangle,
  Check,
  CheckCircle2,
  Download,
  Film,
  FolderSync,
  Image as ImageIcon,
  Loader2,
  Minus,
  Play,
  Plus,
  SlidersHorizontal,
  Square,
  Trash2,
  Upload,
  X,
} from "lucide-react";
import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type ChangeEvent,
  type DragEvent,
  type MouseEvent,
  type PointerEvent as ReactPointerEvent,
  type WheelEvent,
  type CSSProperties,
} from "react";
import { useQuery } from "@tanstack/react-query";

import { api, mediaUrl, subscribeToJobs } from "../lib/api";
import { PercentDraftInputV4 } from "../components/percent-draft-input-v4";
import { hexToRgb, projectLatestMaterialFrames, projectMaterialFrameDisplay, selectedFrameSequence, selectMaterialFrame } from "../lib/material-manager-v4";
import { zoomCanvasViewportAtPoint } from "../lib/canvas-viewport";
import { constrainDragPoint, type DragAxis } from "../lib/drag-constraint";
import type { Job, MaterialVariantV4, PhotoshopSheetV4 } from "../lib/types";
import { useEscapeClose } from "../lib/use-escape-close";
import { useAutoDismissNoticeV4 } from "../lib/use-auto-dismiss-notice-v4";

type Notice = { tone: "success" | "error"; message: string };
type PreviewBackground = "checker" | "white" | "black";
type MaterialManagerV4Props = {
  mode?: "overlay" | "page";
  onClose?: () => void;
  characterId?: string | null;
  focusSourceId?: string;
  focusFrameIndex?: number;
  onCharacterChange?: (characterId: string) => void;
};

function isVideo(file: File): boolean {
  return file.type.startsWith("video/") || /\.(mp4|mov|mkv|avi|webm|m4v)$/i.test(file.name);
}

function variantLabel(variant: MaterialVariantV4): string {
  if (variant.kind === "photoshop") return "PS";
  return variant.kind[0].toUpperCase() + variant.kind.slice(1);
}

export function MaterialManagerV4({
  mode = "overlay",
  onClose,
  characterId: controlledCharacterId,
  focusSourceId,
  focusFrameIndex,
  onCharacterChange,
}: MaterialManagerV4Props) {
  const domainQuery = useQuery({ queryKey: ["domain-v4"], queryFn: api.domainV4 });
  const refetchDomain = domainQuery.refetch;
  const domain = domainQuery.data;
  const [localCharacterId, setLocalCharacterId] = useState<string | null>(null);
  const [sourceId, setSourceId] = useState<string | null>(null);
  const [variantId, setVariantId] = useState<string | null>(null);
  const [selection, setSelection] = useState(new Set<number>());
  const [selectionAnchor, setSelectionAnchor] = useState<number | null>(null);
  const [playingVideo, setPlayingVideo] = useState(false);
  const [previewBackground, setPreviewBackground] = useState<PreviewBackground>("checker");
  const [previewLayer, setPreviewLayer] = useState<"rgba" | "emission">("rgba");
  const [previewZoom, setPreviewZoom] = useState(1);
  const [previewPan, setPreviewPan] = useState({ x: 0, y: 0 });
  const [previewPanning, setPreviewPanning] = useState(false);
  const [materialType, setMaterialType] = useState<"character" | "effect">("character");
  const [screenFirst, setScreenFirst] = useState(true);
  const [protectSubjectColor, setProtectSubjectColor] = useState(false);
  const [screenColor, setScreenColor] = useState("#00ff00");
  const [advanced, setAdvanced] = useState(false);
  const [thresholdLow, setThresholdLow] = useState(18);
  const [thresholdHigh, setThresholdHigh] = useState(62);
  const [feather, setFeather] = useState(3);
  const [spillStrength, setSpillStrength] = useState(0.72);
  const [busy, setBusy] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [pendingQuality, setPendingQuality] = useState<"basic" | "high" | "ultra" | null>(null);
  const [uploadProgress, setUploadProgress] = useState<number | null>(null);
  const [sheet, setSheet] = useState<PhotoshopSheetV4 | null>(null);
  const [photoshopBatchSize, setPhotoshopBatchSize] = useState(32);
  const [frameTileSize, setFrameTileSize] = useState(() => {
    const stored = Number(window.localStorage.getItem("rotoweave.material-grid-tile-size"));
    return Number.isFinite(stored) && stored >= 72 && stored <= 240 ? stored : 132;
  });
  const importInput = useRef<HTMLInputElement>(null);
  const syncInput = useRef<HTMLInputElement>(null);
  const photoshopInput = useRef<HTMLInputElement>(null);
  const seenTerminalJobs = useRef(new Set<string>());
  const frameButtons = useRef(new Map<number, HTMLButtonElement>());
  const previewElement = useRef<HTMLElement>(null);
  const previewDrag = useRef<{ pointerId: number; x: number; y: number; panX: number; panY: number; axis: DragAxis } | null>(null);
  const uploadAbort = useRef<AbortController | null>(null);
  useEscapeClose(() => onClose?.(), mode === "overlay" && !busy);
  useAutoDismissNoticeV4(notice, () => setNotice(null));

  const characters = useMemo(() => domain?.characters ?? [], [domain?.characters]);
  const characterId = controlledCharacterId ?? localCharacterId;
  const character = characters.find((item) => item.id === characterId) ?? characters[0];
  const sources = useMemo(
    () => (domain?.materialSources ?? []).filter((item) => item.characterId === character?.id),
    [character?.id, domain?.materialSources],
  );
  const source = sources.find((item) => item.id === sourceId) ?? sources[0];
  const allSourceVariants = useMemo(
    () => (domain?.materialVariants ?? []).filter((item) => item.sourceId === source?.id),
    [domain?.materialVariants, source?.id],
  );
  const variantById = useMemo(
    () => new Map(allSourceVariants.map((item) => [item.id, item])),
    [allSourceVariants],
  );
  const latestVariantId = source?.variantIds.at(-1);
  const latestVariant = variantById.get(latestVariantId ?? "") ?? null;
  const variant = variantId === latestVariant?.id ? latestVariant : null;
  const processedFrames = useMemo(
    () => projectLatestMaterialFrames(
      (source?.frames ?? []).map((frame) => frame.id),
      source?.variantIds ?? [],
      allSourceVariants,
    ),
    [allSourceVariants, source?.frames, source?.variantIds],
  );
  const visibleFrames = useMemo(() => (variant
    ? projectMaterialFrameDisplay(
      (source?.frames ?? []).map((frame) => frame.id),
      source?.variantIds ?? [],
      allSourceVariants,
    ).map((item) => ({
      ...item,
      key: item.processed
        ? variantById.get(item.variantId ?? "")?.frames[item.variantIndex]?.id ?? `${item.variantId}:${item.sourceIndex}`
        : source?.frames[item.sourceIndex]?.id ?? `source:${item.sourceIndex}`,
    }))
    : (source?.frames ?? []).map((frame, sourceIndex) => ({
      sourceIndex,
      variantId: null,
      variantIndex: -1,
      processed: false,
      key: frame.id,
    }))), [allSourceVariants, source?.frames, source?.variantIds, variant, variantById]);
  const visibleSourceIndexes = useMemo(
    () => new Set(visibleFrames.map((item) => item.sourceIndex)),
    [visibleFrames],
  );
  const selected = selectedFrameSequence(selection).filter((index) => visibleSourceIndexes.has(index));
  const previewIndex = selected[0] ?? 0;
  const previewEntry = visibleFrames.find((item) => item.sourceIndex === previewIndex) ?? visibleFrames[0];
  const previewVariant = previewEntry?.variantId ? variantById.get(previewEntry.variantId) : undefined;
  const hasPreviewEmission = Boolean(
    variant && previewEntry && previewVariant?.frames[previewEntry.variantIndex]?.emission,
  );
  const activeJob = jobs.find((job) =>
    job.source_id === source?.id && ["queued", "running", "cancelling"].includes(job.status),
  );
  const activeImportJob = jobs.find((job) =>
    job.type === "material_import" && job.character_id === character?.id
    && ["queued", "running", "cancelling"].includes(job.status),
  );

  useEffect(() => {
    window.localStorage.setItem("rotoweave.material-grid-tile-size", String(frameTileSize));
  }, [frameTileSize]);

  useEffect(() => {
    if (!focusSourceId || !Number.isInteger(focusFrameIndex)) return;
    const target = sources.find((item) => item.id === focusSourceId);
    if (!target || Number(focusFrameIndex) < 0 || Number(focusFrameIndex) >= target.frames.length) return;
    const index = Number(focusFrameIndex);
    const requestId = requestAnimationFrame(() => {
      setSourceId(target.id);
      setVariantId(null);
      setSelection(new Set([index]));
      setSelectionAnchor(index);
      setPlayingVideo(false);
    });
    return () => cancelAnimationFrame(requestId);
  }, [focusFrameIndex, focusSourceId, sources]);

  useEffect(() => {
    if (!focusSourceId || source?.id !== focusSourceId || !Number.isInteger(focusFrameIndex)) return;
    const requestId = requestAnimationFrame(() => frameButtons.current.get(Number(focusFrameIndex))?.scrollIntoView({ block: "center", inline: "center" }));
    return () => cancelAnimationFrame(requestId);
  }, [focusFrameIndex, focusSourceId, source?.id]);

  useEffect(() => subscribeToJobs(undefined, (nextJobs) => {
    setJobs(nextJobs);
    for (const job of nextJobs) {
      if (!["completed", "failed", "cancelled"].includes(job.status) || seenTerminalJobs.current.has(job.id)) continue;
      seenTerminalJobs.current.add(job.id);
      if (job.type === "material_basic" || job.type === "material_remote" || job.type === "material_import") {
        void (async () => {
          const refreshed = await refetchDomain();
          if (
            job.status === "completed"
            && (job.type === "material_basic" || job.type === "material_remote")
            && job.source_id === source?.id
          ) {
            const refreshedSource = refreshed.data?.materialSources.find((item) => item.id === job.source_id);
            const latestId = refreshedSource?.variantIds.at(-1);
            if (latestId) setVariantId(latestId);
            setPlayingVideo(false);
            setPreviewLayer("rgba");
          }
        })();
        const imported = job.type === "material_import";
        setNotice(job.status === "completed"
          ? { tone: "success", message: imported ? "视频导入、抽帧和发布已完成。" : "本批处理完成；仅所选帧已发布并迁移对应动作引用。" }
          : { tone: "error", message: job.error || (imported ? "视频导入未完成。" : "素材处理未完成。") });
      }
    }
  }), [refetchDomain, source?.id]);

  function chooseCharacter(nextCharacterId: string) {
    setLocalCharacterId(nextCharacterId);
    onCharacterChange?.(nextCharacterId);
    setSourceId(null);
    setVariantId(null);
    setSelection(new Set());
    setSelectionAnchor(null);
    setPlayingVideo(false);
    setSheet(null);
    setPendingQuality(null);
  }

  function chooseSource(nextSourceId: string) {
    const next = sources.find((item) => item.id === nextSourceId);
    setSourceId(nextSourceId);
    setVariantId(null);
    setSelection(next?.frames.length ? new Set([0]) : new Set());
    setSelectionAnchor(next?.frames.length ? 0 : null);
    setPlayingVideo(false);
    setSheet(null);
    setPendingQuality(null);
  }

  function resetPreviewViewport() {
    setPreviewZoom(1);
    setPreviewPan({ x: 0, y: 0 });
  }

  useEffect(() => {
    const requestId = window.requestAnimationFrame(resetPreviewViewport);
    return () => window.cancelAnimationFrame(requestId);
  }, [source?.id, variant?.id, previewIndex, previewLayer]);

  useEffect(() => {
    const interrupt = () => {
      const drag = previewDrag.current;
      if (drag && previewElement.current?.hasPointerCapture(drag.pointerId)) {
        try { previewElement.current.releasePointerCapture(drag.pointerId); } catch { /* pointer capture already ended */ }
      }
      previewDrag.current = null;
      setPreviewPanning(false);
    };
    window.addEventListener("blur", interrupt);
    document.addEventListener("visibilitychange", interrupt);
    return () => { window.removeEventListener("blur", interrupt); document.removeEventListener("visibilitychange", interrupt); };
  }, []);

  function zoomPreview(event: WheelEvent<HTMLElement>) {
    if (!source || playingVideo || selected.length > 1) return;
    event.preventDefault();
    const rect = previewElement.current?.getBoundingClientRect();
    if (!rect) return;
    const cursorX = event.clientX - (rect.left + rect.width / 2);
    const cursorY = event.clientY - (rect.top + rect.height / 2);
    const next = zoomCanvasViewportAtPoint(
      { x: previewPan.x, y: previewPan.y, scale: previewZoom },
      { x: cursorX, y: cursorY },
      Math.exp(-event.deltaY * 0.0015),
    );
    setPreviewPan({ x: next.x, y: next.y });
    setPreviewZoom(next.scale);
  }

  function beginPreviewPan(event: ReactPointerEvent<HTMLElement>) {
    if (!source || playingVideo || selected.length > 1 || event.button !== 0) return;
    event.preventDefault();
    previewDrag.current = {
      pointerId: event.pointerId,
      x: event.clientX,
      y: event.clientY,
      panX: previewPan.x,
      panY: previewPan.y,
      axis: null,
    };
    event.currentTarget.setPointerCapture(event.pointerId);
    setPreviewPanning(true);
  }

  function movePreviewPan(event: ReactPointerEvent<HTMLElement>) {
    const drag = previewDrag.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    event.preventDefault();
    const constrained = constrainDragPoint(
      { x: drag.panX, y: drag.panY },
      {
      x: drag.panX + event.clientX - drag.x,
      y: drag.panY + event.clientY - drag.y,
      },
      event.shiftKey,
      drag.axis,
    );
    drag.axis = constrained.axis;
    setPreviewPan(constrained.point);
  }

  function endPreviewPan(event: ReactPointerEvent<HTMLElement>) {
    if (previewDrag.current?.pointerId !== event.pointerId) return;
    event.preventDefault();
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
    previewDrag.current = null;
    setPreviewPanning(false);
  }

  function frameUrl(index: number): string {
    const entry = visibleFrames.find((item) => item.sourceIndex === index);
    const projectedVariant = entry?.variantId ? variantById.get(entry.variantId) : undefined;
    return variant && entry?.processed && projectedVariant
      ? api.materialVariantFrameUrl(
        projectedVariant.id,
        entry.variantIndex,
        previewLayer === "emission" && projectedVariant.frames[entry.variantIndex]?.emission ? "emission" : "rgba",
      )
      : source ? api.materialSourceFrameUrl(source.id, index) : "";
  }

  function thumbnailUrl(index: number): string {
    const entry = visibleFrames.find((item) => item.sourceIndex === index);
    const projectedVariant = entry?.variantId ? variantById.get(entry.variantId) : undefined;
    return variant && entry?.processed && projectedVariant
      ? api.materialVariantFrameUrl(projectedVariant.id, entry.variantIndex)
      : source ? api.materialSourceThumbnailUrl(source.id, index) : "";
  }

  function chooseFrame(index: number, event: MouseEvent) {
    const result = selectMaterialFrame(selection, index, selectionAnchor, {
      shift: event.shiftKey,
      additive: event.ctrlKey || event.metaKey,
    });
    setSelection(result.selected);
    setSelectionAnchor(result.anchor);
    setPlayingVideo(false);
    const entry = visibleFrames.find((item) => item.sourceIndex === index);
    const projectedVariant = entry?.variantId ? variantById.get(entry.variantId) : undefined;
    if (!variant || !entry || !projectedVariant?.frames[entry.variantIndex]?.emission) setPreviewLayer("rgba");
  }

  async function refresh() {
    await domainQuery.refetch();
  }

  async function importFiles(files: File[], sync = false) {
    if (!character || !domain) return;
    const videos = files.filter(isVideo);
    if (!videos.length) {
      setNotice({ tone: "error", message: "请选择受支持的视频文件。" });
      return;
    }
    setBusy(sync ? "sync" : "import");
    setUploadProgress(0);
    const controller = new AbortController();
    uploadAbort.current = controller;
    try {
      const job = await api.createMaterialImportJob(
        character.id,
        videos,
        domain.revisionId,
        24,
        setUploadProgress,
        controller.signal,
      );
      setJobs((current) => [job, ...current.filter((item) => item.id !== job.id)]);
      setNotice({ tone: "success", message: `${videos.length} 个视频已上传，正在后台校验和抽帧。` });
    } catch (error) {
      setNotice({ tone: "error", message: error instanceof DOMException && error.name === "AbortError" ? "视频上传已取消。" : error instanceof Error ? error.message : "视频导入失败。" });
    } finally {
      uploadAbort.current = null;
      setUploadProgress(null);
      setBusy(null);
    }
  }

  async function createCharacter() {
    if (!domain) return;
    const name = window.prompt("角色名称", `角色 ${characters.length + 1}`)?.trim();
    if (!name) return;
    setBusy("character");
    try {
      const result = await api.createDomainCharacter(name, domain.revisionId);
      await refresh();
      chooseCharacter(result.character.id);
    } catch (error) {
      setNotice({ tone: "error", message: error instanceof Error ? error.message : "创建角色失败。" });
    } finally {
      setBusy(null);
    }
  }

  async function removeSource() {
    if (!source || !domain || !window.confirm(`删除素材“${source.displayName}”及其全部处理版本和资产？任一版本仍被动作引用时将不会删除任何内容。`)) return;
    setBusy("delete");
    try {
      await api.deleteMaterialSource(source.id, domain.revisionId);
      setSourceId(null);
      await refresh();
      setNotice({ tone: "success", message: "素材已删除。" });
    } catch (error) {
      setNotice({ tone: "error", message: error instanceof Error ? error.message : "素材删除失败。" });
    } finally {
      setBusy(null);
    }
  }

  const processingSettings = useMemo(() => ({
    material_type: materialType,
    chroma: {
      screen_samples: screenFirst ? [{ rgb: hexToRgb(screenColor), color_space: "srgb" }] : [],
      threshold_low: thresholdLow,
      threshold_high: Math.max(thresholdLow + 1, thresholdHigh),
      feather,
      cleanup_radius: 2,
      spill_strength: spillStrength,
      key_mode: protectSubjectColor ? "preserve_subject_screen_color" : "clean_screen",
    },
    ai_assist: true,
  }), [feather, materialType, protectSubjectColor, screenColor, screenFirst, spillStrength, thresholdHigh, thresholdLow]);

  async function startProcessing(quality: "basic" | "high" | "ultra") {
    if (!source || !domain || !selected.length) {
      setNotice({ tone: "error", message: "请至少选择一帧后再创建处理任务。" });
      return;
    }
    setBusy(quality);
    try {
      const job = quality === "basic"
        ? await api.createMaterialBasicJob(source.id, domain.revisionId, { quality: "basic", ...processingSettings }, selected)
        : await api.createMaterialRemoteJob(source.id, domain.revisionId, quality, processingSettings, selected);
      setJobs((current) => [job, ...current.filter((item) => item.id !== job.id)]);
      setNotice({ tone: "success", message: `${quality.toUpperCase()} 任务已进入队列。` });
      setPendingQuality(null);
    } catch (error) {
      setNotice({ tone: "error", message: error instanceof Error ? error.message : "处理任务提交失败。" });
    } finally {
      setBusy(null);
    }
  }

  async function exportPhotoshop() {
    if (!source) return;
    setBusy("photoshop-export");
    try {
      const frameIndexes = selected.length ? selected : source.frames.map((_, index) => index);
      const result = await api.exportMaterialPhotoshopSheet(source.id, variant?.id ?? null, frameIndexes, photoshopBatchSize);
      setSheet(result);
      setNotice({ tone: "success", message: `已把 ${result.selectedFrameCount} 帧导出为 ${result.batchCount} 批；请下载各批次并一次选择全部编辑结果回导。` });
    } catch (error) {
      setNotice({ tone: "error", message: error instanceof Error ? error.message : "Photoshop 拼图导出失败。" });
    } finally {
      setBusy(null);
    }
  }

  async function importPhotoshop(event: ChangeEvent<HTMLInputElement>) {
    const files = [...(event.target.files ?? [])];
    event.target.value = "";
    if (!files.length || !source || !domain) return;
    setBusy("photoshop-import");
    try {
      const result = await api.importMaterialPhotoshopSheet(source.id, files, domain.revisionId);
      await refresh();
      setVariantId(result.variant.id);
      setSheet(null);
      const migrated = result.variant.migration?.actionFrameCount ?? 0;
      setNotice({ tone: "success", message: `Photoshop 拼图已发布为最新处理结果，并迁移 ${migrated} 个动作帧引用。` });
    } catch (error) {
      setNotice({ tone: "error", message: error instanceof Error ? error.message : "Photoshop 拼图回导失败。" });
    } finally {
      setBusy(null);
    }
  }

  function dropFiles(event: DragEvent) {
    event.preventDefault();
    setDragging(false);
    void importFiles([...event.dataTransfer.files]);
  }

  if (domainQuery.isLoading) {
    return <div className={mode === "page" ? "material-manager-page" : "material-manager-overlay"}><div className="material-manager-loading"><Loader2 className="spin" />正在加载 4.0 素材工作区</div></div>;
  }

  return <div className={mode === "page" ? "material-manager-page" : "material-manager-overlay"} {...(mode === "overlay" ? { role: "dialog", "aria-modal": true } : { role: "region" })} aria-label="4.0 素材管理">
    <header className="material-manager-header">
      <div><small>RotoWeave 4.0</small><strong>素材管理</strong></div>
      <span>{source ? `${source.displayName} · ${source.frames.length} 帧 · ${latestVariant ? "已有处理后结果" : "尚未处理"}` : "导入视频开始建立素材"}</span>
      {mode === "overlay" ? <button type="button" aria-label="关闭素材管理" onClick={() => onClose?.()}><X size={18} /></button> : null}
    </header>

    {mode === "overlay" ? <aside className="material-manager-roles">
      <div><strong>角色</strong><button type="button" aria-label="新增角色" disabled={Boolean(busy)} onClick={() => void createCharacter()}><Plus size={14} /></button></div>
      {characters.map((item) => <button key={item.id} type="button" className={item.id === character?.id ? "active" : ""} onClick={() => chooseCharacter(item.id)}>
        <Film size={14} /><span>{item.name}</span><small>{item.materialSourceIds.length}</small>
      </button>)}
      {!characters.length ? <p>先新增一个角色，再导入素材视频。</p> : null}
    </aside> : null}

    <main className="material-manager-main" onDragEnter={(event) => { event.preventDefault(); setDragging(true); }} onDragOver={(event) => event.preventDefault()} onDragLeave={(event) => { if (event.currentTarget === event.target) setDragging(false); }} onDrop={dropFiles}>
      <aside className="material-manager-sources">
        <header><strong>视频列表</strong><span>{sources.length}</span></header>
        <div className="material-manager-source-actions">
          <button type="button" disabled={!character || Boolean(busy)} onClick={() => importInput.current?.click()}><Upload size={13} />导入</button>
          <button type="button" disabled={!character || Boolean(busy)} onClick={() => syncInput.current?.click()}><FolderSync size={13} />同步目录</button>
          <button type="button" disabled={!source || Boolean(busy)} onClick={() => void removeSource()}><Trash2 size={13} />删除</button>
        </div>
        <input ref={importInput} hidden type="file" accept="video/*,.mkv,.avi,.m4v" multiple onChange={(event) => { void importFiles([...(event.target.files ?? [])]); event.target.value = ""; }} />
        <input ref={syncInput} hidden type="file" accept="video/*,.mkv,.avi,.m4v" multiple {...{ webkitdirectory: "" }} onChange={(event) => { void importFiles([...(event.target.files ?? [])], true); event.target.value = ""; }} />
        <nav>{sources.map((item) => <button key={item.id} type="button" className={item.id === source?.id ? "active" : ""} onClick={() => chooseSource(item.id)}>
          <img src={api.materialSourceThumbnailUrl(item.id, 0)} alt="" /><span><strong>{item.displayName}</strong><small>{item.frames.length} 帧 · {item.variantIds.length ? "有处理后" : "仅原始"}</small></span>
        </button>)}</nav>
        {!sources.length ? <div className="material-manager-empty"><Upload size={26} /><strong>拖入视频</strong><span>或使用导入、同步目录</span></div> : null}
      </aside>

      <section className="material-manager-browser">
        <header>
          <div><strong>{source?.displayName ?? "素材帧"}</strong><small>Shift 连选 · Ctrl 增减选择</small><div className="material-grid-density" aria-label="缩略图大小"><button type="button" aria-label="缩小缩略图以显示更多帧" disabled={frameTileSize <= 72} onClick={() => setFrameTileSize((value) => Math.max(72, value - 12))}><Minus size={11} /></button><input type="range" min={72} max={240} step={4} value={frameTileSize} onChange={(event) => setFrameTileSize(Number(event.target.value))} aria-label="缩略图大小" /><button type="button" aria-label="放大缩略图以显示更清晰" disabled={frameTileSize >= 240} onClick={() => setFrameTileSize((value) => Math.min(240, value + 12))}><Plus size={11} /></button><output>{frameTileSize}px</output></div></div>
          <nav><button type="button" className={!variant ? "active" : ""} onClick={() => { setVariantId(null); setPreviewLayer("rgba"); setPendingQuality(null); if (!selection.size && source.frames.length) { setSelection(new Set([0])); setSelectionAnchor(0); } }}>原始素材</button>{latestVariant ? <button type="button" className={latestVariant.id === variant?.id ? "active" : ""} onClick={() => { setVariantId(latestVariant.id); setPreviewLayer("rgba"); setPendingQuality(null); const retained = [...selection].filter((index) => index >= 0 && index < source.frames.length); if (!retained.length && source.frames.length) { setSelection(new Set([0])); setSelectionAnchor(0); } }}>处理后 <small>已处理 {processedFrames.length} / {source.frames.length}</small></button> : null}</nav>
        </header>
        {source ? <div className="material-manager-grid" style={{ "--material-frame-size": `${frameTileSize}px` } as CSSProperties}>{visibleFrames.map(({ sourceIndex, variantId: projectedVariantId, processed, key }) => <button ref={(node) => { if (node) frameButtons.current.set(sourceIndex, node); else frameButtons.current.delete(sourceIndex); }} key={key} type="button" className={selection.has(sourceIndex) ? "selected" : ""} aria-pressed={selection.has(sourceIndex)} onClick={(event) => chooseFrame(sourceIndex, event)}>
          <img src={thumbnailUrl(sourceIndex)} alt={`${source.displayName} 第 ${sourceIndex + 1} 帧`} draggable={false} />{variant ? <span className={`material-frame-status ${processed ? "processed" : "source"}`}>{processed ? "已处理" : "源图"}</span> : null}{selection.has(sourceIndex) ? <span className="material-selection-mark"><Check size={13} />已选</span> : null}<small>{sourceIndex + 1}</small><em>{variant && processed && projectedVariantId ? variantLabel(variantById.get(projectedVariantId) ?? variant) : "源图"}</em>
        </button>)}</div> : <div className="material-manager-empty large"><ImageIcon size={42} /><strong>还没有素材</strong><span>拖入视频后会同步抽帧并生成源缩略图。</span></div>}
        {dragging ? <div className="material-manager-drop"><Upload size={38} /><strong>松开以导入视频</strong></div> : null}
      </section>

      <aside className="material-manager-inspector">
        <section ref={previewElement} className={`material-manager-preview background-${previewBackground}${source && !playingVideo && selected.length <= 1 ? " interactive" : ""}${previewPanning ? " panning" : ""}`} onWheel={zoomPreview} onPointerDown={beginPreviewPan} onPointerMove={movePreviewPan} onPointerUp={endPreviewPan} onPointerCancel={endPreviewPan} onLostPointerCapture={() => { previewDrag.current = null; setPreviewPanning(false); }}>
          {source && playingVideo ? <video key={source.id} src={api.materialSourceVideoUrl(source.id)} controls autoPlay /> : source && selected.length <= 1 && previewEntry ? <><img src={frameUrl(previewEntry.sourceIndex)} draggable={false} onDragStart={(event) => event.preventDefault()} style={{ transform: `translate(${previewPan.x}px, ${previewPan.y}px) scale(${previewZoom})` }} alt={`预览第 ${previewEntry.sourceIndex + 1} 帧${previewLayer === "emission" && hasPreviewEmission ? "特效层" : ""}`} /><span className="material-preview-pan-hint">按住拖拽平移 · 滚轮缩放 · 适配复位</span></> : source ? <div className="material-manager-sequence">{selected.map((index, order) => <button key={index} type="button" onClick={() => { setSelection(new Set([index])); setSelectionAnchor(index); setPlayingVideo(false); const entry = visibleFrames.find((item) => item.sourceIndex === index); const projectedVariant = entry?.variantId ? variantById.get(entry.variantId) : undefined; if (!variant || !entry?.processed || !projectedVariant?.frames[entry.variantIndex]?.emission) setPreviewLayer("rgba"); }}><b>{order + 1}</b><img src={thumbnailUrl(index)} alt={`选择序列 ${order + 1}`} /><span>帧 {index + 1}</span></button>)}</div> : <div className="material-manager-empty"><ImageIcon size={30} />选择素材后预览</div>}
        </section>
        <div className="material-manager-preview-meta">
          <span>{selected.length <= 1 ? `单帧预览 · #${previewIndex + 1}` : `选择序列 · ${selected.length} 帧`}</span>
          <div className="material-manager-preview-tools" aria-label="素材检查工具">
            <button type="button" className={previewBackground === "checker" ? "active" : ""} onClick={() => setPreviewBackground("checker")}>透明</button>
            <button type="button" className={previewBackground === "white" ? "active" : ""} onClick={() => setPreviewBackground("white")}>白底</button>
            <button type="button" className={previewBackground === "black" ? "active" : ""} onClick={() => setPreviewBackground("black")}>黑底</button>
            <button type="button" disabled={!source || playingVideo || selected.length > 1} onClick={resetPreviewViewport}>适配 {Math.round(previewZoom * 100)}%</button>
            <button type="button" className={previewLayer === "emission" && hasPreviewEmission ? "active" : ""} disabled={!hasPreviewEmission || playingVideo || selected.length > 1} title={hasPreviewEmission ? "切换基础/特效层" : "当前帧没有特效层"} onClick={() => setPreviewLayer((value) => value === "rgba" ? "emission" : "rgba")}>特效层</button>
            <button type="button" disabled={!source} onClick={() => setPlayingVideo((value) => !value)}>{playingVideo ? <Square size={12} /> : <Play size={12} />}{playingVideo ? "返回帧" : "播放视频"}</button>
          </div>
        </div>

        <section className="material-manager-controls">
          <div className="material-manager-control-grid">
            <button type="button" className={materialType === "character" ? "active" : ""} onClick={() => setMaterialType("character")}>角色</button>
            <button type="button" className={materialType === "effect" ? "active" : ""} onClick={() => setMaterialType("effect")}>特效</button>
            <button type="button" className={screenFirst ? "active" : ""} onClick={() => setScreenFirst((value) => !value)}>纯色幕优先</button>
            <button type="button" className={protectSubjectColor ? "active" : ""} onClick={() => setProtectSubjectColor((value) => !value)}>保护主体颜色</button>
          </div>
          <label className="material-manager-color"><span>幕布采样颜色</span><input type="color" value={screenColor} onChange={(event) => setScreenColor(event.target.value)} /><code>{screenColor.toUpperCase()}</code></label>
          <button type="button" className="material-manager-advanced-toggle" onClick={() => setAdvanced((value) => !value)}><SlidersHorizontal size={13} />高级设置</button>
          {advanced ? <div className="material-manager-advanced">
            <label>阈值下限<input type="number" min={0} max={254} value={thresholdLow} onChange={(event) => setThresholdLow(Number(event.target.value))} /></label>
            <label>阈值上限<input type="number" min={1} max={255} value={thresholdHigh} onChange={(event) => setThresholdHigh(Number(event.target.value))} /></label>
            <label>羽化<input type="number" min={0} max={31} value={feather} onChange={(event) => setFeather(Number(event.target.value))} /></label>
            <label>去溢色<PercentDraftInputV4 value={spillStrength} minimum={0} maximum={1} onCommit={setSpillStrength} /></label>
          </div> : null}
          <div className="material-manager-quality">
            <button type="button" className={pendingQuality === "basic" ? "active" : ""} disabled={!source || !selected.length || Boolean(activeJob) || Boolean(busy)} onClick={() => setPendingQuality("basic")}>Basic</button>
            <button type="button" className={pendingQuality === "high" ? "active" : ""} disabled={!source || !selected.length || Boolean(activeJob) || Boolean(busy)} onClick={() => setPendingQuality("high")}>High</button>
            <button type="button" className={pendingQuality === "ultra" ? "active" : ""} disabled={!source || !selected.length || Boolean(activeJob) || Boolean(busy)} onClick={() => setPendingQuality("ultra")}>Ultra</button>
          </div>
          {pendingQuality ? <div className="material-manager-quality-confirm" role="group" aria-label="确认抠图"><span>已选择 <strong>{pendingQuality.toUpperCase()}</strong>，将处理本次选择的 <strong>{selected.length}</strong> 帧{variant ? "（包含已处理帧的重新处理）" : ""}。</span><button type="button" className="primary" disabled={!selected.length || Boolean(activeJob) || Boolean(busy)} onClick={() => void startProcessing(pendingQuality)}>确认抠图</button><button type="button" disabled={Boolean(busy)} onClick={() => setPendingQuality(null)}>取消</button></div> : null}
          <div className="material-manager-photoshop">
            <button type="button" disabled={!source || Boolean(busy)} onClick={() => void exportPhotoshop()}><Download size={13} />PS 拼图</button>
            <button type="button" disabled={!source || Boolean(busy)} onClick={() => photoshopInput.current?.click()}><Upload size={13} />回导处理后</button>
            <label>每批帧数<input type="number" min={1} max={128} value={photoshopBatchSize} onChange={(event) => setPhotoshopBatchSize(Math.max(1, Math.min(128, Number(event.target.value) || 1)))} /></label>
            <input ref={photoshopInput} hidden multiple type="file" accept="image/png,.png" onChange={(event) => void importPhotoshop(event)} />
          </div>
          {sheet ? <div className="material-manager-sheet-downloads"><span>拼图批次</span>{sheet.sheets.map((item) => <a key={item.batchIndex} href={mediaUrl(item.downloadUrl)} target="_blank" rel="noreferrer" download>下载 {item.batchIndex + 1}/{sheet.batchCount}</a>)}</div> : null}
          {activeJob || activeImportJob ? <div className="material-manager-job"><div><Loader2 className="spin" size={14} /><strong>{(activeJob ?? activeImportJob)?.type === "material_import" ? "视频导入与抽帧" : activeJob?.type === "material_remote" ? "远程处理" : "本地 Basic"}</strong><span>{Math.round(((activeJob ?? activeImportJob)?.progress ?? 0) * 100)}%</span></div><progress max={1} value={(activeJob ?? activeImportJob)?.progress ?? 0} /><small>{(activeJob ?? activeImportJob)?.stage}</small><button type="button" onClick={() => void api.cancelJob((activeJob ?? activeImportJob)!.id)}>取消</button></div> : uploadProgress !== null ? <div className="material-manager-job"><div><Loader2 className="spin" size={14} /><strong>正在上传视频</strong><span>{Math.round(uploadProgress * 100)}%</span></div><progress max={1} value={uploadProgress} /><small>上传完成后将进入校验、抽帧和发布阶段</small><button type="button" onClick={() => uploadAbort.current?.abort()}>取消上传</button></div> : busy ? <div className="material-manager-job"><div><Loader2 className="spin" size={14} /><strong>正在准备</strong></div></div> : null}
        </section>
      </aside>
    </main>
    {notice ? <button type="button" className={`material-manager-notice ${notice.tone}`} onClick={() => setNotice(null)}>{notice.tone === "success" ? <CheckCircle2 size={15} /> : <AlertTriangle size={15} />}{notice.message}</button> : null}
  </div>;
}
