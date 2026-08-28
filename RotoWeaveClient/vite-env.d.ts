/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_ROTOWEAVE_API?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
