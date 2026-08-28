import type {
  ActionFrameDraftV4,
  Health,
  Job,
  PhotoshopSheetV4,
  RemoteServiceConnectionTest,
  RemoteServiceSettings,
  ShadowPreview,
  SizeProfile,
  SizeSystem,
  WorkspaceState,
  WorkspaceDomainV4,
  DomainActionV4,
  DeploymentBundleExport,
  DeploymentBundlePlan,
} from "./types";
import { LOCAL_API_PREFIX } from "./protocol-contract";
import { workspaceRevisionState } from "./workspace-revision-state";

const configuredApiBase = import.meta.env.VITE_ROTOWEAVE_API?.replace(/\/$/, "");
export const API_BASE = configuredApiBase ?? "";
export const API_PREFIX = LOCAL_API_PREFIX;

export function resetWorkspaceRevisionState(): void {
  workspaceRevisionState.reset();
}

function currentApiUrl(path: string): string {
  return `${API_BASE}${API_PREFIX}${path.startsWith("/") ? path : `/${path}`}`;
}

export async function bootstrapBrowserSession(): Promise<void> {
  const url = new URL(window.location.href);
  const bootstrap = url.searchParams.get("bootstrap");
  if (bootstrap) {
    url.searchParams.delete("bootstrap");
    window.history.replaceState({}, "", `${url.pathname}${url.search}${url.hash}`);
  }
  if (!bootstrap) return;
  const response = await fetch(currentApiUrl("/session/bootstrap"), {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token: bootstrap }),
  });
  if (!response.ok) {
    throw new Error(
      "本地启动令牌无效，请重新启动 RotoWeave 客户端。",
    );
  }
}

export function mediaUrl(path?: string): string {
  if (!path) return "";
  return /^https?:\/\//.test(path)
    ? path
    : `${API_BASE}${path.startsWith("/") ? path : `/${path}`}`;
}

export class ApiRequestError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly code?: string,
    readonly detail?: Record<string, unknown>,
  ) {
    super(message);
    this.name = "ApiRequestError";
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const observation = workspaceRevisionState.beginObservation();
  const method = (init?.method ?? "GET").toUpperCase();
  const revisionId = ["GET", "HEAD", "OPTIONS"].includes(method)
    ? null
    : workspaceRevisionState.revisionForMutation(path);
  const response = await fetch(currentApiUrl(path), {
    ...init,
    credentials: "include",
    headers: {
      ...(init?.body instanceof FormData ? {} : { "Content-Type": "application/json" }),
      ...(revisionId ? { "X-RotoWeave-Revision-Id": revisionId } : {}),
      ...init?.headers,
    },
  });
  if (!response.ok) {
    let message = `请求失败 (${response.status})`;
    let code: string | undefined;
    let detailRecord: Record<string, unknown> | undefined;
    try {
      const payload = (await response.json()) as {
        code?: string;
        detail?: string | Record<string, unknown>;
      };
      code = payload.code;
      if (typeof payload.detail === "string") message = payload.detail;
      else if (payload.detail) {
        detailRecord = payload.detail;
        if (typeof payload.detail.message === "string") {
          message = payload.detail.message;
        }
        if (typeof payload.detail.code === "string") {
          code = payload.detail.code;
        }
      }
    } catch {
      // Keep the HTTP fallback message.
    }
    throw new ApiRequestError(
      message,
      response.status,
      code,
      detailRecord,
    );
  }
  const nextRevisionId = response.headers.get("x-rotoweave-revision-id");
  if (response.status === 204) {
    if (nextRevisionId) {
      workspaceRevisionState.applyMutationRevision(path, nextRevisionId, observation);
    }
    return undefined as T;
  }
  const payload = (await response.json()) as T;
  workspaceRevisionState.remember(payload, observation);
  if (nextRevisionId) {
    workspaceRevisionState.applyMutationRevision(path, nextRevisionId, observation);
  }
  return payload;
}

