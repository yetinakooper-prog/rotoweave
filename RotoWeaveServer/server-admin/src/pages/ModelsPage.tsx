import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, CheckCircle2, FileInput, Gauge, Link2, RefreshCcw, Unlink } from "lucide-react";
import { api } from "../api";
import { Badge, Card, ErrorRecovery, Skeleton, formatBytes, short } from "../components";
import type { ModelAsset, ModelCenter, ModelOperation, RecipeSlot } from "../types";
import { useUi } from "../ui";

type SelectionResult = { cancelled: true } | { cancelled: false; operation: ModelOperation };

export function ModelsPage() {
  const client = useQueryClient();
  const { confirm, toast } = useUi();
  const query = useQuery({
    queryKey: ["model-center"],
    queryFn: () => api<ModelCenter>("/model-center"),
    refetchInterval: ({ state }) => state.data?.operations.some((item) => ["queued", "running"].includes(item.state)) ? 1000 : false,
  });
  const mutation = useMutation({
    mutationFn: ({ path, method = "POST" }: { path: string; method?: string }) => api<unknown>(path, { method }),
    onSuccess: () => { void client.invalidateQueries({ queryKey: ["model-center"] }); },
  });
  const write = async (options: { title: string; message: string; path: string; method?: string; danger?: boolean }) => {
    if (!await confirm({ title: options.title, message: options.message, danger: options.danger, confirmLabel: "继续" })) return;
    await mutation.mutateAsync({ path: options.path, method: options.method });
    toast("操作已提交");
  };
  const select = async (path: string) => {
    const result = await mutation.mutateAsync({ path }) as SelectionResult | ModelOperation;
    if ("cancelled" in result && result.cancelled) return;
    toast("模型选择已提交");
  };
  if (query.isLoading) return <Skeleton rows={8} />;
  const data = query.data;
  if (!data) return <ErrorRecovery error={query.error || new Error("模型中心快照不可用")} retry={() => void query.refetch()} />;
  const running = data.operations.find(item => ["queued", "running"].includes(item.state));
  const readyProfiles = (["high", "ultra"] as const).filter(profile => data.profiles[profile].state === "ready");
  return <div className="page-stack model-center">
    <div className="page-title"><div><p className="eyebrow">Recipe · 原地模型引用 · 固定运行时</p><h1>模型中心</h1><p>通过 Windows 原生对话框选择服务器本机模型；文件不会上传、复制或删除。</p></div><div className="actions"><button className="secondary" onClick={() => void query.refetch()}><RefreshCcw />刷新</button></div></div>
    <ErrorRecovery error={query.error || mutation.error} retry={() => void query.refetch()} />
    <div className="profile-grid">{(["high", "ultra"] as const).map(profile => <ProfileCard key={profile} profile={profile} state={data.profiles[profile]} />)}</div>
    {running && <OperationStrip operation={running} cancel={() => void write({ title: "取消模型操作", message: `取消 ${running.kind}？已经完成的验证记录会保留。`, path: `/model-operations/${running.id}`, method: "DELETE", danger: true })} />}
    <Card title="matting-high-ultra-v1 Recipe 槽位" icon={Link2}>
      <div className="slot-list">{data.slots.map(slot => <SlotRow key={slot.role} slot={slot} running={!!running} choose={() => void select(`/model-selections/${slot.role}/file-dialog`)} unbind={() => void write({ title: "解除模型绑定", message: `解除 ${slot.displayName} 后，相关 Profile 将退出 READY。`, path: `/model-bindings/${slot.role}`, method: "DELETE", danger: true })} />)}</div>
    </Card>
    <div className="activation-bar"><div><strong>草稿 {short(data.draftConfigurationDigest, 18)}</strong><span>活动 {short(data.activeConfiguration?.configurationDigest, 18)} · 可部分激活 {readyProfiles.map(item => `${item.toUpperCase()}（${qualificationLabel(data.profiles[item].qualification)}）`).join(" + ") || "无 READY 档"}</span></div><div className="activation-steps"><button disabled={!!running || !data.slots.some(item => item.binding)} onClick={() => void write({ title: "验证已绑定模型", message: "官方文件核对精确 SHA-256；未知身份在固定产品进程中执行安全结构解析。", path: "/model-configurations/draft/verify" })}>1 验证</button><button disabled={!!running || !data.draftConfigurationDigest} onClick={() => void write({ title: "执行分档 GPU 自检", message: "分别测试 full、balanced、constrained、minimal；通过后取得 READY 资格，但不会自动激活。", path: "/model-configurations/draft/self-test" })}>2 分档自检</button><button className="activate" disabled={!!running || readyProfiles.length === 0} onClick={() => void write({ title: "激活 READY 档位", message: `队列会排空并激活：${readyProfiles.map(item => item.toUpperCase()).join(" + ")}。切换开始后若失败，服务会进入维护态且不会自动回滚。`, path: "/model-configurations/draft/activate" })}><CheckCircle2 />3 激活</button></div></div>
  </div>;
}

