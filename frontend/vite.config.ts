/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  // vitest: src 配下の *.test.ts(x) のみ。globals は使わず vitest から明示 import する
  // (グローバル名前空間を増やさない / tsconfig の types を触らない)。
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.{ts,tsx}"],
  },
  base: "/app/",
  build: {
    outDir: "dist",
    sourcemap: false,
    chunkSizeWarningLimit: 1200,
  },
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8001",
      "/ws": { target: "ws://127.0.0.1:8001", ws: true },
    },
  },
});
