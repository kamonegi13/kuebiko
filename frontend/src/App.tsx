import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./api/client";
import { useFilters } from "./state/filters";
import { AppShell } from "./components/AppShell";
import { Shell } from "./components/Shell";
import { goToIntel } from "./utils/intelNav";
import { DashboardPage } from "./pages/DashboardPage";
import { NewsPage } from "./pages/NewsPage";
import { RunDetailPage } from "./pages/RunDetailPage";
import { HistoryPage } from "./pages/HistoryPage";
import { ArticleDetailPage } from "./pages/ArticleDetailPage";
import { NotesPage } from "./pages/NotesPage";
import { DailyBriefPage } from "./pages/DailyBriefPage";
import { AssistantPage } from "./pages/AssistantPage";
import { SubscriptionsPage } from "./pages/SubscriptionsPage";
import { ActorsPage } from "./pages/ActorsPage";
import { ConfigPage } from "./pages/ConfigPage";
import { JobsConsolePage } from "./pages/JobsConsolePage";
import { PirListPage } from "./pages/PirListPage";
import { PirDetailPage } from "./pages/PirDetailPage";
import { PirEditPage } from "./pages/PirEditPage";
import { FlowPage } from "./pages/FlowPage";
import { MapPage } from "./pages/MapPage";
import { JpCiBoardPage } from "./pages/JpCiBoardPage";
import { JpCiOperatorsPage } from "./pages/JpCiOperatorsPage";
import { useWebSocket } from "./hooks/useWebSocket";
import { useRuntimeFlags, shouldHideFullOnly } from "./hooks/useRuntimeFlags";
import { isFullOnlyPath } from "./components/nav";
import { ArticlePeekHost } from "./components/ArticlePeek";

type IntelTab = "synthesis" | "pmesii" | "threats" | "forecast" | "operations";

type Route =
  | { kind: "dashboard" }
  | { kind: "news" }
  | { kind: "intel"; tab: IntelTab }
  | { kind: "run-detail"; runId: number }
  | { kind: "history" }
  | { kind: "article"; articleId: string }
  | { kind: "notes" }
  | { kind: "daily-brief" }
  | { kind: "assistant" }
  | { kind: "subscriptions" }
  | { kind: "actors" }
  | { kind: "config" }
  | { kind: "flow" }
  | { kind: "map" }
  | { kind: "jpci" }
  | { kind: "jpci-operators" }
  | { kind: "schedule" }
  | { kind: "pir-list" }
  | { kind: "pir-detail"; pirId: string }
  | { kind: "pir-edit"; pirId: string | null }
  | { kind: "unknown" };

const INTEL_TABS: IntelTab[] = ["synthesis", "pmesii", "threats", "forecast", "operations"];

