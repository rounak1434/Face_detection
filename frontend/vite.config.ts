/// <reference types="vitest" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Vite runs this file in Node; declared here to avoid pulling in @types/node.
declare const process: { env: Record<string, string | undefined> };

// In development the API runs on :8000; Vite forwards /api (including the
// live-view websocket) so the browser sees a single origin, as in production.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      // Override with API_TARGET=http://127.0.0.1:8001 when port 8000 is busy.
      "/api": { target: process.env.API_TARGET ?? "http://127.0.0.1:8000", ws: true },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
  },
});
