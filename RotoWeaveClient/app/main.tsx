import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import "./globals.css";
import ClientShellV4 from "./client-shell-v4";
import { bootstrapBrowserSession } from "./lib/api";
import { PageSaveCommandProviderV4 } from "./lib/page-save-command-v4";
import { Providers } from "./providers";
import { useWorkspaceStore } from "./lib/store";


async function start(): Promise<void> {
  const applyTheme = (): void => {
    document.documentElement.dataset.theme = useWorkspaceStore.getState().theme;
  };
  applyTheme();
  useWorkspaceStore.subscribe((state, previous) => {
    if (state.theme !== previous.theme) applyTheme();
  });
  await bootstrapBrowserSession();
  const root = document.getElementById("root");
  if (!root) throw new Error("缺少应用根节点。");
  createRoot(root).render(
    <StrictMode>
      <Providers>
        <PageSaveCommandProviderV4><ClientShellV4 /></PageSaveCommandProviderV4>
      </Providers>
    </StrictMode>,
  );
}


void start().catch((error: unknown) => {
  const root = document.getElementById("root");
  if (root) {
    const message = error instanceof Error ? error.message : "本机会话初始化失败。";
    root.innerHTML = "";
    const panel = document.createElement("div");
    panel.className = "boot-shell error";
    panel.textContent = message;
    root.append(panel);
  }
});
