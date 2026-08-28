import { ChevronDown, ChevronRight, Loader2, Moon, Save, Sun, Trash2, Upload } from "lucide-react";
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
/* eslint-disable react-hooks/set-state-in-effect -- calibration drafts reset when the selected character aggregate changes. */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { GlobalSettingsCanvas, type GlobalCanvasDragMode } from "../components/global-settings-canvas";
import { NumericDraftInput } from "../components/numeric-draft-input";
import { PercentDraftInputV4 } from "../components/percent-draft-input-v4";
import { api } from "../lib/api";
import { usePageSaveCommandV4 } from "../lib/page-save-command-v4";
import { mergeDomainCharacterV4 } from "../lib/domain-cache-v4";
import { CANONICAL_PIXELS_PER_UNIT } from "../lib/protocol-contract";
import { convertSizeProfileUnit, profileSnapshotFromPreset, sizeProfilePixels, sizeProfileWorld, type SizeProfileV4, type SizeUnitModeV4 } from "../lib/size-profile-v4";
import { useWorkspaceStore } from "../lib/store";
import type { CoreReference, DomainCharacterV4, ShadowPreview, SizeProfile, WorkspaceDomainV4 } from "../lib/types";
import { useAutoDismissNoticeV4 } from "../lib/use-auto-dismiss-notice-v4";

const TOOL_LABELS: Record<GlobalCanvasDragMode, string> = { size: "尺寸框", horizon: "地平线", shadow: "阴影线", image: "核心图", viewport: "画布" };
const TOOL_SHORTCUTS: GlobalCanvasDragMode[] = ["viewport", "size", "horizon", "shadow", "image"];

function isEditableTarget(target: EventTarget | null) {
  return Boolean((target as HTMLElement | null)?.closest("input,textarea,select,[contenteditable=true]"));
}

function CollapsiblePanel({ title, children }: { title: string; children: ReactNode }) {
  const [open, setOpen] = useState(false);
  return <fieldset className={open ? "expanded" : "collapsed"}>
    <legend><button type="button" aria-expanded={open} onClick={() => setOpen((value) => !value)}>{open ? <ChevronDown size={12} /> : <ChevronRight size={12} />}{title}</button></legend>
    {open ? children : null}
  </fieldset>;
}

