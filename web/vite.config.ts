/// <reference types="vitest/config" />
import { fileURLToPath } from "node:url";
import { defineConfig, type ProxyOptions } from "vite";
import react from "@vitejs/plugin-react";

// Two personalities, one config (ADR 0030) — the same arrangement as booth-catalog/booth-storage:
//   - `vite`/`vite dev` (command "serve"): the dev harness — index.html/src/main.tsx wraps
//     PipelineApp in a mock shell (src/devshell/DevShell.tsx), since booth-design's real shell
//     doesn't run here.
//   - `vite build` (command "build"): builds the publishable library (@projectbooth/pipeline-ui)
//     from src/index.ts, external-izing react/react-dom so booth-design's own copies are used (two
//     React copies in one page is a classic cause of "Invalid hook call"). @xyflow/react is NOT
//     external: booth-design does not provide it, so it ships inside this package.
//
// What booth-core's gateway does besides stripping the /modules/{id} prefix: it validates the
// browser's X-Workspace header and forwards it to the module as X-Booth-Workspace (ADR 0025). The
// module reads only the forwarded header, so the dev proxy has to do the same translation or no
// request from the harness could ever be authorized.
const gatewayHeaders: NonNullable<ProxyOptions["configure"]> = (proxy) => {
  proxy.on("proxyReq", (proxyReq, req) => {
    const ws = req.headers["x-workspace"];
    if (typeof ws === "string" && ws !== "") proxyReq.setHeader("X-Booth-Workspace", ws);
  });
};

export default defineConfig(({ command }) => ({
  plugins: [react()],
  build:
    command === "build"
      ? {
          lib: {
            entry: fileURLToPath(new URL("./src/index.ts", import.meta.url)),
            formats: ["es"],
            fileName: "index",
          },
          rollupOptions: {
            external: ["react", "react-dom", "react/jsx-runtime"],
          },
        }
      : undefined,
  server: {
    proxy: {
      // Local dev only: booth-core's gateway would normally proxy /modules/pipeline/* to this
      // service's own /api/* routes (and /modules/catalog/* to booth-catalog, which the code
      // picker calls). Point BOOTH_PIPELINE_DEV_BACKEND / BOOTH_CATALOG_DEV_BACKEND at locally
      // running backends so `npm run dev` works without CORS juggling.
      "/modules/pipeline": {
        target: process.env.BOOTH_PIPELINE_DEV_BACKEND ?? "http://localhost:8090",
        changeOrigin: true,
        // Mimics the gateway's prefix-stripping: /modules/pipeline/api/pipelines reaches the
        // backend as /api/pipelines.
        rewrite: (path) => path.replace(/^\/modules\/pipeline/, ""),
        configure: gatewayHeaders,
      },
      "/modules/catalog": {
        target: process.env.BOOTH_CATALOG_DEV_BACKEND ?? "http://localhost:8080",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/modules\/catalog/, ""),
        configure: gatewayHeaders,
      },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/setupTests.ts"],
  },
}));
