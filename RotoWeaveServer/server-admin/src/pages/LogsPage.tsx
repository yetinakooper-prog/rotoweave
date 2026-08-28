import { useQuery } from "@tanstack/react-query";
import { Archive, Search } from "lucide-react";
import { useMemo, useState } from "react";
import { api } from "../api";
import { Badge, Drawer, Empty, ErrorRecovery, Skeleton, short } from "../components";
import type { LogItem } from "../types";
import { CopyButton } from "../ui";

export function LogsPage() {
  const [filters, setFilters] = useState({ level: "", profile: "", modelRole: "", configurationDigest: "", operationId: "", text: "" });
  const [applied, setApplied] = useState(filters);
  const [selected, setSelected] = useState<LogItem>();
  const params = useMemo(() => new URLSearchParams(Object.entries(applied).filter(([, value]) => value)), [applied]);
  const query = useQuery({ queryKey: ["logs", params.toString()], queryFn: () => api<{ items: LogItem[]; total: number }>(`/logs?${params}`) });
  if (query.isLoading) return <Skeleton rows={9} />;
  return <div className="page-stack">
    <div className="page-title"><div><p className="eyebrow">结构化诊断 · 本机路径脱敏导出</p><h1>错误与审计日志</h1><p>按 Profile、模型角色、配置摘要与模型操作筛选；详情在抽屉中查看和复制。</p></div><div className="actions"><a className="button secondary" href="/api/admin/v2/logs/export?format=ndjson">NDJSON</a><a className="button" href="/api/admin/v2/logs/diagnostic.zip"><Archive />诊断 ZIP</a></div></div>
    <ErrorRecovery error={query.error} retry={() => void query.refetch()} />
    <div className="toolbar log-filters"><select aria-label="级别" value={filters.level} onChange={event => setFilters({ ...filters, level: event.target.value })}><option value="">全部级别</option><option>error</option><option>warning</option><option>info</option></select><select aria-label="Profile" value={filters.profile} onChange={event => setFilters({ ...filters, profile: event.target.value })}><option value="">全部 Profile</option><option>high</option><option>ultra</option></select><input value={filters.modelRole} onChange={event => setFilters({ ...filters, modelRole: event.target.value })} placeholder="模型角色" /><input value={filters.configurationDigest} onChange={event => setFilters({ ...filters, configurationDigest: event.target.value })} placeholder="配置摘要" /><input value={filters.operationId} onChange={event => setFilters({ ...filters, operationId: event.target.value })} placeholder="操作 ID" /><div className="search"><Search /><input value={filters.text} onChange={event => setFilters({ ...filters, text: event.target.value })} onKeyDown={event => event.key === "Enter" && setApplied(filters)} placeholder="事件、Job ID 或错误文本" /><button onClick={() => setApplied(filters)}>筛选</button></div></div>
    <div className="log-table"><div className="log-head"><span>时间</span><span>级别</span><span>组件 / 事件</span><span>Job / 配置</span></div>{(query.data?.items || []).map(item => <button className="log-row" key={item.id} onClick={() => setSelected(item)}><span>{new Date(item.createdAt).toLocaleString()}</span><span><Badge state={item.level}>{item.level}</Badge></span><span><strong>{item.component}</strong><small>{item.event}</small></span><span className="mono">{short(item.jobId || item.detail.configurationDigest || item.detail.operationId, 15)}</span></button>)}{!query.data?.items.length && <Empty title="没有匹配日志" detail="调整 Profile、角色、配置或文本筛选。" />}</div>
    {selected && <Drawer title="结构化日志详情" onClose={() => setSelected(undefined)}><div className="drawer-summary"><Badge state={selected.level}>{selected.level}</Badge><span>{selected.component} / {selected.event}</span><CopyButton value={selected} label="复制 JSON" /></div><pre>{JSON.stringify(selected, null, 2)}</pre></Drawer>}
  </div>;
}