function parseRoute(): Route {
  const p = window.location.pathname.replace(/\/$/, "");
  // landing = ダッシュボード (overview-first)
  if (p === "/app" || p === "") return { kind: "dashboard" };
  if (p === "/app/dashboard") return { kind: "dashboard" };
  if (p === "/app/news") return { kind: "news" };
  // Intel Graph: 4 タブを第一級ルート化。/app/intel は synthesis 既定
  if (p === "/app/intel") return { kind: "intel", tab: "synthesis" };
  const intelMatch = p.match(/^\/app\/intel\/([a-z]+)$/);
  if (intelMatch && (INTEL_TABS as string[]).includes(intelMatch[1])) {
    return { kind: "intel", tab: intelMatch[1] as IntelTab };
  }
  // Phase Diamond verify: /app/runs を /app/schedule に集約
  if (p === "/app/runs") {
    window.location.replace("/app/schedule");
    return { kind: "schedule" };
  }
  const m = p.match(/^\/app\/run\/(\d+)$/);
  if (m) return { kind: "run-detail", runId: parseInt(m[1], 10) };
  if (p === "/app/history") return { kind: "history" };
  // /app/health は廃止 (設定・死活の画面統合 P4) — 死活はダッシュボード widget と
  // 各対象画面 (情報フロー/購読ソース/モデルタブ) に統合。旧 URL はダッシュボードへ。
  if (p === "/app/health") {
    window.location.replace("/app/");
    return { kind: "dashboard" };
  }
  // 検索・逆引きは記事サーフェス (/app/news) に統合。旧 /app/search?q= と
  // /app/pivot?type=&value= (= 旧 /app/search?type=&value=) を news のクエリに翻訳して redirect。
  if (p === "/app/pivot" || p === "/app/search") {
    const src = new URLSearchParams(window.location.search);
    const out = new URLSearchParams();
    const type = src.get("type");
    const value = src.get("value");
    const q = src.get("q");
    if (type && value) { out.set("pivot_type", type); out.set("pivot_value", value); }
    else if (q) { out.set("search", q); }
    const qs = out.toString();
    window.location.replace(qs ? `/app/news?${qs}` : "/app/news");
    return { kind: "news" };
  }
  if (p === "/app/notes") return { kind: "notes" };
  if (p === "/app/actors") return { kind: "actors" };
  // 週次振り返りはブリーフページに統合 (2026-07-25 時間軸統合)。旧 URL は週次ビューへ。
  if (p === "/app/retrospect") {
    window.location.replace("/app/daily-brief#weekly");
    return { kind: "daily-brief" };
  }
  if (p === "/app/daily-brief") return { kind: "daily-brief" };
  if (p === "/app/assistant") return { kind: "assistant" };
  const articleMatch = p.match(/^\/app\/article\/(.+)$/);
  if (articleMatch) return { kind: "article", articleId: decodeURIComponent(articleMatch[1]) };
  if (p === "/app/subscriptions") return { kind: "subscriptions" };
  // Phase Diamond verify: /app/prompts を /app/config#prompts に集約
  if (p === "/app/prompts") {
    window.location.replace("/app/config#prompts");
    return { kind: "config" };
  }
  if (p === "/app/config") return { kind: "config" };
  // ナビ整理 (2026-07-26): 変更履歴 / マッチリストは設定のタブへ統合 (旧 URL は redirect)
  if (p === "/app/config-history") {
    window.location.replace("/app/config#history");
    return { kind: "config" };
  }
  // マッチリストは配信ルールの語彙なので情報フローへ移設 (2026-08-02 設定画面の整理)
  if (p === "/app/match-lists") {
    window.location.replace("/app/flow");
    return { kind: "flow" };
  }
  // 配信ルール/チャンネルの専用ページは廃止し情報フローに一本化 (旧 URL は redirect)
  if (p === "/app/routing" || p === "/app/channels") {
    window.location.replace("/app/flow");
    return { kind: "flow" };
  }
  if (p === "/app/flow") return { kind: "flow" };
  if (p === "/app/map") return { kind: "map" };
  if (p === "/app/jpci/operators") return { kind: "jpci-operators" };
  if (p === "/app/jpci") return { kind: "jpci" };
  if (p === "/app/schedule") return { kind: "schedule" };
  // PIR pages
  if (p === "/app/pir") return { kind: "pir-list" };
  if (p === "/app/pir/edit") return { kind: "pir-edit", pirId: null };
  const editMatch = p.match(/^\/app\/pir\/edit\/(.+)$/);
  if (editMatch) return { kind: "pir-edit", pirId: decodeURIComponent(editMatch[1]) };
  const detailMatch = p.match(/^\/app\/pir\/(.+)$/);
  if (detailMatch) return { kind: "pir-detail", pirId: decodeURIComponent(detailMatch[1]) };
  return { kind: "unknown" };
}

