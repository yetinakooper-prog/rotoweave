export type Page = "overview" | "models" | "queue" | "logs" | "deployment";
export type Json = Record<string, unknown>;

export type HardwareWarning = {
  code: string;
  severity: "warning";
  scope: string;
  message: string;
  action: string;
  profile?: "high" | "ultra";
};

export type CudaDevice = {
  index: number;
  uuid: string;
  gpuName: string;
  driverVersion: string;
  computeCapability?: string;
  vramTotalMiB: number;
  vramUsedMiB: number;
  vramFreeMiB: number;
};

export type HardwareStatus = {
  available: boolean;
  compatibilityState: string;
  selectedDevice?: CudaDevice;
  devices: CudaDevice[];
  cudaSmokePassed?: boolean;
  warnings: HardwareWarning[];
};

export type DeploymentBundlePlan = {
  schemaVersion: number; role: "server"; platform: string; productVersion: string;
  compatibilityDigest: string; sourceRevision: string; ready: boolean; estimatedBytes: number; diskFreeBytes: number;
  sourceStatus: "bundle-configured" | "component-only" | "missing";
  requiresNetworkForCache: boolean; pageExportEnabled: boolean;
  components: Array<{ id: string; status: "ready" | "downloadable" | "missing" | "invalid"; detail: string; bytes: number; relativePath: string }>;
  environments: Array<{ id: string; ready: boolean }>;
};

export type DeploymentBundleExport = {
  id: string; role: "server"; state: "queued" | "running" | "completed" | "failed" | "cancelled";
  stage: string; progress: number; message: string; cancelRequested: boolean; outputDirectory: string;
  outputPath?: string | null; sha256?: string | null; bytes?: number | null; error?: string | null;
  createdAt: number; updatedAt: number;
};

export type NetworkSettings = {
  serviceHost: string;
  serviceEndpoint: string;
  apiHost: string;
  apiPort: number;
  endpoint: string;
  apiPath: string;
  scope: "loopback" | "trusted-lan";
  loopbackOnly: boolean;
  adminHost: string;
  adminPort: number;
  adminEndpoint: string;
  configuredHost: string;
  configuredPort: number;
  configuredEndpoint: string;
  restartRequired: boolean;
  configurationError?: string;
  addressError?: string;
};

export type ProfileState = {
  profile: "high" | "ultra";
  state: string;
  blockers: string[];
  runtime: { id: string; digest: string; python: string; installed: boolean; source: string };
  receiptDigest?: string;
  profileConfigurationDigest?: string;
  qualification: "official" | "local-compatible";
  localCompatibleRoles: string[];
  executionModes?: Array<{
    mode: "full" | "balanced" | "constrained" | "minimal";
    state: string;
    peakReservedMiB?: number;
    peakAllocatedMiB?: number;
    peakWorkingSetMiB?: number;
    cpuStages?: string[];
  }>;
};

export type ModelRoot = {
  id: string;
  label: string;
  path: string;
  priority: number;
  enabled: boolean;
  readOnly: boolean;
};

export type ModelAsset = {
  id: string;
  rootId: string;
  role: string;
  modelId: string;
  path: string;
  bytes: number;
  sha256: string;
  state: string;
  verificationKind?: "official" | "structural";
  verificationContractDigest?: string;
  verificationReceiptDigest?: string;
  error?: string;
};

export type RecipeSlot = {
  role: string;
  modelId: string;
  displayName: string;
  filename: string;
  bytes: number;
  sha256: string;
  revision: string;
  sourceUrl: string;
  licenseId: string;
  profiles: string[];
  runtimeContract: string;
  state: string;
  binding?: ModelAsset;
  error?: string;
};

export type ModelOperation = {
  id: string;
  kind: string;
  state: string;
  stage: string;
  progress: number;
  cancelRequested: boolean;
  detail: Json;
  error?: string;
  createdAt: string;
  updatedAt: string;
};

export type ModelCenter = {
  recipe: { id: string; digest: string; displayName: string; qualityComparisonRequired: boolean };
  compatibilityPolicy: { id: string; schemaVersion: number; digest: string };
  slots: RecipeSlot[];
  profiles: Record<"high" | "ultra", ProfileState>;
  draftConfigurationDigest?: string;
  activeConfiguration?: {
    configurationDigest: string;
    recipeId: string;
    profileConfigurationDigests?: Record<string, string>;
    profileExecutionReceipts?: Record<string, {
      qualification?: "official" | "local-compatible";
      localCompatibleRoles?: string[];
    }>;
  };
  operations: ModelOperation[];
};

export type QueueControl = {
  paused: boolean;
  maintenance: boolean;
  mode: string;
  revision: number;
};

export type Job = {
  id: string;
  state: string;
  progress: number;
  stage?: string;
  queueOrder: number;
  parentJobId?: string;
  modelConfigurationDigest?: string;
  qualityProfile?: "high" | "ultra";
  createdAt: string;
  updatedAt: string;
  error?: { message?: string };
  submission?: Json;
};

export type LogItem = {
  id: number;
  createdAt: string;
  level: string;
  component: string;
  event: string;
  jobId?: string;
  detail: Json;
};