export function GlobalSettingsV4({ character, domain, onRefresh }: { character: DomainCharacterV4; domain: WorkspaceDomainV4; onRefresh: () => Promise<unknown> }) {
  const queryClient = useQueryClient();
  const theme = useWorkspaceStore((state) => state.theme);
  const setTheme = useWorkspaceStore((state) => state.setTheme);
  const [calibration, setCalibration] = useState(character.calibration);
  const [shadow, setShadow] = useState(character.shadow);
  const [dragMode, setDragMode] = useState<GlobalCanvasDragMode>("viewport");
  const [guideVisibility, setGuideVisibility] = useState({ size: true, center: true, horizon: true, shadow: true });
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [shadowPreview, setShadowPreview] = useState<ShadowPreview | null>(null);
  const previewSequence = useRef(0);
  const calibrationSnapshot = useRef(character.calibration);
  const shadowSnapshot = useRef(character.shadow);
  const fileRef = useRef<HTMLInputElement>(null);
  const sizeSystemQuery = useQuery({ queryKey: ["size-system-v4"], queryFn: api.sizeSystem });
  const presets = sizeSystemQuery.data?.profiles ?? [];
  useAutoDismissNoticeV4(notice, () => setNotice(null));
  useEffect(() => {
    calibrationSnapshot.current = character.calibration;
    shadowSnapshot.current = character.shadow;
    setCalibration(character.calibration);
    setShadow(character.shadow);
  }, [character]);
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (isEditableTarget(event.target)) return;
      const match = /^(?:Digit|Numpad)([1-5])$/.exec(event.code);
      if (!match) return;
      const mode = TOOL_SHORTCUTS[Number(match[1]) - 1];
      setDragMode(mode);
      if (mode === "size" || mode === "horizon" || mode === "shadow") {
        setGuideVisibility((current) => ({ ...current, [mode]: true }));
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  const pixelsPerUnit = calibration.pixelsPerUnit || CANONICAL_PIXELS_PER_UNIT;
  const active = calibration.sizeProfiles.find((item) => item.id === calibration.activeSizeProfileId) ?? calibration.sizeProfiles[0];
  const pixels = sizeProfilePixels(active, pixelsPerUnit);
  const world = sizeProfileWorld(active, pixelsPerUnit);
  const canvasProfile = useMemo<SizeProfile>(() => ({ id: active.id, revisionId: domain.revisionId, name: active.name, width_world: world.width, height_world: world.height, unit_mode: active.unitMode, created_at: "", updated_at: "" }), [active.id, active.name, active.unitMode, domain.revisionId, world.height, world.width]);
  const core = calibration.coreReference;
  const coreReference = useMemo<CoreReference | null>(() => core ? ({ url: api.domainCoreReferenceUrl(character.id, core.sha256), width: core.width, height: core.height, scale: core.scale, origin_x: core.origin.x, origin_y: core.origin.y, revision: 1 }) : null, [character.id, core]);
  useEffect(() => {
    const sequence = ++previewSequence.current;
    if (!core) {
      setShadowPreview(null);
      return undefined;
    }
    const timer = window.setTimeout(() => {
      void api.previewDomainShadow(character.id, {
        useCoreReference: true,
        shadowStandardY: calibration.shadowStandardY,
        shadow,
      }).then((result) => {
        if (previewSequence.current === sequence) setShadowPreview(result.frames[0] ?? null);
      }).catch((error) => {
        if (previewSequence.current === sequence) {
          setShadowPreview(null);
          setNotice(error instanceof Error ? `阴影预览失败：${error.message}` : "阴影预览失败。");
        }
      });
    }, 120);
    return () => window.clearTimeout(timer);
  }, [calibration.shadowStandardY, character.id, core, shadow]);

  function changeCalibration(next: typeof calibration, _name: string, _mergeKey?: string) {
    if (JSON.stringify(next) === JSON.stringify(calibrationSnapshot.current)) return;
    calibrationSnapshot.current = next;
    setCalibration(next);
  }
  function changeShadow(next: typeof shadow, _name: string, _mergeKey?: string) {
    if (JSON.stringify(next) === JSON.stringify(shadowSnapshot.current)) return;
    shadowSnapshot.current = next;
    setShadow(next);
  }
  function updateActive(changes: Partial<SizeProfileV4>) {
    changeCalibration(
      { ...calibration, sizeProfiles: calibration.sizeProfiles.map((item) => item.id === active.id ? { ...item, ...changes } : item) },
      "调整角色尺寸",
      "size-profile",
    );
  }
  function chooseDragMode(mode: GlobalCanvasDragMode) {
    setDragMode(mode);
    if (mode === "size" || mode === "horizon" || mode === "shadow") {
      setGuideVisibility((current) => ({ ...current, [mode]: true }));
    }
  }
  function toggleGuide(guide: "size" | "center" | "horizon" | "shadow") {
    const visible = !guideVisibility[guide];
    setGuideVisibility((current) => ({ ...current, [guide]: visible }));
    if (!visible && dragMode === guide) setDragMode("viewport");
  }
  function selectPreset(presetId: string) {
    const preset = presets.find((item) => item.id === presetId);
    if (!preset) return;
    const profile = profileSnapshotFromPreset(preset, active.id, pixelsPerUnit);
    changeCalibration({ ...calibration, activeSizeProfileId: profile.id, sizeProfiles: [profile] }, "切换尺寸预设");
    setDragMode("size");
    setNotice(`已采用预设“${profile.name}”的快照。下面的尺寸修正只属于当前角色，不会覆盖预设。`);
  }
  function changeUnit(unitMode: SizeUnitModeV4) {
    updateActive(convertSizeProfileUnit(active, unitMode, pixelsPerUnit));
    setNotice(unitMode === "pixels" ? "已切换为像素输入；矩形实际尺寸保持不变。" : "已切换为 Unity 世界单位；按 100 px = 1 unit 换算。矩形实际尺寸保持不变。");
  }
  async function save(): Promise<boolean> {
    if (busy) return false;
    setBusy(true); setNotice(null);
    try { const result = await api.updateDomainCharacterSettings(character.id, { calibration, shadow }, domain.revisionId); if (!mergeDomainCharacterV4(queryClient, result.character, result.revisionId)) await onRefresh(); setNotice("全局校准与阴影基线已保存。"); return true; }
    catch (error) { setNotice(error instanceof Error ? error.message : "保存失败。"); return false; }
    finally { setBusy(false); }
  }
  usePageSaveCommandV4(save);
  async function upload(file: File) {
    setBusy(true); setNotice(null);
    try {
      const result = await api.uploadDomainCoreReference(character.id, file, domain.revisionId);
      setCalibration(result.character.calibration);
      setDragMode("image");
      await onRefresh();
      const imported = result.character.calibration.coreReference;
      setNotice(imported ? `核心角色图已导入 · ${imported.width}×${imported.height}px。现在可直接拖动核心图校准位置。` : "核心角色图已导入。");
    }
    catch (error) { setNotice(error instanceof Error ? error.message : "核心角色图导入失败。"); }
    finally { setBusy(false); }
  }
  async function removeCore() { setBusy(true); try { const result = await api.deleteDomainCoreReference(character.id, domain.revisionId); setCalibration(result.character.calibration); setDragMode("viewport"); await onRefresh(); setNotice("核心角色图已删除。"); } finally { setBusy(false); } }

  return <section className="global-settings-v4" aria-label="全局设置">
    <header><div><small>GLOBAL CALIBRATION</small><h1>{character.name} · 全局设置</h1></div><div className="v4-header-actions"><button type="button" className="secondary" onClick={() => setTheme(theme === "dark" ? "light" : "dark")}>{theme === "dark" ? <Sun size={14} /> : <Moon size={14} />}主题</button><button type="button" className="primary" disabled={busy} onClick={() => void save()}>{busy ? <Loader2 className="spin" size={14} /> : <Save size={14} />}保存</button></div></header>
    <div className="global-settings-v4-layout">
      <aside className="global-settings-v4-controls">
        <CollapsiblePanel title="尺寸矩形框">
          <label>共享预设<select value={presets.some((item) => item.id === active.presetId) ? active.presetId ?? "" : ""} onChange={(event) => selectPreset(event.target.value)}><option value="">{active.presetId ? "原预设已删除 · 使用角色快照" : "本地尺寸快照"}</option>{presets.map((item) => <option value={item.id} key={item.id}>{item.name}</option>)}</select></label>
          <div className="size-unit-switch" role="group" aria-label="尺寸输入单位"><button type="button" className={active.unitMode === "pixels" ? "active" : ""} onClick={() => changeUnit("pixels")}>像素 px</button><button type="button" className={active.unitMode === "unity" ? "active" : ""} onClick={() => changeUnit("unity")}>Unity unit</button></div>
          <div><label>宽（{active.unitMode === "pixels" ? "px" : "unit"}）<NumericDraftInput value={active.width} strictlyPositive maximum={active.unitMode === "pixels" ? 16384 : 163.84} onCommit={(width) => updateActive({ width: active.unitMode === "pixels" ? Math.round(width) : Math.round(width * pixelsPerUnit) / pixelsPerUnit })} /></label><label>高（{active.unitMode === "pixels" ? "px" : "unit"}）<NumericDraftInput value={active.height} strictlyPositive maximum={active.unitMode === "pixels" ? 16384 : 163.84} onCommit={(height) => updateActive({ height: active.unitMode === "pixels" ? Math.round(height) : Math.round(height * pixelsPerUnit) / pixelsPerUnit })} /></label></div>
          <p className="unit-equivalence"><strong>{Math.round(pixels.width)}×{Math.round(pixels.height)} px</strong><span>= {world.width.toFixed(3)}×{world.height.toFixed(3)} Unity unit · 运行时固定 100 PPU</span></p>
        </CollapsiblePanel>
        <CollapsiblePanel title="辅助线（px）"><label>尺寸框中心 X<NumericDraftInput value={calibration.sizeGuideCenterX} minimum={-65536} maximum={65536} onCommit={(value) => changeCalibration({ ...calibration, sizeGuideCenterX: value }, "调整尺寸框位置")} /></label><label>尺寸框底边 Y<NumericDraftInput value={calibration.sizeGuideBottomY} minimum={-65536} maximum={65536} onCommit={(value) => changeCalibration({ ...calibration, sizeGuideBottomY: value }, "调整尺寸框位置")} /></label><label>地平线 Y<NumericDraftInput value={calibration.alignmentHorizonY} minimum={-65536} maximum={65536} onCommit={(value) => changeCalibration({ ...calibration, alignmentHorizonY: value }, "调整地平线")} /></label><label>阴影线 Y<NumericDraftInput value={calibration.shadowStandardY} minimum={-65536} maximum={65536} onCommit={(value) => changeCalibration({ ...calibration, shadowStandardY: value }, "调整阴影线")} /></label><small>重合时画布会以蓝/橙双轨显示，数值仍保持原坐标。</small></CollapsiblePanel>
        <CollapsiblePanel title="核心角色形象"><input ref={fileRef} hidden type="file" accept="image/png" onChange={(event) => { const file = event.target.files?.[0]; if (file) void upload(file); event.currentTarget.value = ""; }} /><button type="button" className="secondary wide" disabled={busy} onClick={() => fileRef.current?.click()}>{busy ? <Loader2 className="spin" size={13} /> : <Upload size={13} />}导入透明 PNG</button>{core ? <><small className="core-reference-status">已载入 {core.width}×{core.height}px · {core.sha256.slice(0, 8)}</small><label>核心图缩放<PercentDraftInputV4 value={core.scale} minimum={.005} maximum={8} strictlyPositive onCommit={(value) => changeCalibration({ ...calibration, coreReference: { ...core, scale: value } }, "调整核心图缩放")} /></label><button type="button" className="danger wide" disabled={busy} onClick={() => void removeCore()}><Trash2 size={13} />删除核心图</button></> : <small>也可先在素材页处理一帧后导出透明 PNG 再导入。</small>}</CollapsiblePanel>
        <CollapsiblePanel title="阴影基础参数"><label className="check"><input type="checkbox" checked={shadow.enabled} onChange={(event) => changeShadow({ ...shadow, enabled: event.target.checked }, "切换全局阴影")} />启用阴影</label><label>颜色<input type="color" value={shadow.color} onChange={(event) => changeShadow({ ...shadow, color: event.target.value }, "调整全局阴影颜色", "shadow-color")} /></label><label>基础透明度<input type="range" min="0" max="1" step="0.01" value={shadow.baseOpacity} onChange={(event) => changeShadow({ ...shadow, baseOpacity: Number(event.target.value) }, "调整全局阴影透明度", "shadow-opacity")} /><output>{Math.round(shadow.baseOpacity * 100)}%</output></label><label>光源角度<NumericDraftInput value={shadow.lightAngleDegrees} onCommit={(value) => changeShadow({ ...shadow, lightAngleDegrees: value }, "调整全局光源角度")} /></label></CollapsiblePanel>
      </aside>
      <main className="global-settings-v4-canvas">
        <div className="v4-canvas-toolbar"><div role="group" aria-label="全局画布工具">{TOOL_SHORTCUTS.map((item, index) => <button type="button" key={item} className={dragMode === item ? "active" : ""} disabled={item === "image" && !core} onClick={() => chooseDragMode(item)}><kbd>{index + 1}</kbd>{TOOL_LABELS[item]}</button>)}</div><div className="action-guide-toggles" role="group" aria-label="全局辅助线显示"><button type="button" aria-pressed={guideVisibility.size} onClick={() => toggleGuide("size")}>尺寸框</button><button type="button" aria-pressed={guideVisibility.center} onClick={() => toggleGuide("center")}>中轴线</button><button type="button" aria-pressed={guideVisibility.horizon} onClick={() => toggleGuide("horizon")}>地平线</button><button type="button" aria-pressed={guideVisibility.shadow} onClick={() => toggleGuide("shadow")}>阴影线</button></div></div>
        <div className="v4-canvas-surface"><GlobalSettingsCanvas sessionKey={`v4-${character.id}`} alignmentHorizonY={calibration.alignmentHorizonY} shadowStandardY={calibration.shadowStandardY} sizeGuideCenterX={calibration.sizeGuideCenterX} sizeGuideBottomY={calibration.sizeGuideBottomY} sizeProfile={canvasProfile} coreReference={coreReference} coreScale={core?.scale} coreOrigin={core?.origin} shadow={shadowPreview} shadowColor={shadow.color} dragMode={dragMode} guideVisibility={guideVisibility} onAlignmentHorizonChange={(value) => changeCalibration({ ...calibrationSnapshot.current, alignmentHorizonY: value }, "拖动地平线", "canvas-horizon")} onShadowStandardYChange={(value) => changeCalibration({ ...calibrationSnapshot.current, shadowStandardY: value }, "拖动阴影线", "canvas-shadow")} onSizeGuidePositionChange={(point) => changeCalibration({ ...calibrationSnapshot.current, sizeGuideCenterX: point.x, sizeGuideBottomY: point.y }, "拖动尺寸框", "canvas-size")} onCoreOriginChange={(point) => { const currentCore = calibrationSnapshot.current.coreReference; if (currentCore) changeCalibration({ ...calibrationSnapshot.current, coreReference: { ...currentCore, origin: point } }, "拖动核心图", "canvas-core"); }} onCoreOriginCommit={() => undefined} /></div>
        {notice ? <button type="button" className="v4-notice" role="status" onClick={() => setNotice(null)}>{notice}</button> : null}
      </main>
    </div>
  </section>;
}