function ProfileCard({ profile, state }: { profile: "high" | "ultra"; state: ModelCenter["profiles"]["high"] }) {
  return <article className={`profile-card ${profile}`}><div><span>{profile.toUpperCase()}</span><Badge state={state.state}>{state.state}</Badge></div><strong>{profile === "high" ? "稳定生产链" : "SAM3 精细 Alpha"}</strong><small>{state.runtime.id} · {state.runtime.source} · {qualificationLabel(state.qualification)}</small><ul>{state.blockers.length ? state.blockers.map(item => <li key={item}><AlertTriangle />{item}</li>) : <li className="ready"><CheckCircle2 />四种内存模式与当前 GPU 自检有效</li>}{state.localCompatibleRoles.length > 0 && <li><AlertTriangle />本机兼容：{state.localCompatibleRoles.join("、")}；运行通过，但画质、来源和许可未获官方认证</li>}</ul></article>;
}

function OperationStrip({ operation, cancel }: { operation: ModelOperation; cancel: () => void }) {
  const detail = operation.detail as { ambiguousRoles?: string[]; missingRoles?: string[] };
  const summary = detail.ambiguousRoles?.length || detail.missingRoles?.length
    ? `歧义：${detail.ambiguousRoles?.join("、") || "无"}；缺失：${detail.missingRoles?.join("、") || "无"}`
    : operation.error || `操作 ${operation.id}`;
  return <div className="operation-strip"><Gauge /><div><strong>{operation.kind} · {operation.stage}</strong><span>{summary}</span></div><div className="progress"><i style={{ width: `${operation.progress * 100}%` }} /></div><b>{Math.round(operation.progress * 100)}%</b><button className="secondary" disabled={operation.kind === "activate" && ["draining", "switching"].includes(operation.stage)} onClick={cancel}>取消</button></div>;
}

function SlotRow({ slot, running, choose, unbind }: { slot: RecipeSlot; running: boolean; choose: () => void; unbind: () => void }) {
  const status = slot.binding ? assetStatusLabel(slot.binding) : "未绑定";
  return <article className="slot"><div className="slot-identity"><Badge state={slot.state}>{status}</Badge><span><strong>{slot.displayName}</strong><small>{slot.role} · {slot.runtimeContract}</small></span></div><div className="expected"><span>官方精确身份</span><strong>{slot.filename} · {formatBytes(slot.bytes)}</strong><small title={slot.sha256}>SHA-256 {short(slot.sha256, 18)} · rev {short(slot.revision, 10)}</small><small><a href={slot.sourceUrl} target="_blank" rel="noreferrer">官方来源</a> · {slot.licenseId} · 请自行下载</small></div><div className="binding"><span>当前绑定</span><strong className="path-cell" title={slot.binding?.path}>{slot.binding?.path || "未绑定"}</strong><small>{slot.binding ? `${assetStatusLabel(slot.binding)} · SHA ${short(slot.binding.sha256, 14)}` : "请选择模型文件"}</small></div><div className="slot-actions"><button className="secondary" disabled={running} onClick={choose}><FileInput />{slot.binding ? "更换模型文件" : "选择模型文件"}</button>{slot.binding && <button className="danger ghost" disabled={running} onClick={unbind}><Unlink /></button>}</div>{slot.error && <div className="slot-error"><AlertTriangle />{slot.error}</div>}</article>;
}

function qualificationLabel(value: "official" | "local-compatible") {
  return value === "local-compatible" ? "本机兼容" : "官方验证";
}

function assetStatusLabel(asset: ModelAsset) {
  if (asset.state === "incompatible") return "不兼容";
  if (asset.state === "candidate") return "待验证";
  if (asset.verificationKind === "structural") return "结构验证通过，待自检";
  if (asset.verificationKind === "official") return "官方验证";
  return asset.state;
}
