import { Archive, Loader2, PlugZap, Plus, Save, Server, Settings2, Trash2 } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
/* eslint-disable react-hooks/set-state-in-effect -- the editors follow persisted settings after load/save. */
import { useQuery } from "@tanstack/react-query";

import { NumericDraftInput } from "../components/numeric-draft-input";
import { DeploymentBundlePanel } from "./deployment-bundle-panel";
import { api } from "../lib/api";
import { usePageSaveCommandV4 } from "../lib/page-save-command-v4";
import { CANONICAL_PIXELS_PER_UNIT } from "../lib/protocol-contract";
import type { RemoteServiceConnectionTest, RemoteServiceSettings, SizeProfile } from "../lib/types";
import { useAutoDismissNoticeV4 } from "../lib/use-auto-dismiss-notice-v4";

type SizeDraft = {
  name: string;
  unitMode: "pixels" | "unity";
  width: number;
  height: number;
};

type RemoteDraft = {
  enabled: boolean;
  host: string;
  port: number;
};

function draftFromPreset(profile: SizeProfile): SizeDraft {
  const unitMode = profile.unit_mode ?? "unity";
  return {
    name: profile.name,
    unitMode,
    width: unitMode === "pixels"
      ? Math.round(profile.width_world * CANONICAL_PIXELS_PER_UNIT)
      : profile.width_world,
    height: unitMode === "pixels"
      ? Math.round(profile.height_world * CANONICAL_PIXELS_PER_UNIT)
      : profile.height_world,
  };
}

function emptyDraft(index: number): SizeDraft {
  return { name: `预设 ${index}`, unitMode: "pixels", width: 512, height: 512 };
}

function remoteDraftFromSettings(settings?: RemoteServiceSettings): RemoteDraft {
  return {
    enabled: settings?.enabled ?? false,
    host: settings?.host ?? "127.0.0.1",
    port: settings?.port ?? 8443,
  };
}

