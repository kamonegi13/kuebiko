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
  // 公開サイトの静的ビルド (VITE_PUBLIC_STATIC=1) は Cloudflare Pages のルート配下に
  // 置く。運用者の PC で配信する通常ビルドは従来どおり /app/ 配下。
  base: process.env.VITE_PUBLIC_STATIC === "1" ? "/" : "/app/",
  build: {
    outDir: process.env.VITE_PUBLIC_STATIC === "1" ? "dist-public" : "dist",
    sourcemap: false,
    chunkSizeWarningLimit: 1200,
    rollupOptions:
      process.env.VITE_PUBLIC_STATIC === "1"
        ? { input: { public: "public.html" } }
        : undefined,
  },
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8001",
      "/ws": { target: "ws://127.0.0.1:8001", ws: true },
    },
  },
});
