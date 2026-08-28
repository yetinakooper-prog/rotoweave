import assert from "node:assert/strict";
import { access, readFile } from "node:fs/promises";
import { resolve } from "node:path";
import test from "node:test";

async function source(path) {
  return readFile(resolve(import.meta.dirname, path), "utf8");
}

test("business undo UI, provider, API and mutation events are retired", async () => {
  const main = await source("../app/main.tsx");
  const shell = await source("../app/client-shell-v4.tsx");
  const api = await source("../app/lib/api.ts");
  await assert.rejects(access(resolve(import.meta.dirname, "../app/lib/undo-command-v4.tsx")));
  for (const text of [main, shell, api]) {
    assert.doesNotMatch(text, /UndoCommand|workspace\/undo|workspace-mutation|workspace-server-changed/);
  }
  assert.doesNotMatch(shell, /Ctrl\+Z|可用.*撤销|client-v4-undo-button/);
});

test("editors no longer keep draft undo snapshots", async () => {
  for (const path of [
    "../app/features/action-editor-v4.tsx",
    "../app/features/global-settings-v4.tsx",
    "../app/features/export-workbench-v4.tsx",
  ]) {
    const text = await source(path);
    assert.doesNotMatch(text, /recordDraft|clearDrafts|useUndoCommandV4/);
  }
});
