import { Archive, CheckCircle2, Copy, FolderOpen, Loader2, RefreshCcw, Square, XCircle } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { api } from "../lib/api";

function formatBytes(value?: number | null): string {
  if (!value) return "0 B";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let amount = value;
  let index = 0;
  while (amount >= 1024 && index < units.length - 1) { amount /= 1024; index += 1; }
  return `${amount.toFixed(index > 1 ? 2 : 0)} ${units[index]}`;
}

export function DeploymentBundlePanel() {
  const plan = useQuery({ queryKey: ["deployment-bundle-plan", "client"], queryFn: api.deploymentBundlePlan });
  const [exportId, setExportId] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const job = useQuery({
    queryKey: ["deployment-bundle-export", exportId],
    queryFn: () => api.deploymentBundleExport(exportId as string),
    enabled: !!exportId,
    refetchInterval: query => ["queued", "running"].includes(query.state.data?.state ?? "") ? 750 : false,
  });
  const active = job.data && ["queued", "running"].includes(job.data.state);

  async function startExport() {
    setNotice(null);
    try {
      const selected = await api.chooseDeploymentBundleDirectory();
      if (!selected.selectionToken) { setNotice("已取消选择输出目录。"); return; }
      const started = await api.startDeploymentBundleExport(selected.selectionToken);
      setExportId(started.id);
      setNotice(`正在导出到 ${selected.displayPath}`);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "部署包导出启动失败。");
    }
  }

  async function cancelExport() {
    if (!exportId) return;
    try { await api.cancelDeploymentBundleExport(exportId); await job.refetch(); }
    catch (error) { setNotice(error instanceof Error ? error.message : "取消失败。"); }
  }

  async function reveal() {
    if (!exportId) return;
    try { await api.revealDeploymentBundleExport(exportId); }
    catch (error) { setNotice(error instanceof Error ? error.message : "无法打开输出目录。"); }
  }

  async function copySha() {
    if (!job.data?.sha256) return;
    await navigator.clipboard.writeText(job.data.sha256);
    setNotice("ZIP SHA-256 已复制。");
  }

  if (plan.isLoading) return <article className="deployment-bundle-panel"><Loader2 className="spin" />正在检查部署包内容</article>;
  if (plan.isError || !plan.data) return <article className="deployment-bundle-panel"><XCircle /><strong>部署包预检失败</strong><p>{plan.error instanceof Error ? plan.error.message : "无法读取部署包计划。"}</p><button type="button" onClick={() => void plan.refetch()}><RefreshCcw size={13} />重试</button></article>;

  return <article className="deployment-bundle-panel">
    <div><small>DEPLOYMENT BUNDLE</small><h2>客户端环境缓存包</h2><p>导出锁定依赖、离线安装缓存和固定工具链；Basic 始终由目标机 Setup 从固定源码动态生成，不进入 ZIP。</p></div>
    <div className="deployment-bundle-summary"><span><strong>{formatBytes(plan.data.estimatedBytes)}</strong><small>当前预估有效载荷</small></span><span><strong>{formatBytes(plan.data.diskFreeBytes)}</strong><small>当前磁盘余量</small></span><span><strong>{plan.data.sourceRevision}</strong><small>来源 revision</small></span><span><strong>{plan.data.compatibilityDigest.slice(0, 12)}</strong><small>兼容摘要 · 来源 {plan.data.sourceStatus}</small></span></div>
    <div className="deployment-bundle-components">
      {plan.data.components.map(item => <div key={item.id} className={item.status === "ready" ? "ready" : item.status === "downloadable" ? "pending" : "blocked"}>{item.status === "ready" ? <CheckCircle2 /> : item.status === "downloadable" ? <Archive /> : <XCircle />}<span><strong>{item.id}</strong><small>{item.detail}</small></span><b>{formatBytes(item.bytes)}</b></div>)}
      {plan.data.environments.map(item => <div key={item.id} className={item.ready ? "ready" : "pending"}>{item.ready ? <CheckCircle2 /> : <Archive />}<span><strong>{item.id}</strong><small>{item.ready ? "离线缓存已就绪" : "首次导出将联网补齐并验证缓存"}</small></span></div>)}
    </div>
    {job.data ? <div className={`deployment-bundle-job ${job.data.state}`}>
      <div><strong>{job.data.message}</strong><small>{job.data.stage} · {Math.round(job.data.progress * 100)}%</small></div>
      <div className="deployment-bundle-progress"><i style={{ width: `${Math.round(job.data.progress * 100)}%` }} /></div>
      {job.data.outputPath ? <p>{job.data.outputPath}<br /><span>{formatBytes(job.data.bytes)} · SHA-256 {job.data.sha256}</span></p> : null}
      {job.data.error ? <p className="error">{job.data.error}</p> : null}
    </div> : null}
    <footer>
      {active ? <button type="button" className="danger" onClick={() => void cancelExport()}><Square size={12} />取消导出</button> : <button type="button" disabled={!plan.data.ready} onClick={() => void startExport()}><Archive size={13} />选择目录并导出 ZIP</button>}
      {job.data?.state === "completed" ? <><button type="button" onClick={() => void reveal()}><FolderOpen size={13} />打开目录</button><button type="button" onClick={() => void copySha()}><Copy size={13} />复制 SHA</button></> : null}
    </footer>
    {notice ? <button type="button" className="client-settings-v4-notice" role="status" onClick={() => setNotice(null)}>{notice}</button> : null}
  </article>;
}
