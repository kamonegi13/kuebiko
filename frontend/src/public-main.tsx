// 公開サイト専用のエントリ (Cloudflare Pages で配信する静的ビルド)。
//
// 管理 UI (App) を **読み込まない**。同じバンドルに同居させると、読者向けの
// 配信物に運用画面のコードが丸ごと載る (1.38 MB)。公開面が必要とするのは
// ニュースの表示だけなので、依存を切り離す。
//
// 語彙 (VocabGate) も使わない: 公開面で語彙ラベルが要るのはカテゴリ名だけで、
// それは書き出した index.json の `categories` から出せる。認証状態を問い合わせる
// runtime-flags も静的配信では意味を持たない。
import React from "react";
import ReactDOM from "react-dom/client";
import { QueryClientProvider } from "@tanstack/react-query";
import "./index.css";
import { queryClient } from "./api/queryClient";
import { PublicNewsSite } from "./public/PublicNewsSite";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <PublicNewsSite />
    </QueryClientProvider>
  </React.StrictMode>,
);
