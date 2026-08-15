// Subscriptions 再設計: Sticky search + Status group folding + Drawer 詳細。
// 115+ feeds 縦並びの 4.8× viewport scroll を解消、status 別に発見しやすく。

import { useState, useMemo } from "react";
import { AlertTriangle, Check, Flame, Info, Moon, Pencil, Trash2, X, type LucideIcon } from "lucide-react";
import { pageContainer } from "../components/Page";
import { useQuery, useQueryClient, useMutation } from "@tanstack/react-query";
import { pagesApi, type Subscription, type FeedStats, type ReliabilityTier } from "../api/pages";
import { sourcesV2Api } from "../api/sources_v2";
import { useWebSocket } from "../hooks/useWebSocket";
import { useRuntimeFlags } from "../hooks/useRuntimeFlags";
import { Drawer } from "../components/Drawer";
// 統合 Add Source wizard で全 transport (RSS/sitemap/HTML scraper) を 1 フローで登録。
import { AddSourceWizardV2 } from "../components/AddSourceWizardV2";
// Grok 取込経路 (メール受信 IMAP + Playwright セッション) の設定+死活カード。
// 設定・死活の画面統合 P2/P4: 旧 /app/health から移設し、ソースと同じ画面で一体管理。
import { GrokHubCard } from "../components/GrokHubCard";
import { Spinner } from "../components/Spinner";
import { formatJstDate } from "../utils/date";
import { useChannelMeta } from "../components/channel";
import { vocabLabel } from "../hooks/useVocab";

// 低貢献マーカ (this page 固有の値。SSoT には入れない — 他画面で使わないため)。
// 実値は src/ui/services/subscription_analytics.py:low_contrib_labels に対応。
const LOW_CONTRIB_LABELS: Record<string, string> = {
  new: "新規",
  "no-articles": "記事なし",
  "no-posts": "投稿なし",
  "low-volume": "低頻度",
  "high-dup": "重複多",
  "watch-only": "観察止まり",
};

type GroupKey = "fetch_error" | "body_error" | "warn" | "high_value" | "normal" | "quiet" | "no_data";

interface EnrichedFeed extends Subscription {
  stats?: FeedStats;
  group: GroupKey;
}

// 連続取得失敗がこの回数以上 = 取得系の異常 (要対処)。毎時 fetch なので 3 ≈ 3 時間分
const FETCH_ERROR_THRESHOLD = 3;

function classify(s: FeedStats | undefined, health?: Subscription["health"]): GroupKey {
  // 層別に壊れた場所を名指しする (2026-08-01): 「記事が出ない」を 1 ラベルに潰さない。
  // ① feed 取得層: 連続失敗中は stats の有無に関係なく「取得エラー」
  if (health && health.consecutive_failures >= FETCH_ERROR_THRESHOLD) return "fetch_error";
  if (!s) {
    // stats なし ≠ 異常とは限らない: 取得 OK なら「30日 記事化なし」(低頻度/全落ち) として
    // 静観側に出す (2026-07-12: 「stats なし」が 5 実態の混合で誤解を生んだ根治)
    return health?.last_ok_at ? "quiet" : "no_data";
  }
  const total = s.posted_count + s.dup_skipped;
  // ② 本文取得層: feed は取れて記事レコードは届くが、本文抽出が全滅している
  // (JS チャレンジ導入等)。取得エラーとも「記事なし」とも別の対処が要る
  if (total === 0 && (s.extract_failed_count ?? 0) > 0) return "body_error";
  // ③ 取得も本文も問題ないのに 30 日間 記事化ゼロ: 要対処 (feed 側が枯れた等)
  if (total === 0) return "warn";
  // high-importance 5+ 件: 高貢献
  if (s.high_count >= 5) return "high_value";
  // 投稿 10+ 件: 正常
  if (s.posted_count >= 10) return "normal";
  // それ以外: 静観 (低 volume)
  return "quiet";
}

const GROUP_META: Record<GroupKey, { label: string; Icon: LucideIcon; tone: string; defaultOpen: boolean }> = {
  fetch_error: { label: "取得エラー (feed 取得が連続失敗中)", Icon: AlertTriangle, tone: "border-l-critical", defaultOpen: true },
  body_error: { label: "本文取得エラー (feed は取れるが本文抽出が失敗)", Icon: AlertTriangle, tone: "border-l-critical", defaultOpen: true },
  warn: { label: "要対処 (取得は成功・30日 記事化なし)", Icon: AlertTriangle, tone: "border-l-critical", defaultOpen: true },
  high_value: { label: "高貢献 (重要度: 高 が多い)", Icon: Flame, tone: "border-l-success", defaultOpen: true },
  normal: { label: "正常稼働", Icon: Check, tone: "border-l-accent", defaultOpen: false },
  quiet: { label: "静観 (低頻度 / 新規)", Icon: Moon, tone: "border-l-fg-subtle", defaultOpen: false },
  no_data: { label: "統計データなし", Icon: Info, tone: "border-l-fg-subtle", defaultOpen: false },
} as const;

// folder フィルタの「未分類」疑似値 (null=all と区別)
const UNCATEGORIZED = "__uncategorized__";

