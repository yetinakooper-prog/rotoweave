import {
  Archive,
  Boxes,
  ChevronDown,
  ChevronRight,
  Film,
  FolderOpen,
  Loader2,
  LogOut,
  MoreHorizontal,
  Plus,
  Save,
  Settings,
  Trash2,
} from "lucide-react";
import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
/* eslint-disable react-hooks/set-state-in-effect -- hash navigation is reconciled with asynchronously loaded workspace identities. */
import { useQuery } from "@tanstack/react-query";

import { ActionEditorV4 } from "./features/action-editor-v4";
import { ClientSettingsV4 } from "./features/client-settings-v4";
import { ExportWorkbenchV4 } from "./features/export-workbench-v4";
import { GlobalSettingsV4 } from "./features/global-settings-v4";
import { MaterialManagerV4 } from "./features/material-manager-v4";
import { api } from "./lib/api";
import { usePageSaveExecutorV4 } from "./lib/page-save-command-v4";
import type { DomainCharacterV4, WorkspaceDomainV4, WorkspaceState } from "./lib/types";
import { useAutoDismissNoticeV4 } from "./lib/use-auto-dismiss-notice-v4";
import { useEscapeClose } from "./lib/use-escape-close";

type RouteV4 =
  | { page: "empty" }
  | { page: "app-settings" }
  | { page: "materials"; characterId: string; sourceId?: string; frameIndex?: number }
  | { page: "settings" | "export"; characterId: string }
  | { page: "action"; characterId: string; actionId: string };

type NavigableRouteV4 = Exclude<RouteV4, { page: "empty" }>;

type NameDialogState = {
  title: string;
  initialValue: string;
  submitLabel: string;
  onSubmit: (name: string) => Promise<void>;
};

function decode(value: string | undefined): string {
  if (!value) return "";
  try { return decodeURIComponent(value); } catch { return ""; }
}