export default function App() {
  const [route, setRoute] = useState(parseRoute);
  const flags = useRuntimeFlags();

  useEffect(() => {
    const handler = () => setRoute(parseRoute());
    window.addEventListener("popstate", handler);
    return () => window.removeEventListener("popstate", handler);
  }, []);

  const pathname = typeof window !== "undefined" ? window.location.pathname : "/app";

  // readonly instance かつ未認証では fullOnly ページを直 URL でも描画しない
  // (nav.ts fullOnly が SSoT)。API はサーバ側 READ_ONLY_GET_DENYLIST が 403 で遮断する
  // ため、ここは UI の整合 (壊れた画面を見せない) が目的。認証済み (Tier1) は閲覧可。
  const effectiveRoute: Route =
    shouldHideFullOnly(flags) && isFullOnlyPath(pathname) ? { kind: "unknown" } : route;

  return (
    <>
    {/* 記事サイドピーク: /app/article/ リンクのクリックを捕捉して遷移なしでプレビュー */}
    <ArticlePeekHost />
    <AppShell pathname={pathname}>
      {effectiveRoute.kind === "dashboard" && <DashboardPage />}
      {effectiveRoute.kind === "news" && <NewsPage />}
      {effectiveRoute.kind === "intel" && <IntelGraphRoute tab={effectiveRoute.tab} />}
      {effectiveRoute.kind === "run-detail" && <RunDetailPage runId={effectiveRoute.runId} />}
      {effectiveRoute.kind === "history" && <HistoryPage />}
      {effectiveRoute.kind === "article" && <ArticleDetailPage articleId={effectiveRoute.articleId} />}
      {effectiveRoute.kind === "notes" && <NotesPage />}
      {effectiveRoute.kind === "daily-brief" && <DailyBriefPage />}
      {effectiveRoute.kind === "assistant" && <AssistantPage />}
      {effectiveRoute.kind === "subscriptions" && <SubscriptionsPage />}
      {effectiveRoute.kind === "actors" && <ActorsPage />}
      {effectiveRoute.kind === "config" && <ConfigPage />}
      {effectiveRoute.kind === "flow" && <FlowPage />}
      {effectiveRoute.kind === "map" && <MapPage />}
      {effectiveRoute.kind === "jpci" && <JpCiBoardPage />}
      {effectiveRoute.kind === "jpci-operators" && <JpCiOperatorsPage />}
      {effectiveRoute.kind === "schedule" && <JobsConsolePage />}
      {effectiveRoute.kind === "pir-list" && <PirListPage />}
      {effectiveRoute.kind === "pir-detail" && <PirDetailPage pirId={effectiveRoute.pirId} />}
      {effectiveRoute.kind === "pir-edit" && <PirEditPage pirId={effectiveRoute.pirId} />}
      {effectiveRoute.kind === "unknown" && (
        <div className="max-w-[800px] mx-auto p-10 text-center">
          <h2 className="text-fg text-xl mb-4">404 — Not Found</h2>
          <a href="/app/" className="text-accent hover:underline">→ ダッシュボードに戻る</a>
        </div>
      )}
    </AppShell>
    </>
  );
}

function IntelGraphRoute({ tab }: { tab: IntelTab }) {
  const f = useFilters();
  const qc = useQueryClient();

  // ルートのタブを Intel Graph の表示タブへ反映 (sidebar からの遷移を受ける)
  useEffect(() => {
    f.setTab(tab);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab]);

  const { data: snapshot } = useQuery({
    queryKey: ["snapshot", f.time, f.nation, f.family, f.japan_only, f.high_only, f.search],
    queryFn: () => api.snapshot({
      time: f.time, nation: f.nation, family: f.family,
      japan_only: f.japan_only, high_only: f.high_only, search: f.search,
    }),
  });

  useWebSocket((ev) => {
    if (ev.type === "article_posted") {
      qc.invalidateQueries({ queryKey: ["snapshot"] });
      qc.invalidateQueries({ queryKey: ["threats"] });
    }
    if (ev.type === "synthesis_ready") {
      qc.invalidateQueries({ queryKey: ["synthesis"] });
      qc.invalidateQueries({ queryKey: ["pmesii"] });
    }
    if (ev.type === "actor_new" || ev.type === "spike_alert") {
      qc.invalidateQueries({ queryKey: ["snapshot"] });
      qc.invalidateQueries({ queryKey: ["threats"] });
    }
  });

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement;
      if (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT") return;
      if (e.key === "g") {
        (window as unknown as { _ig_gPressed: boolean })._ig_gPressed = true;
        setTimeout(() => { (window as unknown as { _ig_gPressed: boolean })._ig_gPressed = false; }, 800);
        return;
      }
      const g = (window as unknown as { _ig_gPressed?: boolean })._ig_gPressed;
      if (g) {
        // g+letter: ビュー遷移 (左 nav と同じ pathname 遷移に一本化)
        if (e.key === "s") { e.preventDefault(); goToIntel("synthesis"); }
        if (e.key === "p") { e.preventDefault(); goToIntel("pmesii"); }
        if (e.key === "t") { e.preventDefault(); goToIntel("threats"); }
        if (e.key === "f") { e.preventDefault(); goToIntel("forecast"); }
        if (e.key === "o") { e.preventDefault(); goToIntel("operations"); }
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, []);

  return <Shell snapshot={snapshot} tab={tab} />;
}
