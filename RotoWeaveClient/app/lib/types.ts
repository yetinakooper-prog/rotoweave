export type JobStatus = "queued" | "running" | "cancelling" | "cancelled" | "failed" | "completed";

export type DomainAssetV4 = {
  id: string;
  index?: number;
  path: string;
  sha256: string;
  bytes: number;
};

export type ActionFrameTransformV4 = {
  position: { x: number; y: number };
  scale: { x: number; y: number };
  rotationDegrees: number;
  color: string;
  opacity: number;
  shadow: {
    enabled: boolean | null;
    color: string | null;
    opacity: number | null;
    offset: { x: number; y: number };
    scale: { x: number; y: number };
  };
};

export type ActionFrameRefV4 = {
  id: string;
  variantId: string;
  frameId: string;
  durationSeconds: number;
  enabled: boolean;
  transform: ActionFrameTransformV4;
};

export type ActionFrameDraftV4 = Omit<ActionFrameRefV4, "id"> & { id?: string };

export type DomainActionV4 = {
  id: string;
  characterId: string;
  name: string;
  previewLoop?: boolean;
  loop?: boolean;
  frameRefs: ActionFrameRefV4[];
};

export type MaterialVariantV4 = {
  id: string;
  sourceId: string;
  kind: "basic" | "high" | "ultra" | "photoshop";
  settings: Record<string, unknown>;
  settingsSha256: string;
  frames: Array<DomainAssetV4 & { sourceFrameId: string; emission?: DomainAssetV4 }>;
  migration?: { actionFrameCount: number; actionIds: string[] };
};

export type MaterialSourceV4 = {
  id: string;
  characterId: string;
  displayName: string;
  video: DomainAssetV4;
  frames: Array<DomainAssetV4 & { ptsUs: number; durationUs: number; width: number; height: number }>;
  variantIds: string[];
};

export type DomainCharacterV4 = {
  id: string;
  name: string;
  actionIds: string[];
  materialSourceIds: string[];
  calibration: {
    sizeProfiles: Array<{ id: string; name: string; presetId: string | null; unitMode: "pixels" | "unity"; width: number; height: number }>;
    activeSizeProfileId: string;
    pixelsPerUnit: number;
    sizeGuideCenterX: number;
    sizeGuideBottomY: number;
    alignmentHorizonY: number;
    shadowStandardY: number;
    coreReference: (DomainAssetV4 & { width: number; height: number; scale: number; origin: { x: number; y: number } }) | null;
  };
  shadow: { enabled: boolean; color: string; baseOpacity: number; lightAngleDegrees: number };
  delivery: {
    defaultActionId: string | null;
    globalTextureScale: number;
    actionSettings: Record<string, { textureScale: number; runtimeLoop: boolean; includeInExport: boolean }>;
    atlas: { maxSize: 2048 | 4096 | 8192; padding: number; extrude: number; framePadding: number };
  };
  exportState: {
    status: "not-exported" | "stale" | "current" | "failed";
    currentAtlas: DomainAssetV4 | null;
  };
};

export type WorkspaceDomainV4 = {
  domainSchemaRevision?: 7;
  revisionId: string;
  characters: DomainCharacterV4[];
  actions: DomainActionV4[];
  materialSources: MaterialSourceV4[];
  materialVariants: MaterialVariantV4[];
};

export type PhotoshopSheetV4 = {
  schemaVersion: 2;
  sheetId: string;
  sourceId: string;
  sourceSha256: string;
  baseVariantId?: string | null;
  sourceFrameCount: number;
  selectedFrameCount: number;
  batchSize: number;
  batchCount: number;
  mappingSha256: string;
  sheets: Array<{
    batchIndex: number;
    width: number;
    height: number;
    frameCount: number;
    downloadUrl: string;
  }>;
};

export type JobFailure = {
  code: string;
  title: string;
  message: string;
  guidance: string;
  failedStage: string;
  retryable: boolean;
  targetStep?: "range" | "matte" | "anchor" | "atlas" | "complete" | null;
  details: string;
  constraintReview?: {
    failedFrameCount: number;
    failedFrames: Array<{
      frameId: string;
      frameIndex: number;
      qc: Record<string, unknown>;
      warnings: string[];
    }>;
  };
  frameIndex?: number;
};