export function parseClientRouteV4(hash = window.location.hash): RouteV4 {
  const parts = hash.replace(/^#\/?/, "").split("/").filter(Boolean);
  if (parts[0] === "settings" && parts.length === 1) return { page: "app-settings" };
  if (parts[0] !== "characters" || !parts[1]) return { page: "empty" };
  const characterId = decode(parts[1]);
  if (parts[2] === "materials") {
    const frameIndex = parts[4] === undefined ? undefined : Number(parts[4]);
    return {
      page: "materials",
      characterId,
      ...(parts[3] ? { sourceId: decode(parts[3]) } : {}),
      ...(Number.isInteger(frameIndex) && Number(frameIndex) >= 0 ? { frameIndex: Number(frameIndex) } : {}),
    };
  }
  if (parts[2] === "settings") return { page: "settings", characterId };
  if (parts[2] === "export") return { page: "export", characterId };
  if (parts[2] === "actions" && parts[3]) {
    return { page: "action", characterId, actionId: decode(parts[3]) };
  }
  return { page: "empty" };
}

function routeHash(route: NavigableRouteV4): string {
  if (route.page === "app-settings") return "#/settings";
  const character = encodeURIComponent(route.characterId);
  if (route.page === "action") return `#/characters/${character}/actions/${encodeURIComponent(route.actionId)}`;
  if (route.page === "materials" && route.sourceId) return `#/characters/${character}/materials/${encodeURIComponent(route.sourceId)}/${route.frameIndex ?? 0}`;
  return `#/characters/${character}/${route.page}`;
}

function defaultRoute(domain: WorkspaceDomainV4, characterId?: string): RouteV4 {
  const character = domain.characters.find((item) => item.id === characterId) ?? domain.characters[0];
  return character ? { page: "materials", characterId: character.id } : { page: "empty" };
}

function normalizedWorkspaceRoot(root: string | null | undefined): string {
  return (root ?? "").replace(/[\\/]+$/, "").toLocaleLowerCase();
}

function isCurrentWorkspace(workspace: WorkspaceState, item: NonNullable<WorkspaceState["recent"]>[number]): boolean {
  if (workspace.workspaceId && item.workspaceId) return workspace.workspaceId === item.workspaceId;
  return normalizedWorkspaceRoot(workspace.root) === normalizedWorkspaceRoot(item.root);
}

function WorkspaceGateV4({ workspace, busy, notice, onCreate, onOpen }: {
  workspace: WorkspaceState;
  busy: boolean;
  notice: string | null;
  onCreate: () => void;
  onOpen: (root?: string) => void;
}) {
  return <div className="client-shell-v4-workspace-gate">
    <section>
      <div className="client-v4-logo">RW</div>
      <small>RotoWeave CLIENT 4.0.0</small>
      <h1>选择 Workspace Format 3 工作区</h1>
      <p>工作区、素材、动作和导出数据只保存在本机。你可以建立新工作区，或继续最近使用的工作区。</p>
      <div className="client-v4-workspace-actions">
        <button type="button" className="primary" disabled={busy} onClick={onCreate}><Plus size={15} />新建工作区</button>
        <button type="button" disabled={busy} onClick={() => onOpen()}><FolderOpen size={15} />打开工作区</button>
      </div>
      {notice ? <p className="client-v4-workspace-error">{notice}</p> : null}
      {workspace.recent?.length ? <div className="client-v4-recent-workspaces"><strong>最近工作区</strong>{workspace.recent.map((item) => <button type="button" key={item.root} disabled={busy || !item.available} onClick={() => onOpen(item.root)}><span>{item.name}</span><small>{item.available ? item.root : "路径不可用"}</small></button>)}</div> : null}
    </section>
  </div>;
}

function WorkspaceSwitcherV4({ workspace, busy, onOpen, onExit }: {
  workspace: WorkspaceState;
  busy: boolean;
  onOpen: (root?: string) => void;
  onExit: () => void;
}) {
  const [open, setOpen] = useState(false);
  const container = useRef<HTMLDivElement>(null);
  const recent = workspace.recent ?? [];
  useEscapeClose(() => setOpen(false), open && !busy);

  useEffect(() => {
    if (!open) return;
    const closeOutside = (event: PointerEvent) => {
      if (event.target instanceof Node && !container.current?.contains(event.target)) setOpen(false);
    };
    window.addEventListener("pointerdown", closeOutside);
    return () => window.removeEventListener("pointerdown", closeOutside);
  }, [open]);

  function choose(root?: string) {
    setOpen(false);
    onOpen(root);
  }

  function exit() {
    setOpen(false);
    onExit();
  }

  return <div ref={container} className="client-v4-workspace-switcher">
    <div className="client-v4-workspace-current" title={workspace.root ?? workspace.name ?? "当前工作区"}>
      <span><small>工作区</small><strong>{workspace.name ?? "未命名工作区"}</strong></span>
      <button type="button" aria-label="工作区菜单" aria-haspopup="menu" aria-expanded={open} disabled={busy} onClick={() => setOpen((value) => !value)}><MoreHorizontal size={16} /></button>
    </div>
    {open ? <div className="client-v4-workspace-menu" role="menu" aria-label="切换工作区">
      <button type="button" role="menuitem" className="client-v4-workspace-open" disabled={busy} onClick={() => choose()}><FolderOpen size={14} /><span>打开工作区</span></button>
      <div className="client-v4-workspace-menu-title">最近工作区</div>
      <div className="client-v4-workspace-menu-recent">
        {recent.length ? recent.map((item) => {
          const current = isCurrentWorkspace(workspace, item);
          return <button type="button" role="menuitem" key={item.root} disabled={busy || current || !item.available} title={item.root} onClick={() => choose(item.root)}><span><strong>{item.name}</strong><small>{current ? "当前工作区" : item.available ? item.root : "路径不可用"}</small></span></button>;
        }) : <p>还没有最近工作区</p>}
      </div>
      <button type="button" role="menuitem" className="client-v4-workspace-exit" disabled={busy} onClick={exit}><LogOut size={14} /><span>退出工作区</span></button>
    </div> : null}
  </div>;
}

function NameDialogV4({ dialog, busy, onClose }: { dialog: NameDialogState; busy: boolean; onClose: () => void }) {
  const [value, setValue] = useState(dialog.initialValue);
  useEscapeClose(onClose, !busy);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const name = value.trim();
    if (!name || busy) return;
    await dialog.onSubmit(name);
  }
  return <div className="client-v4-dialog-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget && !busy) onClose(); }}>
    <form className="client-v4-dialog" role="dialog" aria-modal="true" aria-label={dialog.title} onSubmit={(event) => void submit(event)}>
      <small>RotoWeave 4.0</small><h2>{dialog.title}</h2>
      <label>名称<input autoFocus maxLength={120} value={value} onChange={(event) => setValue(event.target.value)} /></label>
      <footer><button type="button" disabled={busy} onClick={onClose}>取消</button><button type="submit" className="primary" disabled={busy || !value.trim()}>{busy ? <Loader2 className="spin" size={14} /> : null}{dialog.submitLabel}</button></footer>
    </form>
  </div>;
}

