import { useQuery } from "@tanstack/react-query";
import { Archive, CheckCircle2, Copy, FolderOpen, RefreshCcw, Square, XCircle } from "lucide-react";
import { useState } from "react";

import { api } from "../api";
import type { DeploymentBundleExport, DeploymentBundlePlan } from "../types";
import { ErrorRecovery } from "../components";

function formatBytes(value?: number | null): string {
  if (!value) return "0 B";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let amount = value;
  let index = 0;
  while (amount >= 1024 && index < units.length - 1) { amount /= 1024; index += 1; }
  return `${amount.toFixed(index > 1 ? 2 : 0)} ${units[index]}`;
}

export function DeploymentPage() {
  const plan = useQuery({ queryKey: ["deployment-bundle-plan"], queryFn: () => api<DeploymentBundlePlan>("/deployment-bundles/plan") });
  const [exportId, setExportId] = useState<string>();
  const [message, setMessage] = useState<string>();
  const job = useQuery({
    queryKey: ["deployment-bundle-export", exportId],
    queryFn: () => api<DeploymentBundleExport>(`/deployment-bundles/exports/${exportId}`),
    enabled: !!exportId,
    refetchInterval: query => ["queued", "running"].includes(query.state.data?.state ?? "") ? 750 : false,
  });
  const active = ["queued", "running"].includes(job.data?.state ?? "");

  async function start() {
    setMessage(undefined);
    try {
      const selected = await api<{ selectionToken: string | null; displayPath: string | null }>("/deployment-bundles/output-directory-dialog", { method: "POST" });
      if (!selected.selectionToken) { setMessage("已取消选择输出目录。"); return; }
      const created = await api<DeploymentBundleExport>("/deployment-bundles/exports", { method: "POST", body: JSON.stringify({ selectionToken: selected.selectionToken }) });
      setExportId(created.id);
      setMessage(`正在导出到 ${selected.displayPath}`);
    } catch (error) { setMessage(error instanceof Error ? error.message : "无法启动导出。"); }
  }

  async function cancel() {
    if (!exportId) return;
    try { await api(`/deployment-bundles/exports/${exportId}`, { method: "DELETE" }); await job.refetch(); }
    catch (error) { setMessage(error instanceof Error ? error.message : "无法取消导出。"); }
  }

  async function reveal() {
    if (!exportId) return;
    try { await api(`/deployment-bundles/exports/${exportId}/reveal`, { method: "POST" }); }
    catch (error) { setMessage(error instanceof Error ? error.message : "无法打开输出目录。"); }
  }

  async function copySha() {
    if (!job.data?.sha256) return;
    await navigator.clipboard.writeText(job.data.sha256);
    setMessage("ZIP SHA-256 已复制。");
  }

  return <div className="page-stack deployment-page">
    <div className="page-title"><div><p className="eyebrow">OFFLINE BOOTSTRAP · ZIP64 · LOCALHOST ONLY</p><h1>服务端部署包</h1><p>默认导出 High/Ultra 固定运行时、五个精确模型与离线依赖；不恢复旧整合模型包。</p></div><div className="actions"><button className="secondary" onClick={() => void plan.refetch()}><RefreshCcw />刷新预检</button>{active ? <button className="danger" onClick={() => void cancel()}><Square />取消导出</button> : <button disabled={!plan.data?.ready} onClick={() => void start()}><Archive />选择目录并导出 ZIP</button>}</div></div>
    <ErrorRecovery error={plan.error || job.error} retry={() => { void plan.refetch(); if (exportId) void job.refetch(); }} />
    {plan.data ? <>
      <section className="status-grid deployment-summary"><article><Archive /><span>预计有效载荷</span><strong>{formatBytes(plan.data.estimatedBytes)}</strong><small>首次导出可能再增加离线依赖缓存</small></article><article><CheckCircle2 /><span>当前磁盘余量</span><strong>{formatBytes(plan.data.diskFreeBytes)}</strong><small>受控源 {plan.data.sourceStatus}</small></article><article><CheckCircle2 /><span>来源 revision</span><strong>{plan.data.sourceRevision}</strong><small>仅作观测，不进入兼容摘要</small></article><article><CheckCircle2 /><span>兼容摘要</span><strong className="mono">{plan.data.compatibilityDigest.slice(0, 12)}</strong><small>依赖锁 / Recipe / 协议</small></article></section>
      <section className="card"><header><div><Archive /><h2>部署内容</h2></div><span className={`badge ${plan.data.ready ? "good" : "bad"}`}>{plan.data.ready ? "READY" : "BLOCKED"}</span></header><div className="dense-list deployment-components">{plan.data.components.map(item => <div key={item.id}>{item.status === "ready" ? <CheckCircle2 className="good-text" /> : item.status === "downloadable" ? <Archive /> : <XCircle className="bad-text" />}<div><strong>{item.id}<b>{formatBytes(item.bytes)}</b></strong><small>{item.detail}</small></div></div>)}{plan.data.environments.map(item => <div key={item.id}>{item.ready ? <CheckCircle2 className="good-text" /> : <Archive />}<div><strong>{item.id}</strong><small>{item.ready ? "离线缓存已就绪" : "首次导出将从锁文件联网补齐"}</small></div></div>)}</div></section>
    </> : null}
    {job.data ? <section className={`card deployment-operation ${job.data.state}`}><header><div><Archive /><h2>{job.data.message}</h2></div><span className={`badge ${job.data.state === "completed" ? "good" : job.data.state === "failed" ? "bad" : "work"}`}>{job.data.state}</span></header><div className="progress-head"><strong>{Math.round(job.data.progress * 100)}%</strong><span>{job.data.stage}</span></div><div className="progress"><i style={{ width: `${Math.round(job.data.progress * 100)}%` }} /></div>{job.data.outputPath ? <p className="deployment-output mono">{job.data.outputPath}<br />{formatBytes(job.data.bytes)} · SHA-256 {job.data.sha256}</p> : null}{job.data.error ? <div className="notice error"><XCircle /><span>{job.data.error}</span></div> : null}{job.data.state === "completed" ? <div className="actions"><button className="secondary" onClick={() => void reveal()}><FolderOpen />打开目录</button><button className="secondary" onClick={() => void copySha()}><Copy />复制 SHA</button></div> : null}</section> : null}
    {message ? <div className="notice deployment-message"><span>{message}</span></div> : null}
  </div>;
}
