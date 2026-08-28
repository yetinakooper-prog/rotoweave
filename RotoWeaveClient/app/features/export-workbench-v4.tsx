import { Archive, Download, Loader2, RefreshCw, Save } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
/* eslint-disable react-hooks/set-state-in-effect -- delivery drafts reset when the selected character aggregate changes. */
import { NumericDraftInput } from "../components/numeric-draft-input";
import { PercentDraftInputV4 } from "../components/percent-draft-input-v4";
import { api } from "../lib/api";
import { usePageSaveCommandV4 } from "../lib/page-save-command-v4";
import { mergeDomainCharacterV4 } from "../lib/domain-cache-v4";
import type { DomainCharacterV4, WorkspaceDomainV4 } from "../lib/types";
import { useAutoDismissNoticeV4 } from "../lib/use-auto-dismiss-notice-v4";

type Estimate = Awaited<ReturnType<typeof api.estimateDomainCharacterExport>>;
type Delivery = DomainCharacterV4["delivery"];
type ActionSetting = Delivery["actionSettings"][string];
function bytes(value: number) { return value > 1024 ** 2 ? `${(value / 1024 ** 2).toFixed(1)} MiB` : `${Math.round(value / 1024)} KiB`; }
function settingFor(delivery: Delivery, actionId: string): ActionSetting { return delivery.actionSettings[actionId] ?? { textureScale: delivery.globalTextureScale, runtimeLoop: true, includeInExport: true }; }

