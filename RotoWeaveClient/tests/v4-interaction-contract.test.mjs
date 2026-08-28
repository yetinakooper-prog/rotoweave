import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import test from "node:test";

async function source(path) {
  return readFile(resolve(import.meta.dirname, path), "utf8");
}

test("V4 transient notices use one clickable 3000ms contract", async () => {
  const hook = await source("../app/lib/use-auto-dismiss-notice-v4.ts");
  assert.match(hook, /V4_TIP_DURATION_MS = 3000/);
  assert.match(hook, /window\.setTimeout/);
  for (const path of [
    "../app/client-shell-v4.tsx",
    "../app/features/client-settings-v4.tsx",
    "../app/features/global-settings-v4.tsx",
    "../app/features/material-manager-v4.tsx",
    "../app/features/action-editor-v4.tsx",
    "../app/features/export-workbench-v4.tsx",
  ]) {
    const text = await source(path);
    assert.match(text, /useAutoDismissNoticeV4/);
    assert.match(text, /onClick=\{\(\) => setNotice\(null\)\}/);
  }
});

test("Ctrl+S routes through the active page and Escape closes only the top dialog", async () => {
  const saveCommand = await source("../app/lib/page-save-command-v4.tsx");
  const escape = await source("../app/lib/use-escape-close.ts");
  const shell = await source("../app/client-shell-v4.tsx");
  assert.match(saveCommand, /event\.key\.toLowerCase\(\) !== "s"/);
  assert.match(saveCommand, /event\.preventDefault\(\)/);
  assert.match(saveCommand, /commandRef\.current \? await commandRef\.current\(\) : true/);
  assert.match(escape, /escapeStack\.at\(-1\)/);
  assert.match(escape, /active\.close\(\)/);
  assert.match(shell, /useEscapeClose\(onCancel, !busy\)/);
  assert.match(shell, /if \(!await executePageSave\(\)\) return/);
});

test("sidebar workspace menu switches recent workspaces and exits through the welcome gate", async () => {
  const shell = await source("../app/client-shell-v4.tsx");
  for (const label of [
    "工作区菜单",
    "打开工作区",
    "最近工作区",
    "当前工作区",
    "退出工作区",
  ]) {
    assert.ok(shell.includes(label), `missing workspace switcher label: ${label}`);
  }
  assert.match(shell, /workspace\.recent \?\? \[\]/);
  assert.match(shell, /api\.prepareAndCloseWorkspace\(\)/);
  assert.match(shell, /function requestWorkspaceOperation\(proceed: \(\) => void\)/);
  assert.match(shell, /dirtyAction && route\.page === "action"/);
  assert.match(shell, /setPendingLeave\(\{ proceed \}\)/);
  assert.match(shell, /window\.history\.replaceState/);
});

test("percent fields delegate invalid and Escape restoration to the shared numeric draft", async () => {
  const percent = await source("../app/components/percent-draft-input-v4.tsx");
  const numeric = await source("../app/components/numeric-draft-input.tsx");
  assert.match(percent, /<NumericDraftInput/);
  assert.match(numeric, /!Number\.isFinite\(parsed\)/);
  assert.match(numeric, /event\.key === "Escape"/);
  assert.match(numeric, /setDraft\(String\(value\)\)/);
});

test("client settings expose a trusted-LAN no-auth remote compute workflow", async () => {
  const settings = await source("../app/features/client-settings-v4.tsx");
  const api = await source("../app/lib/api.ts");
  assert.match(settings, /远程算力服务/);
  assert.match(settings, /服务端固定局域网 IPv4/);
  assert.match(settings, /HTTP API 端口/);
  assert.doesNotMatch(settings, /Bearer 凭据/);
  assert.doesNotMatch(settings, /LAN CA 证书/);
  assert.match(settings, /保存并测试连接/);
  assert.match(settings, /连接不加密，也不验证客户端身份/);
  assert.match(settings, /请勿映射到公网/);
  assert.match(api, /\/remote-service\/settings/);
  assert.match(api, /\/remote-service\/test/);
  assert.match(api, /new FormData\(\)/);
});

test("all transparent canvases share viewport wheel zoom and Shift drag locking", async () => {
  const action = await source("../app/components/action-canvas-v4.tsx");
  const global = await source("../app/components/global-settings-canvas.tsx");
  const material = await source("../app/features/material-manager-v4.tsx");
  for (const text of [action, global, material]) {
    assert.match(text, /zoomCanvasViewportAtPoint/);
  }
  assert.doesNotMatch(action, /onScale/);
  assert.match(action, /mode === "shadow-x"/);
  assert.match(action, /onShadowX\(dx\)/);
  assert.match(global, /constrainDragPoint/);
  assert.match(material, /constrainDragPoint/);
  assert.match(material, /event\.shiftKey/);
});
