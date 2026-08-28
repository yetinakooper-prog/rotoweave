import { useQueryClient } from "@tanstack/react-query";
import { Activity, Archive, Box, Cpu, FileWarning, ListOrdered, ShieldCheck } from "lucide-react";
import { useEffect, useState } from "react";
import { ensureSession, eventsUrl } from "./api";
import { OverviewPage } from "./pages/OverviewPage";
import { ModelsPage } from "./pages/ModelsPage";
import { QueuePage } from "./pages/QueuePage";
import { LogsPage } from "./pages/LogsPage";
import { DeploymentPage } from "./pages/DeploymentPage";
import type { Page } from "./types";
import { UiProvider } from "./ui";

const navigation = [
  ["overview", Activity, "总览"],
  ["models", Box, "模型中心"],
  ["queue", ListOrdered, "任务队列"],
  ["logs", FileWarning, "错误日志"],
  ["deployment", Archive, "部署包"],
] as const;

function pageFromHash(): Page {
  const value = window.location.hash.replace(/^#\/?/, "").split("/")[0];
  return navigation.some(([id]) => id === value) ? value as Page : "overview";
}

function AppShell() {
  const [page, setPage] = useState<Page>(pageFromHash);
  const [connected, setConnected] = useState(false);
  const queryClient = useQueryClient();
  useEffect(() => {
    if (!window.location.hash) window.history.replaceState(null, "", "#/overview");
    const sync = () => setPage(pageFromHash());
    window.addEventListener("hashchange", sync);
    return () => window.removeEventListener("hashchange", sync);
  }, []);
  useEffect(() => {
    let source: EventSource | undefined;
    let retry: number | undefined;
    const connect = async () => {
      try {
        await ensureSession();
        source = new EventSource(eventsUrl);
        source.onopen = () => setConnected(true);
        source.addEventListener("entities-changed", event => {
          const payload = JSON.parse((event as MessageEvent).data) as { entities?: string[] };
          if (payload.entities?.includes("modelCenter")) void queryClient.invalidateQueries({ queryKey: ["model-center"] });
          if (payload.entities?.includes("overview")) {
            void queryClient.invalidateQueries({ queryKey: ["overview"] });
            void queryClient.invalidateQueries({ queryKey: ["queue"] });
            void queryClient.invalidateQueries({ queryKey: ["logs"] });
          }
        });
        source.onerror = () => { setConnected(false); source?.close(); retry = window.setTimeout(connect, 1600); };
      } catch { setConnected(false); retry = window.setTimeout(connect, 1600); }
    };
    void connect();
    return () => { source?.close(); if (retry) window.clearTimeout(retry); };
  }, [queryClient]);
  return <div className="shell">
    <aside><div className="brand"><div className="brand-mark"><Cpu /></div><div><strong>RotoWeave</strong><span>MODEL OPS · 4.0</span></div></div><nav aria-label="后台导航">{navigation.map(([id, Icon, label]) => <a key={id} href={`#/${id}`} className={page === id ? "active" : ""}><Icon />{label}</a>)}</nav><div className="side-status"><span className={`status-dot ${connected ? "good" : "bad"}`} /><div><strong>{connected ? "实时连接正常" : "正在重新连接"}</strong><small>断线后重新拉取实体快照</small></div></div><div className="ownership"><ShieldCheck /><span>部署包仅本机管理员生成<br />远程抠图协议 v1 不分发权重</span></div></aside>
    <main id="main-content" tabIndex={-1}>{page === "overview" && <OverviewPage />}{page === "models" && <ModelsPage />}{page === "queue" && <QueuePage />}{page === "logs" && <LogsPage />}{page === "deployment" && <DeploymentPage />}</main>
  </div>;
}

export function App() {
  return <UiProvider><AppShell /></UiProvider>;
}
