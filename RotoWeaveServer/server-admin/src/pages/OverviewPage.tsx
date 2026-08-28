import { useMutation, useQuery } from "@tanstack/react-query";
import { Activity, AlertTriangle, Cpu, Gauge, HardDrive, ListOrdered, Network, Save, Server } from "lucide-react";
import { useEffect, useState } from "react";
import { api } from "../api";
import { Badge, Card, ErrorRecovery, Skeleton, formatBytes, short } from "../components";
import type { HardwareStatus, HardwareWarning, Json, ModelCenter, NetworkSettings } from "../types";
import { useUi } from "../ui";

export function OverviewPage() {
  const query = useQuery({ queryKey: ["overview"], queryFn: () => api<Json>("/overview") });
  const { toast } = useUi();
  const [apiPort, setApiPort] = useState("");
  const data = query.data || {};
  const network = (data.network || {}) as Partial<NetworkSettings>;
  useEffect(() => {
    setApiPort(network.configuredPort ? String(network.configuredPort) : network.apiPort ? String(network.apiPort) : "");
  }, [network.apiPort, network.configuredPort]);
  const networkMutation = useMutation({
    mutationFn: () => api<NetworkSettings>("/network-settings", {
      method: "PUT",
      body: JSON.stringify({ apiPort: Number(apiPort) }),
    }),
    onSuccess: saved => {
      toast(saved.restartRequired ? "端口已保存，下次启动生效" : "端口设置未变化");
      void query.refetch();
    },
  });
  if (query.isLoading) return <Skeleton rows={7} />;
  const worker = (data.worker || {}) as Json;
  const detail = (worker.detail || {}) as Json;
  const hardware = ((detail.hardware || worker.hardware || {}) as unknown) as Partial<HardwareStatus>;
  const selectedDevice = hardware.selectedDevice;
  const warnings = [
    ...((worker.warnings || []) as unknown as HardwareWarning[]),
    ...((detail.warnings || []) as unknown as HardwareWarning[]),
    ...((hardware.warnings || []) as HardwareWarning[]),
  ].filter((item, index, all) => all.findIndex(other => other.code === item.code && other.profile === item.profile) === index);
  const execution = ((detail.execution || {}) as Json);
  const memoryPlan = ((detail.memoryPlan || {}) as Json);
  const queue = (data.queue || {}) as Json;
  const states = (queue.states || {}) as Record<string, number>;
  const disk = (data.disk || {}) as Record<string, number>;
  const startup = (data.startup || {}) as Json;
  const center = (data.modelCenter || {}) as ModelCenter;
  const profiles = center.profiles || {} as ModelCenter["profiles"];
  const resident = String(worker.profile || (detail.modelConfiguration as Json | undefined)?.profile || "high");
  const blockers = ["high", "ultra"].flatMap(profile => profiles[profile as "high" | "ultra"]?.blockers || []);
  return <div className="page-stack">
    <div className="page-title"><div><p className="eyebrow">RotoWeave 4.0 · localhost</p><h1>服务运行总览</h1><p>服务端拥有固定运行时和调度；模型权重由用户模型库提供。</p></div><Badge state={String(worker.state || "starting")}>{String(worker.state || "starting")}</Badge></div>
    <ErrorRecovery error={query.error || networkMutation.error} retry={() => void query.refetch()} />
    {warnings.length > 0 && <div className="network-hint warning"><AlertTriangle /><span><strong>{warnings.length} 条非阻断运行警告</strong>{warnings[0].message} {warnings[0].action}</span></div>}
    {blockers.length > 0 && <a className="blocker-banner" href="#/models"><AlertTriangle /><span><strong>{blockers.length} 个 Profile 阻断项</strong>{blockers[0]}</span><b>前往模型中心 →</b></a>}
    <Card title="局域网服务地址" icon={Network}>
      <form className="network-form network-form-simple" onSubmit={event => { event.preventDefault(); networkMutation.mutate(); }}>
        <label><span>服务地址</span><input aria-label="服务地址" value={network.serviceHost || "未检测到局域网地址"} readOnly /></label>
        <label><span>本机端口（1024–65535）</span><input aria-label="本机端口" type="number" min={1024} max={65535} value={apiPort} onChange={event => setApiPort(event.target.value)} /></label>
        <button type="submit" disabled={networkMutation.isPending || !network.serviceHost || !apiPort || Number(apiPort) < 1024 || Number(apiPort) > 65535}><Save />{networkMutation.isPending ? "检查并保存…" : "保存端口"}</button>
      </form>
      <p className="network-footnote">服务地址由本机默认路由自动识别，不可修改。端口保存后在下次启动时生效。</p>
      {network.addressError && <div className="network-hint warning"><AlertTriangle /><span><strong>未检测到正式局域网地址</strong>{network.addressError}</span></div>}
      {network.configurationError && <div className="network-hint warning"><AlertTriangle /><span><strong>启动配置需要修正</strong>{network.configurationError}</span></div>}
    </Card>
    <div className="status-grid">
      <article><Server /><span>服务</span><strong>{String(worker.state || "starting")}</strong><small>远程协议 v{String(data.protocolVersion || 1)}</small></article>
      {(["high", "ultra"] as const).map(profile => <article key={profile}><Gauge /><span>{profile.toUpperCase()}</span><strong>{profiles[profile]?.state || "blocked"}</strong><small>{profiles[profile]?.blockers?.[0] || "Recipe 与 GPU 自检有效"}</small></article>)}
      <article><Cpu /><span>GPU / 驻留 Worker</span><strong>{selectedDevice?.gpuName || resident.toUpperCase()}</strong><small>{selectedDevice ? `驱动 ${selectedDevice.driverVersion} · CC ${selectedDevice.computeCapability || "未知"} · 空闲 ${selectedDevice.vramFreeMiB} / ${selectedDevice.vramTotalMiB} MiB` : "未选择 CUDA 设备"}</small></article>
      <article><Gauge /><span>内存执行计划</span><strong>{String(memoryPlan.selectedMode || execution.memoryMode || "待自检")}</strong><small>{Array.isArray(execution.cpuStages) && execution.cpuStages.length ? `CPU 阶段：${execution.cpuStages.join(", ")} · 耗时可能显著增加` : "GPU 全链或尚未执行"}</small></article>
      <article><ListOrdered /><span>活动队列</span><strong>{(states.queued || 0) + (states.running || 0)} 项</strong><small>{states.running || 0} 正在运行</small></article>
      <article><HardDrive /><span>可用磁盘</span><strong>{formatBytes(disk.freeBytes)}</strong><small>总计 {formatBytes(disk.totalBytes)}</small></article>
    </div>
    <div className="two-column">
      <Card title="启动与运行阶段" icon={Activity}>
        <div className="progress-head"><strong>{Math.round(Number(startup.progress || 0) * 100)}%</strong><span>{String(startup.completedStages || 0)} / {String(startup.totalStages || 8)}</span></div>
        <div className="progress"><i style={{ width: `${Number(startup.progress || 0) * 100}%` }} /></div>
        <div className="dense-list">{((startup.stages || []) as Json[]).map(stage => <div key={String(stage.id)}><Badge state={String(stage.state)} /><span><strong>{String(stage.label)}</strong><small>{String(stage.error || stage.detail || "等待")}</small></span></div>)}</div>
      </Card>
      <Card title="最近模型操作" icon={Gauge}>
        <div className="dense-list">{(center.operations || []).slice(0, 6).map(item => <div key={item.id}><Badge state={item.state} /><span><strong>{item.kind} · {item.stage}</strong><small>{short(item.id, 18)} · {Math.round(item.progress * 100)}%</small></span></div>)}{!center.operations?.length && <p className="muted-copy">尚无模型扫描、验证或激活操作。</p>}</div>
      </Card>
    </div>
  </div>;
}
