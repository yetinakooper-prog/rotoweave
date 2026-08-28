import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import test from "node:test";

import ts from "typescript";


const sourcePath = resolve(
  import.meta.dirname,
  "..",
  "app",
  "lib",
  "workspace-revision-state.ts",
);
const source = await readFile(sourcePath, "utf8");
const compiled = ts.transpileModule(source, {
  compilerOptions: {
    module: ts.ModuleKind.ES2022,
    target: ts.ScriptTarget.ES2022,
  },
  fileName: sourcePath,
}).outputText;
const { WorkspaceRevisionState } = await import(
  `data:text/javascript;base64,${Buffer.from(compiled).toString("base64")}`
);

test("older responses cannot roll an aggregate revision backward", () => {
  const state = new WorkspaceRevisionState();
  const oldQuery = state.beginObservation();
  state.remember(
    { workspaceFormatVersion: 3, domainRevision: 7, revisionId: "rev_old" },
    oldQuery,
  );

  const mutation = state.beginObservation();
  assert.equal(state.revisionForMutation("/domain/characters"), "rev_old");
  state.applyMutationRevision(
    "/domain/characters",
    "rev_current",
    mutation,
  );
  state.remember(
    { workspaceFormatVersion: 3, domainRevision: 7, revisionId: "rev_old" },
    oldQuery,
  );

  assert.equal(
    state.revisionForMutation("/domain/characters"),
    "rev_current",
  );
});

test("the mutation response header wins over an older payload snapshot", () => {
  const state = new WorkspaceRevisionState();
  const observation = state.beginObservation();

  state.applyMutationRevision(
    "/domain/characters/chr_hero/settings",
    "rev_header",
    observation,
  );
  state.remember(
    {
      character: { id: "chr_hero" },
      revisionId: "rev_payload",
    },
    observation,
  );

  assert.equal(
    state.revisionForMutation("/domain/characters/chr_hero/settings"),
    "rev_header",
  );
});

test("workspace reset rejects responses from the previous session", () => {
  const state = new WorkspaceRevisionState();
  const previousWorkspaceQuery = state.beginObservation();
  state.reset();
  state.remember(
    { workspaceFormatVersion: 3, domainRevision: 7, revisionId: "rev_previous" },
    previousWorkspaceQuery,
  );

  assert.equal(
    state.revisionForMutation("/domain/characters"),
    null,
  );
});
