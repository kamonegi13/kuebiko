import React from "react";
import ReactDOM from "react-dom/client";
import { QueryClientProvider } from "@tanstack/react-query";
import "./index.css";
import App from "./App";
import { MIRROR_HOME } from "./components/nav";
import { installMirrorFetch } from "./api/mirrorFetch";
import { queryClient } from "./api/queryClient";
import { VocabGate } from "./components/VocabGate";

// 写しの着地先。ダッシュボードは写していないので、mount 前に写した画面へ付け替える。
// **描画中ではなく mount 前に**やる — 描画中に URL を書き換えると、この時点で
// 解釈済みの route と食い違って一度空の画面が出る。
if (import.meta.env.VITE_MIRROR === "1") {
  installMirrorFetch();
  const p = window.location.pathname;
  if (p === "/" || p === "/app" || p === "/app/") {
    window.history.replaceState({}, "", MIRROR_HOME + window.location.search);
  }
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <VocabGate>
        <App />
      </VocabGate>
    </QueryClientProvider>
  </React.StrictMode>,
);
