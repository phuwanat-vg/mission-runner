import { defineConfig } from "vite";

// The runner (mission_runner) serves the built bundle from mission_runner/webui at GET /.
// During development the API is proxied to a runner on localhost:8080
// (override with MISSION_API=http://host:port when another runner is used).
const RUNNER = process.env.MISSION_API ?? "http://localhost:8080";

export default defineConfig({
  base: "./",
  clearScreen: false,
  server: {
    port: 5173,
    strictPort: false,
    // model/templates.ts imports ../examples/*.json, which lives outside the Vite root.
    fs: { allow: [".."] },
    proxy: {
      "/api/events": { target: RUNNER, ws: true, changeOrigin: true },
      "/api": { target: RUNNER, changeOrigin: true },
      "/hooks": { target: RUNNER, changeOrigin: true },
    },
  },
  build: {
    outDir: "../runner/mission_runner/webui",
    emptyOutDir: true,
    target: ["es2022", "chrome110"],
    sourcemap: false,
  },
  test: {
    environment: "node",
    include: ["test/**/*.test.ts"],
  },
});