export type Job = {
  id: string;
  project_id: string;
  character_id?: string | null;
  source_id?: string | null;
  animation_id?: string | null;
  animation_name?: string | null;
  character_name?: string | null;
  type: string;
  status: JobStatus;
  stage: string;
  progress: number;
  error?: string | null;
  result?: Record<string, unknown> & {
    failure?: JobFailure;
    matte?: {
      publishedCandidate?: string;
      manualReview?: {
        accepted: boolean;
        candidateId: "production" | "hybrid";
        acceptedAt: string;
        sourceJobId: string;
      };
      [key: string]: unknown;
    };
  };
  logs?: Array<{ time: string; level: string; message: string }>;
  created_at: string;
  updated_at: string;
};

export type SizeProfile = {
  id: string;
  revisionId: string;
  name: string;
  width_world: number;
  height_world: number;
  unit_mode: "pixels" | "unity";
  created_at: string;
  updated_at: string;
  referenced_character_count?: number;
};

export type SizeSystem = {
  canonicalPixelsPerUnit: number;
  revisionId: string;
  profiles: SizeProfile[];
};

export type RemoteServiceSettings = {
  enabled: boolean;
  endpoint: string;
  host: string;
  port: number;
};

export type RemoteServiceConnectionTest = {
  connected: true;
  protocolVersion: number;
  service: string;
  ready: boolean;
  startupState: "starting" | "ready" | "failed";
  workerState: string;
  ownership: "short-lived-remote-jobs-only";
};

export type CoreReference = {
  url: string;
  width: number;
  height: number;
  origin_x: number;
  origin_y: number;
  scale: number;
  revision: number;
};

export type ShadowSettings = {
  enabled: boolean;
  color: string;
  opacity: number;
  light_angle_degrees: number;
  standard_y: number | null;
};

export type ShadowPreview = {
  positionPx: [number, number];
  widthPx: number;
  depthPx: number;
  rotationDegrees: number;
  alpha: number;
  airborneRatio: number;
};

export type WorkspaceState = {
  state: "Closed" | "Opening" | "Open" | "Closing";
  epoch: number;
  workspaceId: string | null;
  name: string | null;
  root?: string | null;
  readOnly: boolean;
  validation: "not_checked" | "valid" | "changed";
  conflict: string | null;
  canMutate: boolean;
  canManageWorkspace: boolean;
  recent?: Array<{
    root: string;
    name: string;
    workspaceId?: string | null;
    available: boolean;
  }>;
};

export type Health = {
  status: string;
  version: string;
  apiVersion: number;
  localOnly: true;
  processing: {
    basic: { available: boolean; mode?: string | null };
    high: { owner: "server" };
    ultra: { owner: "server" };
  };
};

export type DeploymentBundlePlan = {
  schemaVersion: number;
  role: "client" | "server";
  platform: string;
  productVersion: string;
  compatibilityDigest: string;
  sourceRevision: string;
  ready: boolean;
  estimatedBytes: number;
  diskFreeBytes: number;
  sourceStatus: "bundle-configured" | "component-only" | "missing";
  requiresNetworkForCache: boolean;
  pageExportEnabled: boolean;
  components: Array<{ id: string; status: "ready" | "downloadable" | "missing" | "invalid"; detail: string; bytes: number; relativePath: string }>;
  environments: Array<{ id: string; ready: boolean }>;
};

export type DeploymentBundleExport = {
  id: string;
  role: "client" | "server";
  state: "queued" | "running" | "completed" | "failed" | "cancelled";
  stage: string;
  progress: number;
  message: string;
  cancelRequested: boolean;
  outputDirectory: string;
  outputPath?: string | null;
  sha256?: string | null;
  bytes?: number | null;
  error?: string | null;
  createdAt: number;
  updatedAt: number;
};
