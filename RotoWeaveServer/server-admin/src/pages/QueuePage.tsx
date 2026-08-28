import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronDown, ChevronUp, CirclePause, CirclePlay, RotateCcw, Square, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";
import { api } from "../api";
import { Badge, Drawer, Empty, ErrorRecovery, Skeleton, short } from "../components";
import type { Job, Json, QueueControl } from "../types";
import { CopyButton, useUi } from "../ui";

export function QueuePage() {
  const client = useQueryClient();
  const { confirm, toast } = useUi();
  const [state, setState] = useState("");
  const [profile, setProfile] = useState("");
  const [selected, setSelected] = useState<Job>();
  const query = useQuery({ queryKey: ["queue", state, profile], queryFn: () => api<{ items: Job[]; total: number; control: QueueControl }>(`/queue?${new URLSearchParams({ ...(state && { state }), ...(profile && { profile }) })}`) });
  const [jobs, setJobs] = useState<Job[]>([]);
  useEffect(() => setJobs(query.data?.items || []), [query.data]);
  const mutation = useMutation({ mutationFn: ({ path, method = "POST", body = {} }: { path: string; method?: string; body?: Json }) => api(path, { method, body: JSON.stringify(body) }), onSuccess: () => { toast("队列操作已完成"); void client.invalidateQueries({ queryKey: ["queue"] }); } });
  const write = async (title: string, message: string, path: string, body: Json = {}, method = "POST", danger = false) => {
    if (await confirm({ title, message, danger })) await mutation.mutateAsync({ path, method, body });
  };
  if (query.isLoading) return <Skeleton rows={8} />;
  const control = query.data?.control || { paused: false, maintenance: false, mode: "normal", revision: 0 };
  const queued = jobs.filter(item => item.state === "queued");
  const reorder = (id: string, direction: -1 | 1) => {
    const ids = queued.map(item => item.id); const from = ids.indexOf(id); const to = from + direction;
    if (from < 0 || to < 0 || to >= ids.length) return;
    [ids[from], ids[to]] = [ids[to], ids[from]];
    void write("调整队列顺序", "队列 revision 会在写入时校验，冲突时请刷新重试。", "/queue/reorder", { jobIds: ids, revision: control.revision });
  };
  return <div className="page-stack">
    <div className="page-title"><div><p className="eyebrow">冻结配置 · Profile 切换阶段 · 串行 GPU</p><h1>任务队列</h1><p>每项任务冻结 quality Profile 与配置摘要；运行时切换不会并存两个 GPU Worker。</p></div><div className="actions"><button className="secondary" onClick={() => void write(control.paused ? "恢复调度" : "暂停调度", control.paused ? "继续领取队列中的任务。" : "当前运行任务不会被打断。", control.paused ? "/queue/resume" : "/queue/pause", { revision: control.revision })}>{control.paused ? <CirclePlay /> : <CirclePause />}{control.paused ? "恢复" : "暂停"}</button><button className="secondary" onClick={() => void write("清理终态任务", "删除 completed / failed / cancelled 任务文件，审计日志继续保留。", "/queue/cleanup", {}, "POST", true)}><Trash2 />批量清理</button><button className="danger" onClick={() => void write("紧急停止", "将取消当前任务、终止 Worker 并释放 CUDA。", "/queue/emergency-stop", { confirm: "EMERGENCY_STOP" }, "POST", true)}><Square />紧急停止</button></div></div>
    <ErrorRecovery error={query.error || mutation.error} retry={() => void query.refetch()} />
    <div className="toolbar"><div className="tabs">{["", "queued", "running", "completed", "failed", "cancelled"].map(item => <button key={item} className={state === item ? "active" : ""} onClick={() => setState(item)}>{item || "全部状态"}</button>)}</div><div className="toolbar-right"><select aria-label="Profile 筛选" value={profile} onChange={event => setProfile(event.target.value)}><option value="">High + Ultra</option><option value="high">High</option><option value="ultra">Ultra</option></select><Badge state={control.mode}>{control.mode}</Badge><span>rev {control.revision}</span></div></div>
    <div className="job-list">{jobs.map(job => <article className="job" key={job.id} onClick={() => setSelected(job)}><div className="job-order" onClick={event => event.stopPropagation()}>{job.state === "queued" ? <><button aria-label="上移" onClick={() => reorder(job.id, -1)}><ChevronUp /></button><b>{job.queueOrder}</b><button aria-label="下移" onClick={() => reorder(job.id, 1)}><ChevronDown /></button></> : <Badge state={job.state}>{job.state}</Badge>}</div><div className="job-body"><div className="title-row"><strong>{job.id}</strong><Badge state={job.qualityProfile || "high"}>{(job.qualityProfile || "high").toUpperCase()}</Badge><Badge state={job.state}>{job.state}</Badge></div><div className="job-progress"><div className="progress"><i style={{ width: `${job.progress * 100}%` }} /></div><span>{Math.round(job.progress * 100)}% · {job.stage || "—"}</span></div><div className="meta"><span>配置 {short(job.modelConfigurationDigest, 16)}</span><span>更新 {new Date(job.updatedAt).toLocaleString()}</span>{job.error?.message && <span className="bad-text">{job.error.message}</span>}</div></div><div className="job-actions" onClick={event => event.stopPropagation()}>{["queued", "running"].includes(job.state) && <button className="danger ghost" onClick={() => void write("取消任务", `取消 ${job.id}？`, `/jobs/${job.id}/cancel`, {}, "POST", true)}><Square />取消</button>}{["completed", "failed", "cancelled"].includes(job.state) && <button onClick={() => void write("重试任务", "新任务会继承冻结的 Profile 与模型配置。", `/jobs/${job.id}/retry`)}><RotateCcw />重试</button>}{["completed", "failed", "cancelled"].includes(job.state) && <button className="danger ghost" onClick={() => void write("删除任务", "删除任务文件但保留运营审计记录。", `/jobs/${job.id}`, {}, "DELETE", true)}><Trash2 /></button>}</div></article>)}{!jobs.length && <Empty title="当前筛选没有任务" detail="新提交任务会显示其 High/Ultra Profile 和冻结配置摘要。" />}</div>
    {selected && <JobDrawer jobId={selected.id} fallback={selected} onClose={() => setSelected(undefined)} />}
  </div>;
}

function JobDrawer({ jobId, fallback, onClose }: { jobId: string; fallback: Job; onClose: () => void }) {
  const query = useQuery({ queryKey: ["job", jobId], queryFn: () => api<{ job: Job; events: Json[] }>(`/jobs/${jobId}`) });
  const job = query.data?.job || fallback;
  return <Drawer title="任务详情" onClose={onClose}><div className="drawer-summary"><Badge state={job.qualityProfile || "high"}>{(job.qualityProfile || "high").toUpperCase()}</Badge><Badge state={job.state}>{job.state}</Badge><CopyButton value={job} label="复制任务 JSON" /></div><dl><dt>Job ID</dt><dd>{job.id}</dd><dt>配置摘要</dt><dd>{job.modelConfigurationDigest || "—"}</dd><dt>运行阶段</dt><dd>{job.stage || "—"}</dd><dt>更新时间</dt><dd>{new Date(job.updatedAt).toLocaleString()}</dd></dl><h3>事件链</h3><pre>{JSON.stringify(query.data?.events || [], null, 2)}</pre></Drawer>;
}
