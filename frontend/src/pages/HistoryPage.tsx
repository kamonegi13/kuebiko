// History page 再設計: Sticky filter + Quick chips + Pagination + Article drawer。
// 200 articles 縦並びの 9.1× viewport scroll 地獄を解消。

import { useState, useMemo } from "react";
import { Flame, Trash2, X } from "lucide-react";
import { pageContainer } from "../components/Page";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { pagesApi, type HistoryArticle } from "../api/pages";
import { StatusBadge } from "./DashboardPage";
import { useWebSocket } from "../hooks/useWebSocket";
import { formatJst, formatJstCompact } from "../utils/date";
import { useRuntimeFlags } from "../hooks/useRuntimeFlags";
import { Drawer } from "../components/Drawer";
import { useChannelMeta } from "../components/channel";
import { vocabLabel } from "../hooks/useVocab";

const PAGE_SIZE = 30;

export function HistoryPage() {
  const qc = useQueryClient();
  const { read_only } = useRuntimeFlags();
  const chMeta = useChannelMeta();
  const [importance, setImportance] = useState("");
  const [status, setStatus] = useState("");
  const [channel, setChannel] = useState(""); // quick chip
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(1);
  const [openArticle, setOpenArticle] = useState<HistoryArticle | null>(null);

  const { data } = useQuery({
    queryKey: ["history", importance, status],
    queryFn: () => pagesApi.history({ importance, status, limit: 500 }),
  });

  useWebSocket((ev) => {
    if (ev.type === "article_posted" || ev.type === "pipeline_running" || ev.type === "pipeline_complete") {
      qc.invalidateQueries({ queryKey: ["history"] });
    }
  });

  const del = useMutation({
    mutationFn: (id: number) => pagesApi.historyDelete(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["history"] }),
  });
  const purge = useMutation({
    mutationFn: (days: number) => pagesApi.historyPurge(days),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["history"] }),
  });

  // フロント側 filter (chip + search)
  const filtered = useMemo(() => {
    const arr = data?.articles || [];
    return arr.filter((a) => {
      if (channel && a.posted_channel !== channel) return false;
      if (search) {
        const hay = `${a.title || ""} ${a.feed_title || ""}`.toLowerCase();
        if (!hay.includes(search.toLowerCase())) return false;
      }
      return true;
    });
  }, [data, channel, search]);

  const totalPages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const currentPage = Math.min(page, totalPages);
  const pageSlice = filtered.slice((currentPage - 1) * PAGE_SIZE, currentPage * PAGE_SIZE);

  return (
    <div className={`${pageContainer("wide")} space-y-4`}>
      <div className="flex items-baseline justify-between flex-wrap gap-2">
        <div>
          <h2 className="m-0 text-xl font-bold text-fg tracking-tight">取込・処理履歴</h2>
          <p className="m-0 mt-1 text-xs text-fg-subtle">
            パイプラインが処理した全記事 (失敗・重複スキップ含む) と実行の監査ログ。記事を読む・探すのは{" "}
            <a href="/app/news" className="text-accent hover:underline">ニュース・検索</a> へ。
          </p>
        </div>
        <span className="text-fg-subtle text-xs tnum">{filtered.length} / {data?.articles.length || 0} 件（ページ {currentPage}/{totalPages}）</span>
      </div>

      {/* Sticky filter bar */}
      <div className={`md:sticky md:top-12 z-20 bg-surface-1/95 backdrop-blur-md border border-border-subtle rounded-md p-2.5 flex flex-wrap items-center gap-2`}>
        <input
          type="search"
          value={search}
          onChange={(e) => { setSearch(e.target.value); setPage(1); }}
          placeholder="タイトル / フィード 検索..."
          className="bg-surface-2 border border-border-subtle rounded px-2 py-1 text-sm min-w-[180px] flex-1 max-w-[280px]"
        />
        <select value={importance} onChange={(e) => { setImportance(e.target.value); setPage(1); }} className="bg-surface-2 border border-border-subtle rounded px-2 py-1 text-sm">
          <option value="">重要度すべて</option>
          <option value="high">高</option>
          <option value="medium">中</option>
          <option value="low">低</option>
        </select>
        <select value={status} onChange={(e) => { setStatus(e.target.value); setPage(1); }} className="bg-surface-2 border border-border-subtle rounded px-2 py-1 text-sm">
          <option value="">状態すべて</option>
          <option value="posted">投稿済</option>
          <option value="summarized">要約済</option>
          <option value="extract_failed">本文抽出失敗</option>
          <option value="summarize_failed">要約失敗</option>
          <option value="post_failed">投稿失敗</option>
          <option value="skipped_duplicate">重複でスキップ</option>
        </select>
        <span className="w-px h-5 bg-border-subtle" />
        <Chip on={importance === "high"} onClick={() => { setImportance(importance === "high" ? "" : "high"); setPage(1); }}><span className="inline-flex items-center gap-1"><Flame className="h-3.5 w-3.5" /> 高のみ</span></Chip>
        <Chip on={status === "extract_failed" || status === "summarize_failed" || status === "post_failed"} onClick={() => { setStatus(status.includes("failed") ? "" : "post_failed"); setPage(1); }}><span className="inline-flex items-center gap-1"><X className="h-3.5 w-3.5" /> 失敗のみ</span></Chip>
        <Chip on={channel === "japan_watch"} onClick={() => { setChannel(channel === "japan_watch" ? "" : "japan_watch"); setPage(1); }}>日本</Chip>
        <Chip on={channel === "alert"} onClick={() => { setChannel(channel === "alert" ? "" : "alert"); setPage(1); }}>即応</Chip>
        <span className="ml-auto" />
        {!read_only && (
          <button
            onClick={() => { if (confirm("30 日より古いログを削除します。記事は保持されます。よろしいですか?")) purge.mutate(30); }}
            className="inline-flex items-center gap-1 bg-surface-2 border border-border-default hover:bg-critical-soft hover:border-critical hover:text-critical text-fg-muted text-xs px-2.5 py-1 rounded transition-colors"
            title="古いログのみ削除（記事・実行履歴は保持）"
          >
            <Trash2 className="h-3.5 w-3.5" /> 30日ログ削除
          </button>
        )}
      </div>

      {/* Recent runs (collapsible) */}
      <details className="bg-surface-1 border border-border-subtle rounded-lg overflow-hidden">
        <summary className="px-4 py-2 cursor-pointer hover:bg-surface-2 flex items-center justify-between">
          <h3 className="m-0 text-md font-semibold text-fg">直近の実行 ({data?.recent_runs.length || 0})</h3>
          <span className="text-fg-subtle text-xs">クリックで展開</span>
        </summary>
        <div className="overflow-x-auto"><table className="w-full text-sm">
          <thead className="bg-surface-2 text-fg-muted text-xs uppercase">
            <tr>
              <th className="text-left px-4 py-2">ID</th>
              <th className="text-left px-4 py-2">パイプライン</th>
              <th className="text-left px-4 py-2">状態</th>
              <th className="text-right px-4 py-2 hidden sm:table-cell">投稿数</th>
              <th className="text-right px-4 py-2 hidden sm:table-cell">エラー数</th>
              <th className="text-left px-4 py-2 hidden sm:table-cell">時刻</th>
              <th className="text-center px-4 py-2 w-12 hidden sm:table-cell">削除</th>
            </tr>
          </thead>
          <tbody>
            {data?.recent_runs.map((r) => (
              <tr key={r.id} className="border-t border-border-subtle hover:bg-surface-2 transition-colors">
                <td className="px-4 py-2"><a href={`/app/run/${r.id}`} className="text-accent hover:underline tnum">#{r.id}</a></td>
                <td className="px-4 py-2 text-fg">{r.pipeline}{r.dry_run && <span className="ml-1 text-xs text-fg-subtle">(dry)</span>}</td>
                <td className="px-4 py-2"><StatusBadge status={r.status} /></td>
                <td className="px-4 py-2 text-right tnum text-fg hidden sm:table-cell">{r.posted}</td>
                <td className={`px-4 py-2 text-right tnum hidden sm:table-cell ${r.error_count > 0 ? "text-critical" : "text-fg-subtle"}`}>{r.error_count}</td>
                <td className="px-4 py-2 text-fg-subtle text-xs hidden sm:table-cell">{formatJst(r.started_at)}</td>
                <td className="px-4 py-2 text-center hidden sm:table-cell">
                  {!read_only && r.status !== "running" && (
                    <button
                      onClick={() => { if (confirm(`run #${r.id} を削除します。よろしいですか?`)) del.mutate(r.id); }}
                      className="inline-flex text-fg-subtle hover:text-critical text-xs"
                      disabled={del.isPending}
                    ><Trash2 className="h-3.5 w-3.5" /></button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table></div>
      </details>

      {/* Articles table (paginated) */}
      <div className="bg-surface-1 border border-border-subtle rounded-lg overflow-hidden">
        <div className="overflow-x-auto"><table className="w-full text-sm">
          <thead className="bg-surface-2 text-fg-muted text-[10.5px] uppercase tracking-wider">
            <tr>
              <th className="text-left px-3 py-2 w-28 hidden sm:table-cell">時刻</th>
              <th className="text-left px-3 py-2 w-32 hidden md:table-cell">フィード</th>
              <th className="text-left px-3 py-2">タイトル</th>
              <th className="text-left px-3 py-2 w-20">重要度</th>
              <th className="text-left px-3 py-2 w-28 hidden sm:table-cell">状態</th>
              <th className="text-left px-3 py-2 w-24 hidden md:table-cell">チャンネル</th>
            </tr>
          </thead>
          <tbody>
            {pageSlice.length === 0 && (
              <tr><td colSpan={6} className="px-4 py-8 text-center text-fg-subtle text-sm">該当する記事なし</td></tr>
            )}
            {pageSlice.map((a) => (
              <tr key={a.id} className="border-t border-border-subtle hover:bg-surface-2 transition-colors cursor-pointer" onClick={() => setOpenArticle(a)}>
                <td className="px-3 py-2 text-fg-subtle text-xs whitespace-nowrap hidden sm:table-cell">{formatJstCompact(a.created_at)}</td>
                <td className="px-3 py-2 text-fg-muted text-xs truncate max-w-[120px] hidden md:table-cell">{a.feed_title?.slice(0, 18)}</td>
                <td className="px-3 py-2 text-fg text-sm">{a.title?.slice(0, 100)}</td>
                <td className="px-3 py-2"><ImportanceBadge value={a.importance} /></td>
                <td className="px-3 py-2 text-fg text-xs font-mono hidden sm:table-cell">{vocabLabel("article_status", a.status)}</td>
                <td className="px-3 py-2 text-fg-muted text-xs hidden md:table-cell">{a.posted_channel ? chMeta(a.posted_channel).label : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table></div>
        {totalPages > 1 && (
          <div className="flex items-center justify-between gap-2 px-4 py-2.5 border-t border-border-subtle bg-surface-2/40">
            <button onClick={() => setPage(1)} disabled={currentPage === 1} className="text-xs px-2 py-1 rounded bg-surface-2 border border-border-subtle text-fg-muted hover:bg-surface-3 disabled:opacity-30">最初</button>
            <button onClick={() => setPage(p => Math.max(1, p - 1))} disabled={currentPage === 1} className="text-xs px-2 py-1 rounded bg-surface-2 border border-border-subtle text-fg-muted hover:bg-surface-3 disabled:opacity-30">← 前へ</button>
            <span className="text-fg-subtle text-xs tnum">ページ {currentPage} / {totalPages}</span>
            <button onClick={() => setPage(p => Math.min(totalPages, p + 1))} disabled={currentPage === totalPages} className="text-xs px-2 py-1 rounded bg-surface-2 border border-border-subtle text-fg-muted hover:bg-surface-3 disabled:opacity-30">次へ →</button>
            <button onClick={() => setPage(totalPages)} disabled={currentPage === totalPages} className="text-xs px-2 py-1 rounded bg-surface-2 border border-border-subtle text-fg-muted hover:bg-surface-3 disabled:opacity-30">最後</button>
          </div>
        )}
      </div>

      {/* Drawer: article 詳細 */}
      {openArticle && (
        <Drawer
          isOpen={openArticle !== null}
          onClose={() => setOpenArticle(null)}
          title="記事詳細"
        >
          <ArticleDetailView article={openArticle} />
        </Drawer>
      )}
    </div>
  );
}

function Chip({ on, onClick, children }: { on: boolean; onClick: () => void; children: React.ReactNode }) {
  return (
    <span
      onClick={onClick}
      className={`px-2.5 py-1 rounded cursor-pointer text-xs font-medium select-none transition-all ${
        on
          ? "bg-accent-subtle border border-accent text-accent-hover"
          : "bg-surface-2 border border-border-subtle text-fg-muted hover:bg-surface-3 hover:text-fg"
      }`}
    >{children}</span>
  );
}

function ArticleDetailView({ article: a }: { article: HistoryArticle }) {
  const chMeta = useChannelMeta();
  return (
    <div className="space-y-3">
      <div className="bg-surface-2 rounded p-3">
        <div className="flex items-baseline gap-2 mb-1 flex-wrap">
          <ImportanceBadge value={a.importance} />
          <span className="text-[10px] uppercase bg-surface-3 text-fg-muted px-2 py-0.5 rounded font-mono">{vocabLabel("article_status", a.status)}</span>
          {a.posted_channel && <span className="text-[10px] uppercase bg-accent-subtle text-accent-hover px-2 py-0.5 rounded font-mono">{chMeta(a.posted_channel).label}</span>}
          <span className="text-fg-subtle text-xs ml-auto">{formatJst(a.created_at)}</span>
        </div>
        <h3 className="m-0 mt-1 text-md font-semibold text-fg">{a.title}</h3>
        <div className="text-fg-subtle text-xs mt-1">{a.feed_title}{a.category && ` · ${vocabLabel("category", a.category)}`}</div>
      </div>

      <div className="space-y-2 text-sm">
        <div>
          <div className="text-[10px] uppercase tracking-wider text-fg-muted mb-1">URL</div>
          <a href={a.url} target="_blank" rel="noopener noreferrer" className="text-accent hover:underline text-xs break-all">{a.url}</a>
        </div>
        <div className="grid grid-cols-2 gap-3 text-xs">
          <div>
            <div className="text-[10px] uppercase tracking-wider text-fg-muted mb-1">article_id</div>
            <code className="text-fg-muted">{a.article_id}</code>
          </div>
          <div>
            <div className="text-[10px] uppercase tracking-wider text-fg-muted mb-1">実行</div>
            <a href={`/app/run/${a.run_id}`} className="text-accent hover:underline">#{a.run_id}</a>
          </div>
        </div>
        {a.status === "posted" && (
          <a
            href={`/app/article/${encodeURIComponent(a.article_id)}`}
            className="inline-block text-xs text-accent hover:underline"
          >
            ニュースで見る (要約・IoC・アクター等の全情報) →
          </a>
        )}
      </div>

      <div className="text-fg-subtle text-[11px] italic border-t border-border-subtle pt-2">
        要約・IoC・アクター言及などの詳細は今後表示予定です。
        現在は基本情報のみ表示しています。
      </div>
    </div>
  );
}

function ImportanceBadge({ value }: { value: string }) {
  if (!value) return <span className="text-fg-subtle text-xs">—</span>;
  const cls =
    value === "high" ? "bg-critical-soft text-critical" :
    value === "medium" ? "bg-warning-soft text-warning" :
    "bg-surface-3 text-fg-muted";
  return <span className={`inline-flex items-center px-2 py-0.5 rounded text-[10px] font-semibold ${cls}`}>{vocabLabel("importance", value)}</span>;
}