export function ExportWorkbenchV4({ character, domain, onRefresh }: { character: DomainCharacterV4; domain: WorkspaceDomainV4; onRefresh: () => Promise<unknown> }) {
  const queryClient = useQueryClient();
  const [delivery, setDelivery] = useState(character.delivery);
  const [estimate, setEstimate] = useState<Estimate | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const repairInput = useRef<HTMLInputElement>(null); const repairPage = useRef(0);
  const deliverySnapshot = useRef(character.delivery);
  const actions = domain.actions.filter((item) => item.characterId === character.id);
  const enabledCount = (actionId: string) => actions.find((item) => item.id === actionId)?.frameRefs.filter((frame) => frame.enabled !== false).length ?? 0;
  const configuredActions = actions.filter((action) => settingFor(delivery, action.id).includeInExport);
  const includedActions = configuredActions.filter((action) => enabledCount(action.id) > 0);
  useEffect(() => {
    deliverySnapshot.current = character.delivery;
    setDelivery(character.delivery);
  }, [character]);
  useAutoDismissNoticeV4(notice, () => setNotice(null));

  function changeDelivery(next: Delivery, _name: string, _mergeKey?: string) {
    if (JSON.stringify(next) === JSON.stringify(deliverySnapshot.current)) return;
    deliverySnapshot.current = next;
    setDelivery(next);
    setEstimate(null);
  }

  async function persist() {
    const validDefault = includedActions.some((action) => action.id === delivery.defaultActionId)
      ? delivery.defaultActionId
      : includedActions[0]?.id ?? null;
    const normalized = validDefault === delivery.defaultActionId ? delivery : { ...delivery, defaultActionId: validDefault };
    if (normalized !== delivery) {
      deliverySnapshot.current = normalized;
      setDelivery(normalized);
    }
    const result = await api.updateDomainCharacterSettings(character.id, { delivery: normalized }, domain.revisionId);
    if (!mergeDomainCharacterV4(queryClient, result.character, result.revisionId)) await onRefresh();
    return result.revisionId;
  }
  async function saveSettings(): Promise<boolean> {
    if (busy) return false;
    setBusy(true); setNotice(null);
    try { await persist(); setNotice("导出设置已保存。"); return true; }
    catch (error) { setNotice(error instanceof Error ? error.message : "导出设置保存失败。"); return false; }
    finally { setBusy(false); }
  }
  usePageSaveCommandV4(saveSettings);
  async function calculate() { setBusy(true); try { const revision = await persist(); setEstimate(await api.estimateDomainCharacterExport(character.id, revision, delivery.atlas.maxSize)); setNotice("预估已按参与导出的动作刷新。"); } catch (error) { setNotice(error instanceof Error ? error.message : "预估失败。"); } finally { setBusy(false); } }
  async function build() { setBusy(true); try { const revision = await persist(); await api.exportDomainCharacter(character.id, revision, delivery.atlas.maxSize); await onRefresh(); setNotice("当前图集与角色包已原子生成。"); } catch (error) { setNotice(error instanceof Error ? error.message : "导出失败。"); } finally { setBusy(false); } }
  async function repair(file: File) { setBusy(true); try { await api.repairDomainCharacterAtlasPage(character.id, repairPage.current, file, domain.revisionId); await onRefresh(); setNotice(`第 ${repairPage.current + 1} 页修复图已校验并成为新的 current generation。`); } catch (error) { setNotice(error instanceof Error ? error.message : "修复图回导失败。"); } finally { setBusy(false); } }
  function updateAction(actionId: string, patch: Partial<ActionSetting>) {
    const current = deliverySnapshot.current;
    const actionSettings = { ...current.actionSettings, [actionId]: { ...settingFor(current, actionId), ...patch } };
    let defaultActionId = current.defaultActionId;
      if (patch.includeInExport === false && defaultActionId === actionId) defaultActionId = actions.find((item) => item.id !== actionId && settingFor({ ...current, actionSettings }, item.id).includeInExport && enabledCount(item.id) > 0)?.id ?? null;
      if (patch.includeInExport === true && !defaultActionId && enabledCount(actionId) > 0) defaultActionId = actionId;
    changeDelivery({ ...current, defaultActionId, actionSettings }, "调整动作导出参数");
  }

  return <section className="export-workbench-v4" aria-label="导出设置">
    <header><div><small>DELIVERY WORKBENCH</small><h1>{character.name} · 导出设置</h1></div><div className="v4-header-actions"><button type="button" className="secondary" disabled={busy} onClick={() => void saveSettings()}><Save size={14} />保存设置</button><button type="button" className="secondary" disabled={busy || !includedActions.length} onClick={() => void calculate()}><RefreshCw size={14} />刷新预估</button><button type="button" className="primary" disabled={busy || !includedActions.length} onClick={() => void build()}>{busy ? <Loader2 className="spin" size={14} /> : <Archive size={14} />}构建当前角色包</button></div></header>
    <div className="export-workbench-v4-layout">
      <aside><fieldset><legend>动画交付参数</legend><p className="export-selection-summary">已配置 <strong>{configuredActions.length}</strong> 个动作，当前 <strong>{includedActions.length}</strong> 个含启用帧并可交付</p><p className="export-texture-scale-hint">纹理比例只控制图集清晰度，不改变 Unity 显示尺寸。</p><label>默认动作<select value={includedActions.some((action) => action.id === delivery.defaultActionId) ? delivery.defaultActionId ?? "" : ""} disabled={!includedActions.length} onChange={(event) => changeDelivery({ ...delivery, defaultActionId: event.target.value || null }, "切换默认动作")}><option value="">请选择可交付动作</option>{includedActions.map((action) => <option key={action.id} value={action.id}>{action.name}</option>)}</select></label><label>批量纹理比例<PercentDraftInputV4 value={delivery.globalTextureScale} maximum={8} strictlyPositive onCommit={(value) => { const current = deliverySnapshot.current; changeDelivery({ ...current, globalTextureScale: value, actionSettings: Object.fromEntries(character.actionIds.map((id) => [id, { ...settingFor(current, id), textureScale: settingFor(current, id).includeInExport ? value : settingFor(current, id).textureScale }])) }, "批量调整纹理比例"); }} /></label>{actions.map((action) => { const setting = settingFor(delivery, action.id); const activeFrames = enabledCount(action.id); return <article key={action.id} className={`export-action-card-v4 ${setting.includeInExport ? "included" : "excluded"} ${!activeFrames ? "no-enabled-frames" : ""}`}><div className="export-action-row primary-row"><strong>{action.name}</strong><span className={`export-frame-status ${activeFrames ? "ready" : "empty"}`}>{activeFrames} / {action.frameRefs.length} 帧启用</span><label className="check include-export"><input type="checkbox" checked={setting.includeInExport} onChange={(event) => updateAction(action.id, { includeInExport: event.target.checked })} />参与导出</label></div><div className="export-action-row secondary-row"><label>纹理比例<PercentDraftInputV4 value={setting.textureScale} disabled={!setting.includeInExport} maximum={8} strictlyPositive onCommit={(value) => updateAction(action.id, { textureScale: value })} /></label><label className="check"><input type="checkbox" disabled={!setting.includeInExport} checked={setting.runtimeLoop} onChange={(event) => updateAction(action.id, { runtimeLoop: event.target.checked })} />Runtime Loop</label></div>{!setting.includeInExport ? <small>保留在工作区，不参与本次交付。</small> : !activeFrames ? <small>没有启用帧，将自动跳过且不能设为默认动作。</small> : null}</article>; })}</fieldset>
        <fieldset><legend>Atlas</legend><label>上限<select value={delivery.atlas.maxSize} onChange={(event) => changeDelivery({ ...delivery, atlas: { ...delivery.atlas, maxSize: Number(event.target.value) as 2048 | 4096 | 8192 } }, "调整图集上限")}><option value={2048}>2048</option><option value={4096}>4096</option><option value={8192}>8192</option></select></label><div><label>间距<NumericDraftInput value={delivery.atlas.padding} minimum={0} maximum={128} onCommit={(value) => changeDelivery({ ...delivery, atlas: { ...delivery.atlas, padding: value } }, "调整图集间距")} /></label><label>扩边<NumericDraftInput value={delivery.atlas.extrude} minimum={0} maximum={32} onCommit={(value) => changeDelivery({ ...delivery, atlas: { ...delivery.atlas, extrude: value } }, "调整图集扩边")} /></label></div><label>紧裁留白<NumericDraftInput value={delivery.atlas.framePadding} minimum={0} maximum={256} onCommit={(value) => changeDelivery({ ...delivery, atlas: { ...delivery.atlas, framePadding: value } }, "调整图集留白")} /></label></fieldset></aside>
      <main><section className="export-metrics-v4">{estimate ? <><article><small>引用 / 去重</small><strong>{estimate.referencedFrames} / {estimate.uniqueSprites}</strong></article><article><small>最大输出</small><strong>{estimate.maximumOutput.width}×{estimate.maximumOutput.height}</strong></article><article><small>页面</small><strong>{estimate.pageCount}</strong></article><article><small>RGBA 内存</small><strong>{bytes(estimate.rgbaBytes)}</strong></article><article><small>PNG 估算</small><strong>{bytes(estimate.estimatedPngBytes)}</strong></article><article><small>装箱率</small><strong>{(estimate.packingRatio * 100).toFixed(1)}%</strong></article></> : <div className="export-empty-state"><Archive size={30} /><strong>{includedActions.length ? "等待图集预估" : "没有可交付动作"}</strong><p>{includedActions.length ? "点击“刷新预估”检查启用帧的去重、分辨率和装箱结果。" : "请先勾选动作，并在动作时间轴中至少启用一帧。"}</p></div>}</section>
        <section className="export-atlas-preview-v4"><header><div><small>ATLAS OUTPUT</small><strong>图集页面预览</strong></div>{character.exportState.status === "current" ? <a className="export-download-primary" href={api.domainCharacterExportDownloadUrl(character.id)} download><Download size={14} />下载角色包</a> : <span className="export-state-badge">尚未构建</span>}</header><input ref={repairInput} hidden type="file" accept="image/png" onChange={(event) => { const file = event.target.files?.[0]; if (file) void repair(file); event.currentTarget.value = ""; }} />{character.exportState.status === "current" && (estimate?.pages ?? [{ index: 0, width: 0, height: 0 }]).map((page) => <figure key={page.index}><div className="export-atlas-image"><img src={api.domainCharacterAtlasPageUrl(character.id, page.index)} alt={`图集页面 ${page.index + 1}`} /></div><figcaption><strong>第 {page.index + 1} 页</strong><span>{page.width ? `${page.width}×${page.height}` : "当前图集"}</span></figcaption><div className="export-page-actions"><a href={api.domainCharacterAtlasPageUrl(character.id, page.index)} download={`atlas-${page.index}.png`}><Download size={13} />下载原图</a><button type="button" className="secondary" disabled={busy} onClick={() => { repairPage.current = page.index; repairInput.current?.click(); }}>回导修复图</button></div></figure>)}</section>{notice ? <button type="button" className="v4-notice" role="status" onClick={() => setNotice(null)}>{notice}</button> : null}</main>
    </div>
  </section>;
}