export function ClientSettingsV4() {
  const sizeQuery = useQuery({ queryKey: ["size-system-v4"], queryFn: api.sizeSystem });
  const remoteQuery = useQuery({ queryKey: ["remote-service-settings-v4"], queryFn: api.remoteServiceSettings });
  const profiles = useMemo(() => sizeQuery.data?.profiles ?? [], [sizeQuery.data?.profiles]);
  const [view, setView] = useState<"remote" | "size" | "deployment">("remote");
  const [selectedId, setSelectedId] = useState<string>("");
  const [draft, setDraft] = useState<SizeDraft>(() => emptyDraft(1));
  const [creating, setCreating] = useState(false);
  const [remoteDraft, setRemoteDraft] = useState<RemoteDraft>(() => remoteDraftFromSettings());
  const [connectionResult, setConnectionResult] = useState<RemoteServiceConnectionTest | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const selected = useMemo(() => profiles.find((item) => item.id === selectedId), [profiles, selectedId]);
  useAutoDismissNoticeV4(notice, () => setNotice(null));

  useEffect(() => {
    if (creating) return;
    const next = selected ?? profiles[0];
    if (!next) return;
    if (next.id !== selectedId) setSelectedId(next.id);
    setDraft(draftFromPreset(next));
  }, [creating, profiles, selected, selectedId]);

  useEffect(() => {
    if (!remoteQuery.data) return;
    setRemoteDraft(remoteDraftFromSettings(remoteQuery.data));
  }, [remoteQuery.data]);

  function showRemote() {
    setView("remote");
    setNotice(null);
  }

  function choose(profile: SizeProfile) {
    setView("size");
    setCreating(false);
    setSelectedId(profile.id);
    setDraft(draftFromPreset(profile));
    setNotice(null);
  }

  function beginCreate() {
    setView("size");
    setCreating(true);
    setSelectedId("");
    setDraft(emptyDraft(profiles.length + 1));
    setNotice("正在建立新的跨角色预设。保存后所有角色均可选择，但角色修正不会反写这里。");
  }

  function changeUnit(unitMode: "pixels" | "unity") {
    if (unitMode === draft.unitMode) return;
    const factor = unitMode === "pixels" ? CANONICAL_PIXELS_PER_UNIT : 1 / CANONICAL_PIXELS_PER_UNIT;
    setDraft((current) => ({
      ...current,
      unitMode,
      width: Number((current.width * factor).toFixed(unitMode === "pixels" ? 0 : 4)),
      height: Number((current.height * factor).toFixed(unitMode === "pixels" ? 0 : 4)),
    }));
  }

  async function persistPreset(): Promise<SizeProfile> {
    if (!draft.name.trim()) throw new Error("预设名称不能为空。");
    const widthWorld = draft.unitMode === "pixels" ? draft.width / CANONICAL_PIXELS_PER_UNIT : draft.width;
    const heightWorld = draft.unitMode === "pixels" ? draft.height / CANONICAL_PIXELS_PER_UNIT : draft.height;
    const payload = {
      name: draft.name.trim(),
      width_world: widthWorld,
      height_world: heightWorld,
      unit_mode: draft.unitMode,
    } as const;
    const saved = selected && !creating
      ? await api.updateSizeProfile(selected.id, payload)
      : await api.createSizeProfile(payload);
    setCreating(false);
    setSelectedId(saved.id);
    await sizeQuery.refetch();
    return saved;
  }

  async function savePreset(): Promise<boolean> {
    if (busy) return false;
    setBusy(true);
    setNotice(null);
    try {
      const saved = await persistPreset();
      setNotice(`预设“${saved.name}”已保存。已有角色快照不会被自动改写。`);
      return true;
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "尺寸预设保存失败。");
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function persistRemoteSettings(): Promise<RemoteServiceSettings> {
    const saved = await api.saveRemoteServiceSettings({
      ...remoteDraft,
      host: remoteDraft.host.trim(),
      port: Math.round(remoteDraft.port),
    });
    setConnectionResult(null);
    setRemoteDraft(remoteDraftFromSettings(saved));
    await remoteQuery.refetch();
    return saved;
  }

  async function saveRemote(): Promise<boolean> {
    if (busy) return false;
    setBusy(true);
    setNotice(null);
    try {
      const saved = await persistRemoteSettings();
      setNotice(saved.enabled
        ? `远程算力连接已保存：${saved.endpoint}。新建 High/Ultra 任务将使用该连接。`
        : "远程算力服务已关闭；本地 Basic 与 Photoshop 流程不受影响。");
      return true;
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "远程算力连接保存失败。");
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function testConnection() {
    if (busy) return;
    if (!remoteDraft.enabled) {
      setNotice("请先启用远程算力服务，再保存并测试连接。");
      return;
    }
    setBusy(true);
    setNotice(null);
    let saved = false;
    try {
      await persistRemoteSettings();
      saved = true;
      const result = await api.testRemoteService();
      setConnectionResult(result);
      setNotice(result.ready
        ? `连接、私网地址和协议验证通过；${result.service} 已就绪。`
        : `连接、私网地址和协议验证通过；远程 Worker 当前为 ${result.workerState}。`);
    } catch (error) {
      setConnectionResult(null);
      const message = error instanceof Error ? error.message : "远程服务连接测试失败。";
      setNotice(saved ? `连接设置已保存，但测试失败：${message}` : message);
    } finally {
      setBusy(false);
    }
  }

  async function saveActive(): Promise<boolean> {
    if (view === "deployment") return true;
    return view === "remote" ? saveRemote() : savePreset();
  }
  usePageSaveCommandV4(saveActive);

  async function remove() {
    if (!selected || creating || !window.confirm(`删除预设“${selected.name}”？已经选择它的角色会继续保留各自快照。`)) return;
    setBusy(true);
    setNotice(null);
    try {
      await api.deleteSizeProfile(selected.id);
      setSelectedId("");
      await sizeQuery.refetch();
      setNotice("预设已删除；角色本地尺寸不会改变。");
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "尺寸预设删除失败。");
    } finally {
      setBusy(false);
    }
  }

  if (sizeQuery.isLoading || remoteQuery.isLoading) return <div className="client-shell-v4-loading"><Loader2 className="spin" />正在读取客户端设置</div>;
  if (sizeQuery.isError || remoteQuery.isError) {
    const error = remoteQuery.error ?? sizeQuery.error;
    return <div className="client-shell-v4-loading error"><strong>无法读取客户端设置</strong><span>{error instanceof Error ? error.message : "客户端设置不可用。"}</span><button type="button" onClick={() => { void sizeQuery.refetch(); void remoteQuery.refetch(); }}>重试</button></div>;
  }

  const pixels = draft.unitMode === "pixels" ? { width: draft.width, height: draft.height } : { width: draft.width * CANONICAL_PIXELS_PER_UNIT, height: draft.height * CANONICAL_PIXELS_PER_UNIT };
  const world = draft.unitMode === "unity" ? { width: draft.width, height: draft.height } : { width: draft.width / CANONICAL_PIXELS_PER_UNIT, height: draft.height / CANONICAL_PIXELS_PER_UNIT };
  const persistedRemote = remoteQuery.data;
  const statusLabel = connectionResult
    ? (connectionResult.ready ? "已连接 · Worker 就绪" : `已连接 · ${connectionResult.workerState}`)
    : (persistedRemote?.enabled ? "已保存 · 尚未测试" : "未启用");

  return <section className="client-settings-v4" aria-label="客户端设置">
    <header><div><small>CLIENT SETTINGS</small><h1>设置</h1><p>管理本机远程算力连接、尺寸预设和快速部署包。</p></div>{view !== "deployment" ? <button type="button" className="primary" disabled={busy} onClick={() => void saveActive()}>{busy ? <Loader2 className="spin" size={14} /> : <Save size={14} />}{view === "remote" ? "保存连接" : "保存预设"}</button> : null}</header>
    <div className="client-settings-v4-layout">
      <aside>
        <div className="client-settings-v4-list-title"><strong>本机连接</strong></div>
        <button type="button" className={view === "remote" ? "active" : ""} onClick={showRemote}><Server size={14} /><span><strong>远程算力服务</strong><small>{persistedRemote?.endpoint ?? "HTTP / High / Ultra"}</small></span></button>
        <button type="button" className={view === "deployment" ? "active" : ""} onClick={() => { setView("deployment"); setNotice(null); }}><Archive size={14} /><span><strong>部署包导出</strong><small>环境缓存 / 单 ZIP</small></span></button>
        <div className="client-settings-v4-list-title client-settings-v4-preset-title"><strong>尺寸矩形框预设</strong><button type="button" onClick={beginCreate}><Plus size={13} />新增</button></div>
        {profiles.map((profile) => <button type="button" key={profile.id} className={view === "size" && !creating && selectedId === profile.id ? "active" : ""} onClick={() => choose(profile)}><Settings2 size={14} /><span><strong>{profile.name}</strong><small>{Math.round(profile.width_world * CANONICAL_PIXELS_PER_UNIT)}×{Math.round(profile.height_world * CANONICAL_PIXELS_PER_UNIT)} px</small></span></button>)}
        {!profiles.length ? <p>尚无预设。点击“新增”建立第一个跨角色档位。</p> : null}
      </aside>
      <main>
        {view === "deployment" ? <DeploymentBundlePanel /> : view === "remote" ? <article className="client-settings-remote-service">
          <div><small>REMOTE COMPUTE</small><h2>远程算力服务</h2><p>每台客户端保留自己的工作区，只把新建的 High/Ultra 帧包发送到可信局域网服务器。</p></div>
          <div className={`remote-service-status ${connectionResult ? "connected" : ""} ${connectionResult?.ready ? "ready" : ""}`}><span /><strong>{statusLabel}</strong><small>{persistedRemote?.endpoint ?? "尚未保存服务地址"}</small></div>
          <label className="remote-service-toggle"><input type="checkbox" checked={remoteDraft.enabled} onChange={(event) => { setRemoteDraft((current) => ({ ...current, enabled: event.target.checked })); setConnectionResult(null); }} /><span><strong>启用远程 High/Ultra</strong><small>关闭后仍可使用本地 Basic 与 Photoshop 往返。</small></span></label>
          <div className="remote-service-address-fields">
            <label>服务端固定局域网 IPv4<input value={remoteDraft.host} placeholder="192.168.1.40" spellCheck={false} onChange={(event) => { setRemoteDraft((current) => ({ ...current, host: event.target.value })); setConnectionResult(null); }} /></label>
            <label>HTTP API 端口<NumericDraftInput value={remoteDraft.port} strictlyPositive maximum={65535} onCommit={(port) => { setRemoteDraft((current) => ({ ...current, port: Math.round(port) })); setConnectionResult(null); }} /></label>
          </div>
          <p className="remote-service-boundary">仅用于可信局域网：连接不加密，也不验证客户端身份，请勿映射到公网。管理端口 8444 仍不对客户端开放。</p>
          <footer><button type="button" disabled={busy || !remoteDraft.enabled} onClick={() => void testConnection()}>{busy ? <Loader2 className="spin" size={13} /> : <PlugZap size={13} />}保存并测试连接</button></footer>
        </article> : <article>
          <div><small>{creating ? "NEW PRESET" : "SHARED PRESET"}</small><h2>{creating ? "新建尺寸预设" : selected?.name ?? "尺寸预设"}</h2><p>修改这里只影响共享预设；角色已经保存的尺寸修正不会被覆盖。</p></div>
          <label>预设名称<input maxLength={80} value={draft.name} onChange={(event) => setDraft((current) => ({ ...current, name: event.target.value }))} /></label>
          <div className="size-unit-switch" role="group" aria-label="预设尺寸输入单位"><button type="button" className={draft.unitMode === "pixels" ? "active" : ""} onClick={() => changeUnit("pixels")}>像素 px</button><button type="button" className={draft.unitMode === "unity" ? "active" : ""} onClick={() => changeUnit("unity")}>Unity unit</button></div>
          <div className="client-settings-size-fields"><label>宽（{draft.unitMode === "pixels" ? "px" : "unit"}）<NumericDraftInput value={draft.width} strictlyPositive maximum={draft.unitMode === "pixels" ? 16384 : 163.84} onCommit={(width) => setDraft((current) => ({ ...current, width: draft.unitMode === "pixels" ? Math.round(width) : Math.round(width * 10000) / 10000 }))} /></label><label>高（{draft.unitMode === "pixels" ? "px" : "unit"}）<NumericDraftInput value={draft.height} strictlyPositive maximum={draft.unitMode === "pixels" ? 16384 : 163.84} onCommit={(height) => setDraft((current) => ({ ...current, height: draft.unitMode === "pixels" ? Math.round(height) : Math.round(height * 10000) / 10000 }))} /></label></div>
          <p className="unit-equivalence"><strong>{Math.round(pixels.width)}×{Math.round(pixels.height)} px</strong><span>= {world.width.toFixed(3)}×{world.height.toFixed(3)} Unity unit · 固定 100 PPU</span></p>
          <footer><button type="button" className="danger" disabled={!selected || creating || busy} onClick={() => void remove()}><Trash2 size={13} />删除预设</button></footer>
        </article>}
        {notice ? <button type="button" className="client-settings-v4-notice" role="status" onClick={() => setNotice(null)}>{notice}</button> : null}
      </main>
    </div>
  </section>;
}
