import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  cacheDir: "node_modules/.vite",
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 8445,
    strictPort: true,
    proxy: { "/api": { target: "http://127.0.0.1:8444", changeOrigin: false } },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    sourcemap: false,
  },
});