export function SubscriptionsPage() {
  const { read_only } = useRuntimeFlags();
  const qc = useQueryClient();
  const { data } = useQuery({
    queryKey: ["subscriptions"],
    queryFn: () => pagesApi.subscriptions(),
    staleTime: 5 * 60_000,
  });
  useWebSocket((ev) => {
    if (ev.type === "article_posted") qc.invalidateQueries({ queryKey: ["subscriptions"] });
  });

  const [search, setSearch] = useState("");
  const [folderFilter, setFolderFilter] = useState<string | null>(null); // null = all
  const [open, setOpen] = useState<EnrichedFeed | null>(null);
  // Phase F: 単一 wizard
  const [sourceWizardOpen, setSourceWizardOpen] = useState(false);
  const [sourceWizardInitialUrl, setSourceWizardInitialUrl] = useState<string>("");
  // Phase F+: multi-select は feed_id キー (全 transport 横断)
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [bulkFolder, setBulkFolder] = useState("");

  // bulk enable/disable/delete は transport 透過の統合 endpoint。
  const bulkMut = useMutation({
    mutationFn: (action: "enable" | "disable" | "delete") =>
      sourcesV2Api.bulk(Array.from(selected), action),
    onSuccess: () => {
      setSelected(new Set());
      qc.invalidateQueries({ queryKey: ["subscriptions"] });
    },
  });
  // folder は全 transport 共通の分類軸 → 選択中の全ソースに適用 (空文字で解除)。
  const folderMut = useMutation({
    mutationFn: () => sourcesV2Api.setFolder(Array.from(selected), bulkFolder.trim()),
    onSuccess: () => {
      setBulkFolder("");
      setSelected(new Set());
      qc.invalidateQueries({ queryKey: ["subscriptions"] });
    },
  });
  // S3: per-source 信頼度ティアの上書き (raw YAML 不要、一覧上のドロップダウンで編集)。
  const reliabilityMut = useMutation({
    mutationFn: (v: { feed_url: string; title: string; tier: ReliabilityTier | "auto" }) =>
      pagesApi.setReliability(v.feed_url, v.title, v.tier),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["subscriptions"] }),
  });

  function openSourceWizard(url = "") {
    setSourceWizardInitialUrl(url);
    setSourceWizardOpen(true);
  }
  function toggleSelected(feedId: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(feedId)) next.delete(feedId);
      else next.add(feedId);
      return next;
    });
  }
  function selectAllInGroup(feedIds: string[]) {
    setSelected((prev) => {
      const next = new Set(prev);
      const allSelected = feedIds.every((id) => next.has(id));
      if (allSelected) feedIds.forEach((id) => next.delete(id));
      else feedIds.forEach((id) => next.add(id));
      return next;
    });
  }

  // enrichment + grouping
  const enriched: EnrichedFeed[] = useMemo(() => {
    if (!data) return [];
    return data.subscriptions.map((s) => {
      // Source Identity Decoupling: 統計は安定キー (s.url = feed_url) で結合する。
      // 旧記事 (feed_url 未充足) 用に表示名 (s.title) へ fallback。
      const stats = data.stats[s.url] ?? data.stats[s.title];
      return { ...s, stats, group: classify(stats, s.health) };
    });
  }, [data]);

  // folder = 全 transport 共通の分類軸。一覧をフィルタする第一の軸。
  const folders = useMemo(() => {
    const set = new Set<string>();
    enriched.forEach((f) => f.folder_labels.forEach((l) => set.add(l)));
    return Array.from(set).sort();
  }, [enriched]);
  const hasUncategorized = useMemo(
    () => enriched.some((f) => f.folder_labels.length === 0),
    [enriched],
  );

  function inFolder(f: EnrichedFeed): boolean {
    if (folderFilter === null) return true;
    if (folderFilter === UNCATEGORIZED) return f.folder_labels.length === 0;
    return f.folder_labels.includes(folderFilter);
  }

  const filtered = useMemo(() => {
    const q = search.toLowerCase();
    return enriched.filter(
      (f) => inFolder(f) && (!q || f.title.toLowerCase().includes(q)),
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enriched, search, folderFilter]);

  const groups: Record<GroupKey, EnrichedFeed[]> = useMemo(() => ({
    fetch_error: filtered.filter((f) => f.group === "fetch_error"),
    body_error: filtered.filter((f) => f.group === "body_error"),
    warn: filtered.filter((f) => f.group === "warn"),
    high_value: filtered.filter((f) => f.group === "high_value"),
    normal: filtered.filter((f) => f.group === "normal"),
    quiet: filtered.filter((f) => f.group === "quiet"),
    no_data: filtered.filter((f) => f.group === "no_data"),
  }), [filtered]);

  // Phase C: Health Card 統計 (全 feed 横断)
  const healthSummary = useMemo(() => {
    const scored = enriched.filter((f) => f.stats?.quality_score !== undefined);
    const total = enriched.length;
    const scoreSum = scored.reduce((s, f) => s + (f.stats?.quality_score ?? 0), 0);
    const avgScore = scored.length ? Math.round(scoreSum / scored.length) : 0;
    const totalPosted = enriched.reduce((s, f) => s + (f.stats?.posted_count ?? 0), 0);
    const totalHigh = enriched.reduce((s, f) => s + (f.stats?.high_count ?? 0), 0);
    const highDupFeeds = enriched.filter((f) => (f.stats?.dup_rate ?? 0) >= 0.8 && (f.stats?.posted_count ?? 0) + (f.stats?.dup_skipped ?? 0) >= 5).length;
    const watchOnlyFeeds = enriched.filter((f) => (f.stats?.watch_only_rate ?? 0) >= 1.0 && (f.stats?.posted_count ?? 0) >= 5).length;
    const deadFeeds = enriched.filter((f) => f.group === "warn").length;
    return { total, avgScore, totalPosted, totalHigh, highDupFeeds, watchOnlyFeeds, deadFeeds };
  }, [enriched]);

  return (
    <div className={`${pageContainer("wide")} space-y-4`}>
      <div className="flex items-baseline justify-between flex-wrap gap-2">
        <h2 className="m-0 text-xl font-bold text-fg tracking-tight">購読 feed</h2>
        <div className="flex items-center gap-3">
          <span className="text-fg-subtle text-xs tnum">{filtered.length} / {enriched.length} 件</span>
          {read_only ? (
            <span className="text-[10px] uppercase tracking-wider bg-surface-3 text-fg-subtle px-2 py-1 rounded font-mono">閲覧専用</span>
          ) : (
            <button
              onClick={() => openSourceWizard()}
              className="bg-accent hover:bg-accent-hover text-white px-3 py-1 rounded text-sm font-semibold transition-colors"
            >
              + ソースを追加
            </button>
          )}
        </div>
      </div>

      {data?.sources_error && (
        <div className="bg-critical-soft border border-critical rounded-md px-4 py-3 text-critical text-sm flex items-center gap-1.5">
          <AlertTriangle className="h-4 w-4 shrink-0" /> フィード設定の読み込みに失敗: {data.sources_error}
        </div>
      )}

      {/* Phase C: Health Card */}
      <HealthCard summary={healthSummary} />

      {/* Grok 取込経路 (feed 以外のソース): 通常は 1 行サマリ、詳細は Drawer (2026-08-15) */}
      <GrokHubCard readOnly={read_only} />

      {/* Phase D: Bulk action toolbar — selected > 0 で出現 (readonly では非表示) */}
      {!read_only && selected.size > 0 && (
        <div className="sticky top-12 z-30 bg-critical-soft border border-critical/40 rounded-md p-2.5 flex flex-wrap items-center gap-2">
          <span className="text-sm text-fg font-semibold inline-flex items-center gap-1">
            <Check className="h-3.5 w-3.5" /> {selected.size} 件選択中
          </span>
          <button
            onClick={() => bulkMut.mutate("enable")}
            disabled={bulkMut.isPending}
            className="border border-success/60 text-success hover:bg-success-soft px-2.5 py-1 rounded text-xs font-semibold inline-flex items-center gap-1"
          >
            <Check className="h-3.5 w-3.5" /> 有効化
          </button>
          <button
            onClick={() => bulkMut.mutate("disable")}
            disabled={bulkMut.isPending}
            className="border border-warning/60 text-warning hover:bg-warning-soft px-2.5 py-1 rounded text-xs font-semibold"
          >
            ⊘ 無効化
          </button>
          <button
            onClick={() => {
              if (confirm(`${selected.size} 件のソースを削除します。 取り消し不可。`)) {
                bulkMut.mutate("delete");
              }
            }}
            disabled={bulkMut.isPending}
            className="border border-critical/60 text-critical hover:bg-critical-soft px-2.5 py-1 rounded text-xs font-semibold inline-flex items-center gap-1"
          >
            <Trash2 className="h-3.5 w-3.5" /> 削除
          </button>
          <span className="text-fg-subtle text-xs mx-1">|</span>
          <input
            type="text"
            list="folder-suggestions"
            value={bulkFolder}
            onChange={(e) => setBulkFolder(e.target.value.toLowerCase())}
            placeholder="folder (空で解除)"
            className="bg-surface-2 border border-border-subtle rounded px-2 py-1 text-xs w-32 font-mono"
          />
          <datalist id="folder-suggestions">
            {folders.map((fl) => <option key={fl} value={fl} />)}
          </datalist>
          <button
            onClick={() => folderMut.mutate()}
            disabled={folderMut.isPending}
            title="選択中の全ソースに folder を適用 (空文字で解除)"
            className="border border-accent/60 text-accent hover:bg-accent-subtle disabled:opacity-40 px-2.5 py-1 rounded text-xs font-semibold"
          >
            フォルダを設定
          </button>
          <button
            onClick={() => setSelected(new Set())}
            className="text-fg-subtle hover:text-fg text-xs ml-auto"
          >
            選択を解除
          </button>
          {(bulkMut.isPending || folderMut.isPending) && (
            <span className="text-fg-subtle text-xs inline-flex items-center gap-1.5">
              <Spinner size="xs" />
              <span>適用中...</span>
            </span>
          )}
          {bulkMut.isError && <span className="text-critical text-xs">{String(bulkMut.error)}</span>}
        </div>
      )}

      {/* Sticky search + group summary */}
      <div className={`md:sticky md:top-12 z-20 bg-surface-1/95 backdrop-blur-md border border-border-subtle rounded-md p-2.5 flex flex-wrap items-center gap-3`}>
        <input
          type="search"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="feed 検索..."
          className="bg-surface-2 border border-border-subtle rounded px-2 py-1 text-sm min-w-[200px] flex-1 max-w-[320px]"
        />
        <div className="flex gap-1.5 flex-wrap">
          {(["fetch_error", "body_error", "warn", "high_value", "normal", "quiet", "no_data"] as GroupKey[]).map((k) => (
            <span key={k} className={`px-2 py-0.5 rounded text-xs flex items-center gap-1 border ${
              k === "warn" ? "border-critical/40 text-critical bg-critical-soft" :
              k === "high_value" ? "border-success/40 text-success bg-success-soft" :
              k === "normal" ? "border-accent/40 text-accent-hover bg-accent-subtle" :
              "border-border-subtle text-fg-muted bg-surface-2"
            }`}>
              {(() => { const I = GROUP_META[k].Icon; return <I className="h-3.5 w-3.5" />; })()} {groups[k].length}
            </span>
          ))}
        </div>
      </div>

      {/* Folder filter axis (全 transport 共通の分類軸) */}
      {(folders.length > 0 || hasUncategorized) && (
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="text-[10px] uppercase tracking-wider text-fg-muted mr-1">folder</span>
          <FolderChip label="all" active={folderFilter === null} onClick={() => setFolderFilter(null)} />
          {folders.map((fl) => (
            <FolderChip
              key={fl}
              label={fl}
              active={folderFilter === fl}
              onClick={() => setFolderFilter(folderFilter === fl ? null : fl)}
            />
          ))}
          {hasUncategorized && (
            <FolderChip
              label="未分類"
              active={folderFilter === UNCATEGORIZED}
              onClick={() => setFolderFilter(folderFilter === UNCATEGORIZED ? null : UNCATEGORIZED)}
            />
          )}
        </div>
      )}

      {/* Status groups */}
      <div className="space-y-2">
        {(["fetch_error", "body_error", "warn", "high_value", "normal", "quiet", "no_data"] as GroupKey[]).map((k) => {
          const items = groups[k];
          const meta = GROUP_META[k];
          if (items.length === 0) return null;
          return (
            <details key={k} className={`bg-surface-1 border border-border-subtle border-l-[3px] ${meta.tone} rounded-lg overflow-hidden`} open={meta.defaultOpen}>
              <summary className="px-4 py-2.5 cursor-pointer hover:bg-surface-2 flex justify-between items-center select-none">
                <h3 className="m-0 text-md font-semibold text-fg flex items-center gap-2">
                  <meta.Icon className="h-4 w-4" /><span>{meta.label}</span>
                </h3>
                <span className="text-fg-subtle text-xs tnum">{items.length} feeds</span>
              </summary>
              <div className="overflow-x-auto"><table className="w-full text-sm">
                <thead className="bg-surface-2 text-fg-muted text-[10.5px] uppercase tracking-wider">
                  <tr>
                    <th className="px-2 py-2 w-8">
                      {!read_only && (
                        <input
                          type="checkbox"
                          checked={items.length > 0 && items.every((f) => selected.has(f.feed_id))}
                          onChange={() => selectAllInGroup(items.map((f) => f.feed_id))}
                          onClick={(e) => e.stopPropagation()}
                          aria-label="select all in group"
                        />
                      )}
                    </th>
                    <th className="text-left px-3 py-2">タイトル</th>
                    <th className="text-left px-3 py-2 w-28 hidden md:table-cell">信頼度</th>
                    <th
                      className="text-right px-3 py-2 w-20 hidden sm:table-cell"
                      title="貢献度: feed の実績スコア 0-100 (記事の量×重要度・alert ヒットで加点、重複/watch偏りで減点)。信頼度=権威とは別軸。"
                    >
                      貢献度
                    </th>
                    <th className="text-right px-3 py-2 w-20">投稿数</th>
                    <th className="text-right px-3 py-2 w-16 hidden sm:table-cell">高</th>
                    <th className="text-right px-3 py-2 w-16 hidden sm:table-cell">中</th>
                    <th
                      className="text-right px-3 py-2 w-20 hidden sm:table-cell"
                      title="本文の全文取得率。低い=このソースは記事本文が全文取得できず「フィード抜粋のみ」の切り株が多い (WAF ブロックや PDF 等)。フィード取得の健全性 (取れたか) とは別レイヤ。"
                    >
                      本文
                    </th>
                    <th className="text-left px-3 py-2 w-28 hidden lg:table-cell">初回検出</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((f) => {
                    const st = f.stats;
                    const high = st?.high_count ?? 0;
                    const med = st?.medium_count ?? 0;
                    const q = st?.quality_score;
                    const isChecked = selected.has(f.feed_id);
                    const isDisabled = f.enabled === false;
                    return (
                      <tr
                        key={f.feed_id}
                        className={`border-t border-border-subtle hover:bg-surface-2 cursor-pointer ${
                          isChecked ? "bg-accent-subtle" : ""
                        } ${isDisabled ? "opacity-50" : ""}`}
                        onClick={() => setOpen(f)}
                      >
                        <td className="px-2 py-2" onClick={(e) => e.stopPropagation()}>
                          {!read_only && (
                            <input
                              type="checkbox"
                              checked={isChecked}
                              onChange={() => toggleSelected(f.feed_id)}
                              aria-label={`select ${f.title}`}
                            />
                          )}
                        </td>
                        <td className="px-3 py-2 text-fg text-sm">
                          <TransportBadge transport={f.transport} />
                          {isDisabled && (
                            <span className="mr-2 px-1.5 py-0.5 rounded text-[9px] font-semibold border border-fg-subtle/40 text-fg-subtle align-middle">
                              無効
                            </span>
                          )}
                          {f.title}
                        </td>
                        <td className="px-3 py-2 hidden md:table-cell" onClick={(e) => e.stopPropagation()}>
                          <ReliabilitySelect
                            value={f.reliability_tier ?? "news"}
                            disabled={read_only}
                            onChange={(tier) =>
                              reliabilityMut.mutate({ feed_url: f.url, title: f.title, tier })
                            }
                          />
                        </td>
                        <td className="px-3 py-2 text-right tnum hidden sm:table-cell"><QualityBadge score={q} /></td>
                        <td className="px-3 py-2 text-right text-fg tnum">{st?.posted_count ?? 0}</td>
                        <td className="px-3 py-2 text-right tnum hidden sm:table-cell">{high > 0 ? <span className="text-critical font-semibold">{high}</span> : <span className="text-fg-subtle">0</span>}</td>
                        <td className="px-3 py-2 text-right tnum hidden sm:table-cell">{med > 0 ? <span className="text-warning">{med}</span> : <span className="text-fg-subtle">0</span>}</td>
                        <td className="px-3 py-2 text-right tnum hidden sm:table-cell">
                          {(() => {
                            const bodyN = (st?.full_body_count ?? 0) + (st?.stump_count ?? 0);
                            if (bodyN === 0) return <span className="text-fg-subtle">—</span>;
                            const rate = st?.full_body_rate ?? 1;
                            const pct = Math.round(rate * 100);
                            const tone = rate >= 0.9 ? "text-success" : rate >= 0.5 ? "text-warning" : "text-critical font-semibold";
                            const stumps = st?.stump_count ?? 0;
                            const label = <span className={tone} title={`全文 ${st?.full_body_count ?? 0} / 切り株 ${stumps} (30日)。クリックで切り株記事一覧`}>{pct}%</span>;
                            // 切り株がある場合はクリックでそのソースの切り株記事一覧へ
                            return stumps > 0
                              ? <a href={`/app/news?feed=${encodeURIComponent(f.title)}&body=stump`} onClick={(e) => e.stopPropagation()} className="hover:underline">{label}</a>
                              : label;
                          })()}
                        </td>
                        <td className="px-3 py-2 text-fg-subtle text-xs hidden lg:table-cell">{formatJstDate(st?.first_seen_at)}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table></div>
            </details>
          );
        })}
      </div>

      {/* Drawer */}
      {open && (
        <Drawer
          isOpen={open !== null}
          onClose={() => setOpen(null)}
          title={open.title}
        >
          <FeedDetailView
            feed={open}
            readOnly={read_only}
            onDeleted={() => {
              setOpen(null);
              qc.invalidateQueries({ queryKey: ["subscriptions"] });
            }}
          />
        </Drawer>
      )}

      {/* Phase F: 統合 Source wizard (readonly では非表示 = write 不可) */}
      {!read_only && (
        <AddSourceWizardV2
          isOpen={sourceWizardOpen}
          onClose={() => {
            setSourceWizardOpen(false);
            setSourceWizardInitialUrl("");
          }}
          initialUrl={sourceWizardInitialUrl}
        />
      )}
    </div>
  );
}

// folder フィルタ chip
function FolderChip({ label, active, onClick }: { label: string; active: boolean; onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      className={`px-2 py-0.5 rounded text-xs border font-mono transition-colors ${
        active
          ? "border-accent text-white bg-accent"
          : "border-border-subtle text-fg-muted bg-surface-2 hover:bg-surface-3"
      }`}
    >
      {label}
    </button>
  );
}

// Phase F: transport を視覚区別するバッジ (統合一覧で「これは scraper」を一目で)
// S3: 信頼度ティアのドロップダウン (raw YAML でなく一覧上で編集)。
// 表示は実効ティア (override→pattern)。auto を選ぶと override 解除し pattern default に戻る。
const _TIER_OPTS: { v: ReliabilityTier; label: string; tone: string }[] = [
  { v: "official", label: "一次", tone: "text-accent" },
  { v: "research", label: "研究", tone: "text-accent" },
  { v: "news", label: "ニュース", tone: "text-fg-muted" },
  { v: "social", label: "SNS", tone: "text-critical" },
  { v: "state_media", label: "国営", tone: "text-warning" },
];

function ReliabilitySelect({
  value,
  disabled,
  onChange,
}: {
  value: string;
  disabled?: boolean;
  onChange: (tier: ReliabilityTier | "auto") => void;
}) {
  const tone = _TIER_OPTS.find((o) => o.v === value)?.tone ?? "text-fg-muted";
  return (
    <select
      value={value}
      disabled={disabled}
      onChange={(e) => onChange(e.target.value as ReliabilityTier | "auto")}
      title="briefing 信頼度ティア。「自動」を選ぶと自動判定に戻す"
      className={`text-[11px] bg-surface-2 border border-border-subtle rounded px-1 py-0.5 ${tone} disabled:opacity-50`}
    >
      {_TIER_OPTS.map((o) => (
        <option key={o.v} value={o.v}>
          {o.label}
        </option>
      ))}
      <option value="auto">自動</option>
    </select>
  );
}

function TransportBadge({ transport }: { transport?: string }) {
  if (!transport || transport === "rss") return null;
  const tone: Record<string, string> = {
    html_scraper: "text-accent bg-accent-subtle border-accent/40",
    sitemap: "text-warning bg-warning-soft border-warning/40",
  };
  const cls = tone[transport] ?? "text-fg-subtle border-border-subtle";
  return (
    <span className={`inline-block mr-2 px-1.5 py-0.5 rounded text-[9px] font-semibold border align-middle ${cls}`}>
      {vocabLabel("transport", transport)}
    </span>
  );
}

// Phase C: Quality badge — 0-100 を色付きで表示
function QualityBadge({ score }: { score: number | undefined }) {
  if (score === undefined) return <span className="text-fg-subtle">-</span>;
  const tone =
    score >= 70
      ? "text-success bg-success-soft border-success/40"
      : score >= 40
      ? "text-warning bg-warning-soft border-warning/40"
      : "text-critical bg-critical-soft border-critical/40";
  return <span className={`inline-block min-w-[2.2rem] text-center px-1.5 py-0.5 rounded text-[10px] font-semibold border ${tone}`}>{score}</span>;
}

// Phase C: Health Card — 全 feed 横断の品質サマリ
interface HealthSummary {
  total: number;
  avgScore: number;
  totalPosted: number;
  totalHigh: number;
  highDupFeeds: number;
  watchOnlyFeeds: number;
  deadFeeds: number;
}

function HealthCard({ summary }: { summary: HealthSummary }) {
  const avgTone =
    summary.avgScore >= 60 ? "text-success" : summary.avgScore >= 40 ? "text-warning" : "text-critical";
  return (
    <div className="bg-surface-1 border border-border-subtle rounded-lg p-3">
      <div className="flex items-baseline justify-between mb-2">
        <h3 className="m-0 text-md font-semibold text-fg flex items-center gap-2">
          <span>購読の健全性 (直近30日)</span>
        </h3>
        <span className="text-xs text-fg-subtle">{summary.total} feeds</span>
      </div>
      <div className="grid grid-cols-2 md:grid-cols-4 gap-2.5">
        <HealthMetric label="平均貢献度" value={summary.avgScore} unit="/100" tone={avgTone} />
        <HealthMetric label="投稿総数" value={summary.totalPosted} tone="text-fg" />
        <HealthMetric label="重要度: 高" value={summary.totalHigh} tone={summary.totalHigh > 0 ? "text-critical font-semibold" : "text-fg-subtle"} />
        <HealthMetric label="停止中フィード" value={summary.deadFeeds} tone={summary.deadFeeds > 0 ? "text-critical" : "text-fg-subtle"} />
        <HealthMetric label="重複の多いフィード" value={summary.highDupFeeds} tone={summary.highDupFeeds > 0 ? "text-warning" : "text-fg-subtle"} />
        <HealthMetric label="watch 止まりのフィード" value={summary.watchOnlyFeeds} tone={summary.watchOnlyFeeds > 0 ? "text-warning" : "text-fg-subtle"} />
      </div>
    </div>
  );
}

function HealthMetric({ label, value, unit, tone }: { label: string; value: number; unit?: string; tone: string }) {
  return (
    <div className="bg-surface-2 rounded p-2">
      <div className="text-[10px] text-fg-muted uppercase tracking-wider">{label}</div>
      <div className={`text-lg tnum font-bold ${tone}`}>
        {value}
        {unit && <span className="text-xs text-fg-subtle ml-0.5">{unit}</span>}
      </div>
    </div>
  );
}

function FeedDetailView({ feed: f, onDeleted, readOnly = false }: { feed: EnrichedFeed; onDeleted: () => void; readOnly?: boolean }) {
  const st = f.stats;
  const chMeta = useChannelMeta();
  const qc = useQueryClient();
  const [editingName, setEditingName] = useState(false);
  const [draftName, setDraftName] = useState(f.title);
  const deleteMut = useMutation({
    mutationFn: () => sourcesV2Api.deleteSource(f.feed_id),
    onSuccess: (res) => {
      if (res.removed) onDeleted();
      else alert(`削除に失敗しました: ${res.error ?? "unknown"}`);
    },
    onError: (e: unknown) => alert(`削除に失敗しました: ${e instanceof Error ? e.message : e}`),
  });
  const renameMut = useMutation({
    mutationFn: (name: string) => sourcesV2Api.setDisplayName(f.feed_id, name),
    onSuccess: (res) => {
      if (res.affected > 0) {
        setEditingName(false);
        qc.invalidateQueries({ queryKey: ["subscriptions"] });
      } else {
        alert(`表示名の変更に失敗しました: ${res.error ?? "対象なし"}`);
      }
    },
    onError: (e: unknown) => alert(`表示名の変更に失敗しました: ${e instanceof Error ? e.message : e}`),
  });
  const toggleMut = useMutation({
    mutationFn: (action: "enable" | "disable") => sourcesV2Api.bulk([f.feed_id], action),
    onSuccess: (res) => {
      if (res.affected > 0) onDeleted(); // close + 一覧 invalidate (状態を反映)
      else alert(`状態変更に失敗しました: ${res.error ?? "対象なし"}`);
    },
    onError: (e: unknown) => alert(`状態変更に失敗しました: ${e instanceof Error ? e.message : e}`),
  });

  function confirmDelete() {
    if (window.confirm(`「${f.title}」を購読から削除しますか?（取り消せません）`)) {
      deleteMut.mutate();
    }
  }

  return (
    <div className="space-y-3">
      <div className="bg-surface-2 rounded p-3 space-y-2">
        <div className="flex items-baseline justify-between gap-2 flex-wrap">
          {editingName ? (
            <div className="flex items-center gap-1.5 flex-1 min-w-[200px]">
              <input
                autoFocus
                value={draftName}
                onChange={(e) => setDraftName(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && draftName.trim()) renameMut.mutate(draftName.trim());
                  if (e.key === "Escape") setEditingName(false);
                }}
                className="flex-1 bg-surface-3 border border-border-default rounded px-2 py-1 text-sm text-fg"
              />
              <button
                onClick={() => draftName.trim() && renameMut.mutate(draftName.trim())}
                disabled={!draftName.trim() || renameMut.isPending}
                className="text-accent hover:underline text-xs disabled:opacity-50"
              >
                {renameMut.isPending ? "保存中…" : "保存"}
              </button>
              <button onClick={() => { setDraftName(f.title); setEditingName(false); }} className="text-fg-subtle hover:text-fg text-xs">
                取消
              </button>
            </div>
          ) : (
            <div className="flex items-center gap-2 min-w-0">
              <h3 className="m-0 text-md font-semibold text-fg truncate">{f.title}</h3>
              {!readOnly && (
                <button
                  onClick={() => { setDraftName(f.title); setEditingName(true); }}
                  title="表示名を変更"
                  className="text-fg-subtle hover:text-accent text-xs shrink-0 inline-flex items-center gap-1"
                >
                  <Pencil className="h-3.5 w-3.5" /> 名称変更
                </button>
              )}
            </div>
          )}
          <a href={f.html_url || f.url} target="_blank" rel="noopener noreferrer" className="text-accent hover:underline text-xs">サイト →</a>
        </div>
        {f.folder_labels.length > 0 && (
          <div className="flex gap-1.5 flex-wrap">
            {f.folder_labels.map((l) => (
              <span key={l} className="text-[10px] uppercase bg-surface-3 text-fg-muted px-1.5 py-0.5 rounded font-mono">{l}</span>
            ))}
          </div>
        )}
        <div className="text-xs text-fg-subtle break-all">{f.url}</div>
      </div>

      {st?.quality_score !== undefined && (
        <div className="bg-surface-2 rounded p-3 space-y-2">
          <h4 className="m-0 text-[10.5px] uppercase tracking-wider text-fg-muted font-semibold">貢献度スコア</h4>
          <div className="flex items-baseline gap-2">
            <QualityBadge score={st.quality_score} />
            <span className="text-fg-subtle text-xs">/100</span>
            {st.low_contrib_labels && st.low_contrib_labels.length > 0 && (
              <div className="flex gap-1 ml-2">
                {st.low_contrib_labels.map((l) => (
                  <span
                    key={l}
                    className={`text-[10px] px-1.5 py-0.5 rounded ${
                      l === "new"
                        ? "bg-accent-subtle text-accent border border-accent/30"
                        : "bg-warning-soft text-warning border border-warning/30"
                    }`}
                  >
                    {LOW_CONTRIB_LABELS[l] ?? l}
                  </span>
                ))}
              </div>
            )}
          </div>
        </div>
      )}

      {st ? (
        <div className="bg-surface-2 rounded p-3 space-y-2">
          <h4 className="m-0 text-[10.5px] uppercase tracking-wider text-fg-muted font-semibold">運用統計 (30 日)</h4>
          <div className="overflow-x-auto"><table className="w-full text-xs">
            <tbody className="[&>tr>th]:text-left [&>tr>th]:text-fg-muted [&>tr>th]:font-normal [&>tr>th]:py-0.5 [&>tr>th]:pr-3 [&>tr>th]:w-32 [&>tr>td]:py-0.5 [&>tr>td]:text-fg [&>tr>td]:tnum">
              <tr><th>投稿数</th><td>{st.posted_count}</td></tr>
              <tr><th>重複でスキップ</th><td>{st.dup_skipped}</td></tr>
              {(st.extract_failed_count ?? 0) > 0 && (
                <tr><th>本文抽出 失敗</th><td className="text-critical font-semibold">{st.extract_failed_count} 件 (feed は取得成功・本文が取れず記事化されない)</td></tr>
              )}
              <tr><th>重要度: 高</th><td className={st.high_count > 0 ? "text-critical font-semibold" : ""}>{st.high_count}</td></tr>
              <tr><th>重要度: 中</th><td className={st.medium_count > 0 ? "text-warning" : ""}>{st.medium_count}</td></tr>
              <tr><th>重要度: 低</th><td>{st.low_count}</td></tr>
              <tr><th>{chMeta("alert").label}</th><td>{st.alert_count}</td></tr>
              <tr><th>{chMeta("brief").label}</th><td>{st.brief_count}</td></tr>
              <tr><th>{chMeta("watch").label}</th><td>{st.watch_count}</td></tr>
              <tr><th>初回検出</th><td className="text-fg-subtle">{formatJstDate(st.first_seen_at)}</td></tr>
            </tbody>
          </table></div>
        </div>
      ) : (
        <div className="text-fg-subtle text-sm italic">この feed の運用統計データはまだありません (新規購読 / 記事なし)。</div>
      )}

      <LivePreviewSection feedId={f.feed_id} />

      <div className="border-t border-border-subtle pt-3 flex items-center justify-between gap-2">
        <span className="text-fg-subtle text-[11px]">
          状態: {f.enabled === false ? "無効" : "有効"}
        </span>
        {readOnly ? (
          <span className="text-[10px] uppercase tracking-wider bg-surface-3 text-fg-subtle px-2 py-1 rounded font-mono">閲覧専用</span>
        ) : (
          <div className="flex items-center gap-2">
            <button
              onClick={() => toggleMut.mutate(f.enabled === false ? "enable" : "disable")}
              disabled={toggleMut.isPending}
              className="border border-border-subtle text-fg hover:bg-surface-3 px-3 py-1.5 rounded text-sm font-semibold disabled:opacity-50 whitespace-nowrap inline-flex items-center gap-1.5"
            >
              {toggleMut.isPending ? "更新中…" : f.enabled === false ? <><Check className="h-3.5 w-3.5" /> 有効化</> : <>⊘ 無効化</>}
            </button>
            <button
              onClick={confirmDelete}
              disabled={deleteMut.isPending}
              className="border border-critical/50 text-critical hover:bg-critical-soft px-3 py-1.5 rounded text-sm font-semibold disabled:opacity-50 whitespace-nowrap inline-flex items-center gap-1.5"
            >
              {deleteMut.isPending ? "削除中…" : <><Trash2 className="h-3.5 w-3.5" /> 削除</>}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

// Phase F: ソースが今も機能しているか最新情報をライブ取得して確認する。
function LivePreviewSection({ feedId }: { feedId: string }) {
  const { data, isFetching, isError, refetch } = useQuery({
    queryKey: ["live_preview", feedId],
    queryFn: () => sourcesV2Api.livePreview(feedId),
    staleTime: 60_000, // 開くたびに連打しない (60s キャッシュ、手動再取得は可)
    retry: false,
  });

  return (
    <div className="bg-surface-2 rounded p-3 space-y-2">
      <div className="flex items-center justify-between">
        <h4 className="m-0 text-[10.5px] uppercase tracking-wider text-fg-muted font-semibold">
          ライブプレビュー (最新取得)
        </h4>
        <button
          onClick={() => refetch()}
          disabled={isFetching}
          className="text-accent hover:underline text-[11px] disabled:text-fg-subtle"
        >
          {isFetching ? "取得中…" : "↻ 再取得"}
        </button>
      </div>

      {isFetching && (
        <div className="flex items-center gap-2 text-xs text-fg-subtle">
          <Spinner /> サイトから最新情報を取得中…
        </div>
      )}

      {!isFetching && (isError || (data && !data.ok)) && (
        <div className="bg-critical-soft border border-critical/40 rounded p-2 text-xs text-fg">
          <span className="inline-flex items-center gap-1"><X className="h-3.5 w-3.5" /> 取得に失敗しました{data?.error ? `: ${data.error}` : ""}</span>
          <div className="text-fg-subtle mt-1">
            登録時から site 構造が変わった / feed が落ちている可能性があります。
          </div>
        </div>
      )}

      {!isFetching && data && data.ok && (
        <>
          <div className="text-xs text-success flex items-center gap-1">
            <Check className="h-3.5 w-3.5" /> {data.items.length} 件取得 ({vocabLabel("transport", data.kind)})
          </div>
          {data.fetch_stage === "browser" && (
            <div className="text-[11px] text-warning">
              bot UA はこのサイトにブロックされるため、ブラウザ相当 UA へ自動切替して取得しました (本番の定期取得も同じ動作)。
            </div>
          )}
          <ul className="divide-y divide-border-subtle border border-border-subtle rounded">
            {data.items.map((a, i) => (
              <li key={i} className="px-2.5 py-1.5 text-xs">
                <a
                  href={a.url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-fg hover:text-accent hover:underline font-medium"
                >
                  {a.title}
                </a>
                {a.published && (
                  <span className="text-fg-subtle ml-2">{formatJstDate(a.published)}</span>
                )}
                <div className="text-[10px] text-fg-subtle font-mono break-all">{a.url}</div>
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}