function UnsavedActionDialogV4({ busy, onSave, onDiscard, onCancel }: { busy: boolean; onSave: () => void; onDiscard: () => void; onCancel: () => void }) {
  useEscapeClose(onCancel, !busy);
  return <div className="client-v4-dialog-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget && !busy) onCancel(); }}>
    <section className="client-v4-dialog client-v4-unsaved-dialog" role="dialog" aria-modal="true" aria-label="动作有未保存修改">
      <small>UNSAVED ACTION</small><h2>动作有未保存修改</h2><p>保存当前动作后继续，或放弃本次草稿。保存失败时会留在当前页面。</p>
      <footer><button type="button" disabled={busy} onClick={onCancel}>取消</button><button type="button" className="danger" disabled={busy} onClick={onDiscard}>放弃修改</button><button type="button" className="primary" disabled={busy} onClick={onSave}>{busy ? <Loader2 className="spin" size={14} /> : <Save size={14} />}保存并继续</button></footer>
    </section>
  </div>;
}

export default function ClientShellV4() {
  const workspaceQuery = useQuery({ queryKey: ["workspace-v4"], queryFn: api.workspace });
  const workspaceOpen = workspaceQuery.data?.state === "Open";
  const domainQuery = useQuery({ queryKey: ["domain-v4"], queryFn: api.domainV4, enabled: workspaceOpen });
  const domain = domainQuery.data;
  const [route, setRoute] = useState<RouteV4>(() => parseClientRouteV4());
  const [dirtyAction, setDirtyAction] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [nameDialog, setNameDialog] = useState<NameDialogState | null>(null);
  const [pendingLeave, setPendingLeave] = useState<{ proceed: () => void } | null>(null);
  const [leavingBusy, setLeavingBusy] = useState(false);
  const pendingCharacterId = useRef<string | null>(null);
  const pendingActionId = useRef<string | null>(null);
  const executePageSave = usePageSaveExecutorV4();
  useAutoDismissNoticeV4(notice, () => setNotice(null), workspaceOpen);

  async function chooseWorkspaceRoot(): Promise<string | null> {
    const selected = await api.chooseWorkspaceFolder();
    return selected.root?.trim() || null;
  }

  async function createWorkspace() {
    const name = window.prompt("新建工作区名称", "RotoWeave 4.0 项目")?.trim();
    if (!name) return;
    setBusy(true); setNotice(null);
    try {
      const root = await chooseWorkspaceRoot();
      if (!root) return;
      await api.createWorkspace(root, name);
      await workspaceQuery.refetch();
      await domainQuery.refetch();
    } catch (error) { setNotice(error instanceof Error ? error.message : "新建工作区失败。"); }
    finally { setBusy(false); }
  }

  async function openWorkspace(root?: string) {
    setBusy(true); setNotice(null);
    try {
      const selected = root ?? await chooseWorkspaceRoot();
      if (!selected) return;
      if (normalizedWorkspaceRoot(selected) === normalizedWorkspaceRoot(workspaceQuery.data?.root)) {
        setNotice("该工作区已经打开。");
        return;
      }
      const inspection = await api.inspectWorkspaceBrand(selected);
      if (inspection.migratable) {
        const accepted = window.confirm(
          `检测到 AIFrameTools 4.0（RotoWeave 前身）/ Workspace Format 3 工作区“${inspection.name ?? "未命名"}”。\n\nRotoWeave 将先保留回滚备份，再原子生成 rotoweave.json；不会迁移 Format 2。是否继续？`,
        );
        if (!accepted) return;
        await api.migrateWorkspaceBrand(selected);
      }
      await api.openWorkspace(selected);
      await workspaceQuery.refetch();
      const nextDomain = await domainQuery.refetch();
      setDirtyAction(false);
      const fallback = nextDomain.data ? defaultRoute(nextDomain.data) : { page: "empty" as const };
      if (fallback.page === "empty") {
        window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}`);
        setRoute(fallback);
      } else {
        navigate(fallback, true, true);
      }
    } catch (error) { setNotice(error instanceof Error ? error.message : "打开工作区失败。"); }
    finally { setBusy(false); }
  }

  async function exitWorkspace() {
    setBusy(true); setNotice(null);
    try {
      await api.prepareAndCloseWorkspace();
      setDirtyAction(false);
      window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}`);
      setRoute({ page: "empty" });
      await workspaceQuery.refetch();
    } catch (error) { setNotice(error instanceof Error ? error.message : "退出工作区失败。"); }
    finally { setBusy(false); }
  }

  function requestWorkspaceOperation(proceed: () => void) {
    if (busy) return;
    if (dirtyAction && route.page === "action") {
      setPendingLeave({ proceed });
      return;
    }
    proceed();
  }

  useEffect(() => {
    const update = () => setRoute(parseClientRouteV4());
    window.addEventListener("hashchange", update);
    return () => window.removeEventListener("hashchange", update);
  }, []);

  const navigate = useCallback((next: NavigableRouteV4, replace = false, force = false) => {
    const proceed = () => {
      const nextHash = routeHash(next);
      if (replace) window.history.replaceState(null, "", nextHash);
      else window.location.hash = nextHash.slice(1);
      setRoute(next);
      setNotice(null);
    };
    if (!force && dirtyAction && route.page === "action" && routeHash(route) !== routeHash(next)) {
      setPendingLeave({ proceed });
      return false;
    }
    proceed();
    return true;
  }, [dirtyAction, route]);

  async function saveAndContinue() {
    if (!pendingLeave || leavingBusy) return;
    setLeavingBusy(true);
    try {
      if (!await executePageSave()) return;
      const { proceed } = pendingLeave;
      setPendingLeave(null);
      setDirtyAction(false);
      proceed();
    } finally { setLeavingBusy(false); }
  }

  function discardAndContinue() {
    if (!pendingLeave || leavingBusy) return;
    const { proceed } = pendingLeave;
    setPendingLeave(null);
    setDirtyAction(false);
    proceed();
  }

  useEffect(() => {
    if (!domainQuery.isFetched || !domain) return;
    if (!domain.characters.length) {
      if (route.page !== "empty" && route.page !== "app-settings") setRoute({ page: "empty" });
      return;
    }
    if (route.page === "app-settings") return;
    const character = route.page === "empty" ? null : domain.characters.find((item) => item.id === route.characterId);
    if (!character) {
      if (route.page !== "empty" && pendingCharacterId.current === route.characterId) return;
      const fallback = defaultRoute(domain);
      if (fallback.page !== "empty") {
        navigate(fallback, true);
        setNotice(route.page === "empty" ? null : "链接中的角色已不存在，已返回最近有效角色。");
      }
      return;
    }
    if (pendingCharacterId.current === character.id) pendingCharacterId.current = null;
    if (route.page === "action" && !domain.actions.some((item) => item.id === route.actionId && item.characterId === character.id)) {
      if (pendingActionId.current === route.actionId) return;
      const first = domain.actions.find((item) => item.characterId === character.id);
      navigate(first ? { page: "action", characterId: character.id, actionId: first.id } : { page: "materials", characterId: character.id }, true);
      setNotice("链接中的动作已不存在，已回到该角色的有效页面。");
    } else if (route.page === "action" && pendingActionId.current === route.actionId) {
      pendingActionId.current = null;
    }
  }, [domain, domainQuery.isFetched, navigate, route]);

  const character = route.page === "empty" || route.page === "app-settings" ? undefined : domain?.characters.find((item) => item.id === route.characterId);
  async function createCharacter(name: string) {
    if (!domain) return;
    setBusy(true);
    try {
      const result = await api.createDomainCharacter(name, domain.revisionId);
      pendingCharacterId.current = result.character.id;
      await domainQuery.refetch();
      navigate({ page: "materials", characterId: result.character.id });
      setNameDialog(null);
    } catch (error) { pendingCharacterId.current = null; setNotice(error instanceof Error ? error.message : "创建角色失败。"); }
    finally { setBusy(false); }
  }

  async function renameCharacter(target: DomainCharacterV4, name: string) {
    if (!domain) return;
    if (!name || name === target.name) return;
    setBusy(true);
    try { await api.updateDomainCharacter(target.id, name, domain.revisionId); await domainQuery.refetch(); setNameDialog(null); }
    catch (error) { setNotice(error instanceof Error ? error.message : "重命名角色失败。"); }
    finally { setBusy(false); }
  }

  async function revealCharacterDirectory(target: DomainCharacterV4) {
    setNotice(null);
    try {
      await api.revealDomainCharacter(target.id);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "打开角色目录失败。");
    }
  }

  async function deleteCharacter(target: DomainCharacterV4) {
    if (!domain || !window.confirm(`删除角色“${target.name}”及其全部动作、素材和处理版本？`)) return;
    setBusy(true);
    try {
      await api.deleteDomainCharacter(target.id, domain.revisionId);
      const result = await domainQuery.refetch();
      const fallback = result.data ? defaultRoute(result.data) : { page: "empty" as const };
      if (fallback.page === "empty") { window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}`); setRoute(fallback); }
      else { setDirtyAction(false); navigate(fallback, true, true); }
    } catch (error) { setNotice(error instanceof Error ? error.message : "删除角色失败。"); }
    finally { setBusy(false); }
  }

  function beginCreateAction(target: DomainCharacterV4) {
    if (!domain) return;
    const characterActions = domain.actions.filter((item) => item.characterId === target.id);
    const proceed = () => setNameDialog({ title: `为 ${target.name} 新建动作`, initialValue: `动作 ${characterActions.length + 1}`, submitLabel: "创建动作", onSubmit: (name) => createAction(target, name) });
    if (dirtyAction && route.page === "action") { setPendingLeave({ proceed }); return; }
    proceed();
  }

  async function createAction(target: DomainCharacterV4, name: string) {
    if (!domain) return;
    setBusy(true);
    try {
      const result = await api.createDomainAction(target.id, name, domain.revisionId);
      pendingActionId.current = result.action.id;
      await domainQuery.refetch();
      setDirtyAction(false);
      navigate({ page: "action", characterId: target.id, actionId: result.action.id }, false, true);
      setNameDialog(null);
    } catch (error) { pendingActionId.current = null; setNotice(error instanceof Error ? error.message : "创建动作失败。"); }
    finally { setBusy(false); }
  }

  async function renameAction(actionId: string, currentName: string, name: string) {
    if (!domain) return;
    if (!name || name === currentName) return;
    setBusy(true);
    try { await api.updateDomainAction(actionId, { name }, domain.revisionId); await domainQuery.refetch(); setNameDialog(null); }
    catch (error) { setNotice(error instanceof Error ? error.message : "重命名动作失败。"); }
    finally { setBusy(false); }
  }

  async function deleteAction(actionId: string, actionName: string, characterId: string, resolvedDirty = false) {
    if (!domain) return;
    const discarding = dirtyAction && route.page === "action" && route.actionId === actionId;
    if (discarding && !resolvedDirty) { setPendingLeave({ proceed: () => { void deleteAction(actionId, actionName, characterId, true); } }); return; }
    if (!window.confirm(`删除动作“${actionName}”？`)) return;
    setBusy(true);
    try {
      await api.deleteDomainAction(actionId, domain.revisionId);
      setDirtyAction(false);
      const result = await domainQuery.refetch();
      const nextAction = result.data?.actions.find((item) => item.characterId === characterId);
      navigate(nextAction ? { page: "action", characterId, actionId: nextAction.id } : { page: "materials", characterId }, true, true);
    } catch (error) { setNotice(error instanceof Error ? error.message : "删除动作失败。"); }
    finally { setBusy(false); }
  }

  if (workspaceQuery.isLoading) return <div className="client-shell-v4-loading"><Loader2 className="spin" />正在连接本地 RotoWeave 4.0 服务</div>;
  if (workspaceQuery.isError || !workspaceQuery.data) return <div className="client-shell-v4-loading error"><strong>本地 4.0 服务未就绪</strong><span>{workspaceQuery.error instanceof Error ? workspaceQuery.error.message : "无法读取本地服务状态。"}</span><button type="button" onClick={() => void workspaceQuery.refetch()}>重试</button></div>;
  if (!workspaceOpen) return <WorkspaceGateV4 workspace={workspaceQuery.data} busy={busy} notice={notice} onCreate={() => void createWorkspace()} onOpen={(root) => void openWorkspace(root)} />;
  if (domainQuery.isLoading) return <div className="client-shell-v4-loading"><Loader2 className="spin" />正在进入 RotoWeave 4.0</div>;
  if (domainQuery.isError) return <div className="client-shell-v4-loading error"><strong>本地 4.0 服务未就绪</strong><span>{domainQuery.error instanceof Error ? domainQuery.error.message : "无法读取 Workspace Format 3。"}</span><button type="button" onClick={() => void domainQuery.refetch()}>重试</button></div>;

  return <div className="client-shell-v4">
    <aside className="client-v4-sidebar">
      <header><div className="client-v4-logo">RW</div><div><strong>RotoWeave</strong><small>CLIENT 4.0.0</small></div></header>
      <WorkspaceSwitcherV4 workspace={workspaceQuery.data} busy={busy} onOpen={(root) => requestWorkspaceOperation(() => { void openWorkspace(root); })} onExit={() => requestWorkspaceOperation(() => { void exitWorkspace(); })} />
      <div className="client-v4-sidebar-title"><span>角色</span><button type="button" aria-label="新增角色" disabled={busy} onClick={() => setNameDialog({ title: "新建角色", initialValue: `角色 ${(domain?.characters.length ?? 0) + 1}`, submitLabel: "创建角色", onSubmit: createCharacter })}><Plus size={14} /></button></div>
      <nav className="client-v4-character-list">
        {(domain?.characters ?? []).map((item) => {
          const active = character?.id === item.id;
          const itemActions = (domain?.actions ?? []).filter((action) => action.characterId === item.id);
          return <section key={item.id} className={active ? "active" : ""}>
            <div className="client-v4-character-row"><button type="button" onClick={() => navigate({ page: "materials", characterId: item.id })}>{active ? <ChevronDown size={14} /> : <ChevronRight size={14} />}<Film size={14} /><strong>{item.name}</strong></button><button type="button" title="打开角色规范目录" aria-label={`打开 ${item.name} 的角色目录`} onClick={() => void revealCharacterDirectory(item)}><FolderOpen size={13} /></button><button type="button" title="重命名角色" onClick={() => setNameDialog({ title: "重命名角色", initialValue: item.name, submitLabel: "保存名称", onSubmit: (name) => renameCharacter(item, name) })}><MoreHorizontal size={14} /></button><button type="button" title="删除角色" onClick={() => void deleteCharacter(item)}><Trash2 size={13} /></button></div>
            {active ? <div className="client-v4-character-pages">
              <button type="button" className={route.page === "settings" ? "active" : ""} onClick={() => navigate({ page: "settings", characterId: item.id })}><Settings size={13} />全局设置</button>
              <button type="button" className={route.page === "materials" ? "active" : ""} onClick={() => navigate({ page: "materials", characterId: item.id })}><Boxes size={13} />素材库<small>{item.materialSourceIds.length}</small></button>
              <div className="client-v4-action-title"><span>动作</span><button type="button" aria-label={`为 ${item.name} 新增动作`} onClick={() => beginCreateAction(item)}><Plus size={12} /></button></div>
              {itemActions.map((action, index) => <div key={action.id} className={`client-v4-action-row ${route.page === "action" && route.actionId === action.id ? "active" : ""}`}><button type="button" onClick={() => navigate({ page: "action", characterId: item.id, actionId: action.id })} onDoubleClick={() => setNameDialog({ title: "重命名动作", initialValue: action.name, submitLabel: "保存名称", onSubmit: (name) => renameAction(action.id, action.name, name) })}><span className="client-v4-index">{String(index + 1).padStart(2, "0")}</span><span>{action.name}</span><small>{action.frameRefs.length}</small></button><button type="button" title="重命名动作" onClick={() => setNameDialog({ title: "重命名动作", initialValue: action.name, submitLabel: "保存名称", onSubmit: (name) => renameAction(action.id, action.name, name) })}><MoreHorizontal size={12} /></button><button type="button" title="删除动作" onClick={() => void deleteAction(action.id, action.name, item.id)}><Trash2 size={11} /></button></div>)}
              {!itemActions.length ? <p>点击 + 新建动作</p> : null}
              <button type="button" className={route.page === "export" ? "active" : ""} onClick={() => navigate({ page: "export", characterId: item.id })}><Archive size={13} />导出设置</button>
            </div> : null}
          </section>;
        })}
      </nav>
      <button type="button" className={`client-v4-app-settings ${route.page === "app-settings" ? "active" : ""}`} onClick={() => navigate({ page: "app-settings" })}><Settings size={14} /><span><strong>设置</strong><small>跨角色尺寸预设</small></span></button>
      <footer className="client-v4-sidebar-footer">
        <small><span className="client-v4-health" />本地 API /api/v4</small>
      </footer>
    </aside>
    <main className="client-v4-content">
      {notice ? <button type="button" className="client-v4-route-notice" onClick={() => setNotice(null)}>{notice}</button> : null}
      {route.page === "app-settings" ? <ClientSettingsV4 /> : !domain?.characters.length || !character ? <section className="client-v4-empty"><div><Plus size={28} /></div><small>RotoWeave 4.0</small><h1>建立第一个角色</h1><p>素材、动作、图集与导出均保存在本机 Workspace Format 3。远程服务器不会拥有你的项目数据。</p><button type="button" disabled={busy} onClick={() => setNameDialog({ title: "新建角色", initialValue: "角色 1", submitLabel: "创建角色", onSubmit: createCharacter })}><Plus size={15} />新建角色</button></section> : route.page === "materials" ? <MaterialManagerV4 mode="page" characterId={character.id} focusSourceId={route.sourceId} focusFrameIndex={route.frameIndex} onCharacterChange={(id) => navigate({ page: "materials", characterId: id })} /> : route.page === "action" ? <ActionEditorV4 mode="page" characterId={character.id} actionId={route.actionId} onActionChange={(id) => { if (id) navigate({ page: "action", characterId: character.id, actionId: id }); }} onDirtyChange={setDirtyAction} /> : route.page === "settings" ? <GlobalSettingsV4 character={character} domain={domain} onRefresh={domainQuery.refetch} /> : route.page === "export" ? <ExportWorkbenchV4 character={character} domain={domain} onRefresh={domainQuery.refetch} /> : null}
    </main>
    {nameDialog ? <NameDialogV4 key={`${nameDialog.title}:${nameDialog.initialValue}`} dialog={nameDialog} busy={busy} onClose={() => setNameDialog(null)} /> : null}
    {pendingLeave ? <UnsavedActionDialogV4 busy={leavingBusy} onSave={() => void saveAndContinue()} onDiscard={discardAndContinue} onCancel={() => { if (!leavingBusy) setPendingLeave(null); }} /> : null}
  </div>;
}
