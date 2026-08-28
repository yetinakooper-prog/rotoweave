import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";
import { fileURLToPath, URL } from "node:url";

import productContract from "@rotoweave/contracts/product.json" with { type: "json" };

const apiPort = productContract.runtime.apiPort;
const webDevelopmentPort = productContract.runtime.webDevelopmentPort;
const developmentApiTarget = process.env.ROTOWEAVE_DEV_API_TARGET?.trim()
  || `http://127.0.0.1:${apiPort}`;
const developmentCacheDirectory = process.env.ROTOWEAVE_VITE_CACHE_DIR?.trim()
  || "node_modules/.vite";

export default defineConfig({
  cacheDir: developmentCacheDirectory,
  plugins: [
    react(),
  ],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL(".", import.meta.url)),
    },
    dedupe: ["react", "react-dom", "konva", "react-konva"],
  },
  server: {
    host: "127.0.0.1",
    port: webDevelopmentPort,
    strictPort: true,
    proxy: {
      "/api": {
        target: developmentApiTarget,
        changeOrigin: false,
      },
    },
    watch: {
      ignored: [
        "**/backend/tests/**",
        "**/dist/**",
        "**/release/**",
        "**/node_modules/**",
        "**/models/**",
        "**/artifacts/**",
        "**/audits/**",
        "**/Temp/**",
        "**/tmp/**",
        "**/.svn/**",
        "**/.git/**",
        "**/.pytest_cache/**",
        "**/.data-dev/**",
      ],
    },
  },
  build: {
    outDir: "runtime/frontend",
    emptyOutDir: true,
    sourcemap: false,
  },
  optimizeDeps: {
    include: ["react", "react-dom", "konva", "react-konva"],
  },
});