export const api = {
  health: () => request<Health>("/health"),
  workspace: () => request<WorkspaceState>("/workspace"),
  chooseWorkspaceFolder: () => request<{ root: string | null }>("/workspace/dialog", { method: "POST" }),
  createWorkspace: (root: string, name: string) =>
    request<WorkspaceState>("/workspace/create", {
      method: "POST",
      body: JSON.stringify({ root, name }),
    }),
  openWorkspace: (root: string) =>
    request<WorkspaceState>("/workspace/open", {
      method: "POST",
      body: JSON.stringify({ root }),
    }),
  inspectWorkspaceBrand: (root: string) =>
    request<{ state: string; migratable: boolean; name: string | null }>("/workspace/brand-migration/inspect", {
      method: "POST",
      body: JSON.stringify({ root }),
    }),
  migrateWorkspaceBrand: (root: string) =>
    request<{ state: string; manifest: string; legacyBackup: string; domainBackup: string }>("/workspace/brand-migration", {
      method: "POST",
      body: JSON.stringify({ root }),
    }),
  validateWorkspace: (fullHash = true) =>
    request<Record<string, unknown>>("/workspace/validate", {
      method: "POST",
      body: JSON.stringify({ full_hash: fullHash }),
    }),
  reloadWorkspace: () =>
    request<WorkspaceState>("/workspace/reload", {
      method: "POST",
    }),
  prepareAndCloseWorkspace: () =>
    request<WorkspaceState & { prepared?: Record<string, unknown> }>("/workspace/prepare-and-close", { method: "POST" }),
  closeWorkspace: () => request<WorkspaceState>("/workspace/close", { method: "POST" }),
  revealWorkspace: () => request<{ opened: boolean }>("/workspace/reveal", { method: "POST" }),
  sizeSystem: () => request<SizeSystem>("/size-system"),
  remoteServiceSettings: () => request<RemoteServiceSettings>("/remote-service/settings"),
  saveRemoteServiceSettings: (payload: {
    enabled: boolean;
    host: string;
    port: number;
  }) => {
    const form = new FormData();
    form.append("enabled", String(payload.enabled));
    form.append("host", payload.host);
    form.append("port", String(payload.port));
    return request<RemoteServiceSettings>("/remote-service/settings", { method: "PUT", body: form });
  },
  testRemoteService: () => request<RemoteServiceConnectionTest>("/remote-service/test", { method: "POST" }),
  deploymentBundlePlan: () => request<DeploymentBundlePlan>("/deployment-bundles/plan"),
  chooseDeploymentBundleDirectory: () => request<{ selectionToken: string | null; displayPath: string | null }>("/deployment-bundles/output-directory-dialog", { method: "POST" }),
  startDeploymentBundleExport: (selectionToken: string) => request<DeploymentBundleExport>("/deployment-bundles/exports", { method: "POST", body: JSON.stringify({ selectionToken }) }),
  deploymentBundleExport: (id: string) => request<DeploymentBundleExport>(`/deployment-bundles/exports/${id}`),
  cancelDeploymentBundleExport: (id: string) => request<DeploymentBundleExport>(`/deployment-bundles/exports/${id}`, { method: "DELETE" }),
  revealDeploymentBundleExport: (id: string) => request<{ opened: boolean }>(`/deployment-bundles/exports/${id}/reveal`, { method: "POST" }),
  createSizeProfile: (payload: { name: string; width_world: number; height_world: number; unit_mode?: "pixels" | "unity" }) =>
    request<SizeProfile>("/size-profiles", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  updateSizeProfile: (id: string, payload: { name?: string; width_world?: number; height_world?: number; unit_mode?: "pixels" | "unity" }) =>
    request<SizeProfile>(`/size-profiles/${id}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    }),
  deleteSizeProfile: (id: string) =>
    request<void>(`/size-profiles/${id}`, { method: "DELETE" }),
  jobs: (characterId?: string) =>
    request<Job[]>(`/jobs${characterId ? `?character_id=${encodeURIComponent(characterId)}` : ""}`),
  cancelJob: (jobId: string) => request<Job>(`/jobs/${jobId}/cancel`, { method: "POST" }),
  domainV4: () => request<WorkspaceDomainV4>("/domain"),
  createDomainCharacter: (name: string, expectedRevisionId: string) =>
    request<{ character: WorkspaceDomainV4["characters"][number]; revisionId: string }>("/domain/characters", {
      method: "POST",
      body: JSON.stringify({ name, expectedRevisionId }),
    }),
  updateDomainCharacter: (characterId: string, name: string, expectedRevisionId: string) =>
    request<{ character: WorkspaceDomainV4["characters"][number]; revisionId: string }>(`/domain/characters/${characterId}`, {
      method: "PATCH",
      body: JSON.stringify({ name, expectedRevisionId }),
    }),
  deleteDomainCharacter: (characterId: string, expectedRevisionId: string) =>
    request<{ revisionId: string; domain: WorkspaceDomainV4 }>(
      `/domain/characters/${characterId}?expected_revision_id=${encodeURIComponent(expectedRevisionId)}&explicit=true`,
      { method: "DELETE" },
    ),
  revealDomainCharacter: (characterId: string) =>
    request<{ opened: boolean; characterId: string }>(`/domain/characters/${characterId}/reveal`, {
      method: "POST",
    }),
  createMaterialImportJob: (
    characterId: string,
    files: File[],
    expectedRevisionId: string,
    targetFps = 24,
    onUploadProgress?: (progress: number) => void,
    signal?: AbortSignal,
  ) => new Promise<Job>((resolve, reject) => {
    const path = `/domain/characters/${characterId}/materials/import-jobs`;
    const xhr = new XMLHttpRequest();
    const form = new FormData();
    for (const file of files) form.append("files", file, file.name);
    form.append("target_fps", String(targetFps));
    form.append("expected_revision_id", expectedRevisionId);
    xhr.open("POST", currentApiUrl(path));
    xhr.withCredentials = true;
    xhr.setRequestHeader("X-RotoWeave-Revision-Id", expectedRevisionId);
    xhr.upload.addEventListener("progress", (event) => {
      if (event.lengthComputable) onUploadProgress?.(event.loaded / event.total);
    });
    xhr.addEventListener("load", () => {
      let payload: unknown;
      try { payload = JSON.parse(xhr.responseText); } catch { payload = null; }
      if (xhr.status >= 200 && xhr.status < 300 && payload) {
        resolve(payload as Job);
        return;
      }
      const detail = (payload as { detail?: string | { message?: string } } | null)?.detail;
      const message = typeof detail === "string" ? detail : detail?.message ?? `请求失败 (${xhr.status})`;
      reject(new ApiRequestError(message, xhr.status));
    });
    xhr.addEventListener("error", () => reject(new ApiRequestError("素材上传连接失败。", 0)));
    xhr.addEventListener("abort", () => reject(new DOMException("素材上传已取消。", "AbortError")));
    const abort = () => xhr.abort();
    signal?.addEventListener("abort", abort, { once: true });
    xhr.addEventListener("loadend", () => {
      signal?.removeEventListener("abort", abort);
    });
    xhr.send(form);
  }),
  importMaterial: (
    characterId: string,
    file: File,
    expectedRevisionId: string,
    targetFps = 24,
  ) => {
    const form = new FormData();
    form.append("file", file);
    form.append("display_name", file.name.replace(/\.[^.]+$/, ""));
    form.append("target_fps", String(targetFps));
    form.append("expected_revision_id", expectedRevisionId);
    return request<{ revisionId: string; domain: WorkspaceDomainV4 }>(
      `/domain/characters/${characterId}/materials/import`,
      { method: "POST", body: form },
    );
  },
  syncMaterials: (
    characterId: string,
    files: File[],
    expectedRevisionId: string,
    targetFps = 24,
  ) => {
    const form = new FormData();
    for (const file of files) form.append("files", file, file.name);
    form.append("target_fps", String(targetFps));
    form.append("expected_revision_id", expectedRevisionId);
    return request<{ revisionId: string; domain: WorkspaceDomainV4 }>(
      `/domain/characters/${characterId}/materials/sync`,
      { method: "POST", body: form },
    );
  },
  deleteMaterialSource: (sourceId: string, expectedRevisionId: string) =>
    request<{ revisionId: string; domain: WorkspaceDomainV4 }>(
      `/material-sources/${sourceId}?expected_revision_id=${encodeURIComponent(expectedRevisionId)}&explicit=true`,
      { method: "DELETE" },
    ),
  createMaterialBasicJob: (
    sourceId: string,
    expectedRevisionId: string,
    settings: Record<string, unknown>,
    frameIndexes: number[],
  ) => request<Job>(`/material-sources/${sourceId}/basic-jobs`, {
    method: "POST",
    body: JSON.stringify({ expectedRevisionId, frameIndexes: [...frameIndexes].sort((a, b) => a - b), settings }),
  }),
  createMaterialRemoteJob: (
    sourceId: string,
    expectedRevisionId: string,
    quality: "high" | "ultra",
    settings: Record<string, unknown>,
    frameIndexes: number[],
  ) => request<Job>(`/material-sources/${sourceId}/remote-jobs`, {
    method: "POST",
    body: JSON.stringify({ expectedRevisionId, frameIndexes: [...frameIndexes].sort((a, b) => a - b), quality, settings }),
  }),
  exportMaterialPhotoshopSheet: (
    sourceId: string,
    variantId: string | null,
    frameIndexes: number[],
    batchSize: number,
  ) =>
    request<PhotoshopSheetV4>(`/material-sources/${sourceId}/photoshop-sheet/export`, {
      method: "POST",
      body: JSON.stringify({ variantId: variantId ?? null, frameIndexes, batchSize }),
    }),
  importMaterialPhotoshopSheet: (
    sourceId: string,
    files: File[],
    expectedRevisionId: string,
  ) => {
    const form = new FormData();
    for (const file of files) form.append("files", file, file.name);
    form.append("expected_revision_id", expectedRevisionId);
    return request<{ revisionId: string; domain: WorkspaceDomainV4; variant: WorkspaceDomainV4["materialVariants"][number] }>(
      `/material-sources/${sourceId}/photoshop-sheet/import`,
      { method: "POST", body: form },
    );
  },
  materialSourceVideoUrl: (sourceId: string) => currentApiUrl(`/material-sources/${sourceId}/video`),
  materialSourceFrameUrl: (sourceId: string, frameIndex: number) =>
    currentApiUrl(`/material-sources/${sourceId}/frames/${frameIndex}`),
  createDomainAction: (characterId: string, name: string, expectedRevisionId: string) =>
    request<{ action: DomainActionV4; revisionId: string }>(`/domain/characters/${characterId}/actions`, {
      method: "POST",
      body: JSON.stringify({ name, expectedRevisionId }),
    }),
  updateDomainAction: (
    actionId: string,
    changes: { name?: string; previewLoop?: boolean; loop?: boolean },
    expectedRevisionId: string,
  ) => request<{ action: DomainActionV4; revisionId: string }>(`/domain/actions/${actionId}`, {
    method: "PATCH",
    body: JSON.stringify({ ...changes, expectedRevisionId }),
  }),
  deleteDomainAction: (actionId: string, expectedRevisionId: string) =>
    request<{ removed: DomainActionV4; revisionId: string }>(`/domain/actions/${actionId}?expectedRevisionId=${encodeURIComponent(expectedRevisionId)}`, {
      method: "DELETE",
    }),
  appendDomainActionFrames: (actionId: string, frames: ActionFrameDraftV4[], expectedRevisionId: string) =>
    request<{ action: DomainActionV4; revisionId: string }>(`/domain/actions/${actionId}/frames`, {
      method: "POST",
      body: JSON.stringify({ frames, expectedRevisionId }),
    }),
  saveDomainActionFrames: (actionId: string, frames: ActionFrameDraftV4[], expectedRevisionId: string) =>
    request<{ action: DomainActionV4; revisionId: string }>(`/domain/actions/${actionId}/frames`, {
      method: "PUT",
      body: JSON.stringify({ frames, expectedRevisionId }),
    }),
  resetDomainAction: (actionId: string) =>
    request<{ action: DomainActionV4; revisionId: string; reset: boolean }>(`/domain/actions/${actionId}/reset`, { method: "POST" }),
  updateDomainCharacterSettings: (characterId: string, changes: Partial<Pick<WorkspaceDomainV4["characters"][number], "calibration" | "shadow" | "delivery">>, expectedRevisionId: string) =>
    request<{ character: WorkspaceDomainV4["characters"][number]; revisionId: string }>(`/domain/characters/${characterId}/settings`, {
      method: "PATCH",
      body: JSON.stringify({ ...changes, expectedRevisionId }),
    }),
  uploadDomainCoreReference: (characterId: string, file: File, expectedRevisionId: string) => {
    const form = new FormData(); form.append("file", file); form.append("expected_revision_id", expectedRevisionId);
    return request<{ character: WorkspaceDomainV4["characters"][number]; revisionId: string }>(`/domain/characters/${characterId}/core-reference`, { method: "POST", body: form });
  },
  deleteDomainCoreReference: (characterId: string, expectedRevisionId: string) => request<{ character: WorkspaceDomainV4["characters"][number]; revisionId: string }>(`/domain/characters/${characterId}/core-reference?expected_revision_id=${encodeURIComponent(expectedRevisionId)}`, { method: "DELETE" }),
  domainCoreReferenceUrl: (characterId: string, contentVersion?: string) => currentApiUrl(`/domain/characters/${characterId}/core-reference${contentVersion ? `?v=${encodeURIComponent(contentVersion)}` : ""}`),
  previewDomainShadow: (characterId: string, payload: {
    frameRefs?: ActionFrameDraftV4[];
    loop?: boolean;
    useCoreReference?: boolean;
    shadowStandardY?: number;
    shadow?: WorkspaceDomainV4["characters"][number]["shadow"];
  }) => request<{ frames: ShadowPreview[] }>(`/domain/characters/${characterId}/shadow-preview`, {
    method: "POST",
    body: JSON.stringify(payload),
  }),
  materialVariantFrameUrl: (variantId: string, frameIndex: number, layer: "rgba" | "emission" = "rgba") =>
    currentApiUrl(`/material-variants/${variantId}/frames/${frameIndex}?layer=${layer}`),
  materialSourceThumbnailUrl: (sourceId: string, frameIndex: number) =>
    currentApiUrl(`/material-sources/${sourceId}/frames/${frameIndex}/thumbnail`),
  exportDomainCharacter: (characterId: string, expectedRevisionId: string, atlasMaxSize: number) =>
    request<{ characterId: string; archivePath: string; sha256: string; bytes: number; exportState: WorkspaceDomainV4["characters"][number]["exportState"] }>(
      `/domain/characters/${characterId}/export`,
      { method: "POST", body: JSON.stringify({ expectedRevisionId, atlasMaxSize }) },
    ),
  estimateDomainCharacterExport: (characterId: string, expectedRevisionId: string, atlasMaxSize: number) => request<{ referencedFrames: number; uniqueSprites: number; maximumOutput: { width: number; height: number }; pageCount: number; pages: Array<{ index: number; width: number; height: number }>; rgbaBytes: number; estimatedPngBytes: number; packingRatio: number }>(`/domain/characters/${characterId}/export/estimate?expected_revision_id=${encodeURIComponent(expectedRevisionId)}&atlas_max_size=${atlasMaxSize}`),
  domainCharacterAtlasPageUrl: (characterId: string, pageIndex: number) => currentApiUrl(`/domain/characters/${characterId}/export/pages/${pageIndex}`),
  repairDomainCharacterAtlasPage: (characterId: string, pageIndex: number, file: File, expectedRevisionId: string) => {
    const form = new FormData(); form.append("file", file); form.append("expected_revision_id", expectedRevisionId);
    return request<{ revisionId: string; sha256: string; exportState: WorkspaceDomainV4["characters"][number]["exportState"] }>(`/domain/characters/${characterId}/export/pages/${pageIndex}/repair`, { method: "POST", body: form });
  },
  domainCharacterExportDownloadUrl: (characterId: string) =>
    currentApiUrl(`/domain/characters/${characterId}/export/download`),
};

export function subscribeToJobs(
  characterId: string | undefined,
  onJobs: (jobs: Job[]) => void,
  onConnectionChange?: (connected: boolean) => void,
): () => void {
  const query = characterId ? `?character_id=${encodeURIComponent(characterId)}` : "";
  const stream = new EventSource(currentApiUrl(`/jobs/events${query}`), {
    withCredentials: true,
  });
  stream.addEventListener("open", () => onConnectionChange?.(true));
  stream.addEventListener("error", () => onConnectionChange?.(false));
  stream.addEventListener("jobs", (event) => {
    onJobs(JSON.parse((event as MessageEvent<string>).data) as Job[]);
  });
  return () => stream.close();
}
