// 事象ニュース一覧。**ニュース検索と同じ絞り込み語彙** を持つ (docs/event_news_design.md)。
// 記事側の条件 (pivot / カテゴリ / チャンネル / 期間 / 全文) は backend で既存のニュース検索
// 経路にそのまま渡され、**1 件でも該当メンバーを含む事象** が返る。条件を二重実装しない。
// 選択肢そのものも components/news/facets.tsx をニュース検索と共有する (ラベルを複製しない)。
//
// 記事側から持ち上げた facet に加えて **事象固有の軸** を持つ (複数媒体 / 統合済み /
// 更新あり)。単独報が全体の 9 割を占めるため、この 3 つが母集団を最も大きく動かす。
//
// URL クエリに同期して deep-link 可能にする — エンティティ chip からの逆引き (pivot) が
// この画面へ来られるようにするため。

import { useEffect, useMemo, useState } from "react";
import { useQuery, keepPreviousData } from "@tanstack/react-query";
import { pageContainer } from "../components/Page";
import { Drawer } from "../components/Drawer";
import { formatJstCompact } from "../utils/date";
import { vocabLabel } from "../hooks/useVocab";
import {
  LevelBadge, readSeverityFacet, severityFacetQueryParams, useFacetOptions, writeSeverityFacet,
  type SeverityFacetState,
} from "../components/news/facets";
import { applyRelation, FilterBar, relationFromState } from "../components/news/FilterBar";
import { NewsViewTabs } from "../components/news/NewsViewTabs";
import { EMPTY_FILTERS, type NewsFilters } from "../components/news/views";
import { EventNewsDetailBody, SourceChip } from "./eventnews/EventNewsDetail";
import { fetchEventNews, type EventNewsQuery } from "../api/eventnews";
import { PAGE_TITLE } from "../components/headings";

// 深刻さ・関連性・戦略上の重み (2026-10-04)。既定は「注意以上 + 政策・地政学を含める」
// (旧既定 level_filter="notable"、さらにその前の high+medium を引き継ぐ) —
// 「すべて」まで出すと単独報の参考級が一覧を埋める。
const DEFAULT_EVENT_SEVERITY: SeverityFacetState = {
  minSeverity: "S2",
  relevantOnly: false,
  includeStrategic: false, // 既定は「注意以上 = サイバーの事象だけ」(利用者承認 2026-10-04)。地政学は切り替えで
};

function isDefaultSeverity(s: SeverityFacetState): boolean {
  return (
    s.minSeverity === DEFAULT_EVENT_SEVERITY.minSeverity &&
    s.relevantOnly === DEFAULT_EVENT_SEVERITY.relevantOnly &&
    s.includeStrategic === DEFAULT_EVENT_SEVERITY.includeStrategic
  );
}

// 「新事実あり」= status 'updated' のみ。裏取りが増えただけの 'reinforced' は除く。
//
// ⚠ ここを "updated,reinforced" にすると **「統合済み」と完全に同じ集合になる**
// (2026-08-25 実測: current_version>0 が 279 件、updated+reinforced も 279 件で
//  両方向の差分が 0)。同じ結果を出すボタンを 2 つ並べない。
const NEW_FACTS_STATUS = "updated";

const PAGE_SIZE = 60;

// 「意味も含める」(= semantic) は embedding 計算 (ライブの Ollama) が要るため、
// 静的な写しでは動かない。検索モード select 自体を隠す (押せても何も変わらない
// UI を出さない。FilterBar に searchMode=undefined を渡す、ニュース検索と同じ扱い)。
const MIRROR = import.meta.env.VITE_MIRROR === "1";

/** URL クエリ ⇄ 絞り込み状態。deep-link と戻る操作を壊さない。
 *  深刻さ・関連性・戦略上の重み (2026-10-04): 新 3 facet が無ければ旧 "level_filter" /
 *  更に古い "importance" クエリ (high/medium/low) を移行する (readSeverityFacet)。 */
function readQuery(): EventNewsQuery & { severity: SeverityFacetState } {
  const p = new URLSearchParams(window.location.search);
  const num = (k: string) => Number(p.get(k) || 0) || 0;
  const severity = readSeverityFacet(p, DEFAULT_EVENT_SEVERITY);
  return {
    severity,
    search: p.get("search") ?? undefined,
    category: p.get("category") ?? undefined,
    channel: p.get("channel") ?? undefined,
    feed: p.get("feed") ?? undefined,
    actor: p.get("actor") ?? undefined,
    cve: p.get("cve") ?? undefined,
    malware: p.get("malware") ?? undefined,
    intent: p.get("intent") ?? undefined,
    pir: p.get("pir") ?? undefined,
    affected_vendor: p.get("affected_vendor") ?? undefined,
    jp: (p.get("jp") as "targeted_affected" | "mentioned" | null) ?? undefined,
    sort: p.get("sort") === "level" ? "level" : undefined,
    // エンティティ chip からの逆引き (ニュース検索と同じクエリ名)
    entity_type: p.get("pivot_type") ?? undefined,
    entity_value: p.get("pivot_value") ?? undefined,
    since_hours: num("since_hours"),
    semantic: p.get("semantic") === "1" ? true : undefined,
    // 事象固有の軸
    min_independent_sources: num("min_sources"),
    has_news: p.get("has_news") === "1" ? true : undefined,
    status: p.get("status") ?? undefined,
  };
}

function writeQuery(q: EventNewsQuery & { severity: SeverityFacetState }): void {
  const p = new URLSearchParams();
  if (!isDefaultSeverity(q.severity)) writeSeverityFacet(p, q.severity);
  if (q.search) p.set("search", q.search);
  if (q.since_hours) p.set("since_hours", String(q.since_hours));
  if (q.min_independent_sources) p.set("min_sources", String(q.min_independent_sources));
  if (q.has_news) p.set("has_news", "1");
  if (q.semantic) p.set("semantic", "1");
  for (const k of ["category", "channel", "feed", "actor", "cve", "malware", "intent", "pir", "affected_vendor", "status", "jp", "sort"] as const) {
    if (q[k]) p.set(k, String(q[k]));
  }
  if (q.entity_type && q.entity_value) {
    p.set("pivot_type", q.entity_type);
    p.set("pivot_value", q.entity_value);
  }
  const next = `${window.location.pathname}${p.toString() ? `?${p}` : ""}`;
  if (next !== window.location.pathname + window.location.search) {
    window.history.replaceState(null, "", next);
  }
}

export function EventNewsPage() {
  const facetOpts = useFacetOptions();
  const [openId, setOpenId] = useState<string | null>(null);
  const [q, setQ] = useState(readQuery);
  const [term, setTerm] = useState(q.search ?? "");
  const [vendorRaw, setVendorRaw] = useState(q.affected_vendor ?? "");
  const [page, setPage] = useState(0);

  // ベンダ名は打鍵ごとに投げない (1 回の走査で記事を最大 2,000 件見るため)
  useEffect(() => {
    const t = setTimeout(
      () => setQ((prev) => ({ ...prev, affected_vendor: vendorRaw.trim() || undefined })),
      300,
    );
    return () => clearTimeout(t);
  }, [vendorRaw]);

  // 絞り込みを変えたら 1 ページ目へ戻す (別条件の続きを見せない)
  useEffect(() => {
    setPage(0);
    writeQuery(q);
  }, [q]);

  const { data, isFetching, error } = useQuery({
    queryKey: ["eventnews-list", q, page],
    queryFn: () => {
      const { severity, ...rest } = q;
      return fetchEventNews({
        ...rest,
        ...severityFacetQueryParams(severity),
        limit: PAGE_SIZE,
        offset: page * PAGE_SIZE,
      });
    },
    refetchInterval: 5 * 60 * 1000,
    // ページ送りで一覧が消えないように前ページを保持する
    placeholderData: keepPreviousData,
  });

  const items = data?.items ?? [];
  const set = (patch: Partial<typeof q>) => setQ((prev) => ({ ...prev, ...patch }));

  // ビュー保存・一致判定用の中間表現 (docs/news_filter_ux.md §3)。検索語・pivot・
  // 意味検索の on/off はビューに含めない (絞り込みの組とは別物として扱う)。
  const currentFilters: NewsFilters = useMemo(() => ({
    ...EMPTY_FILTERS,
    minSeverity: q.severity.minSeverity,
    relevantOnly: q.severity.relevantOnly,
    includeStrategic: q.severity.includeStrategic,
    jp: (q.jp as NewsFilters["jp"]) ?? "",
    since: String(q.since_hours ?? 0),
    sort: (q.sort as NewsFilters["sort"]) ?? "",
    category: q.category ?? "",
    feed: q.feed ?? "",
    intent: q.intent ?? "",
    pir: q.pir ?? "",
    actor: q.actor ?? "",
    affectedVendor: q.affected_vendor ?? "",
    channel: q.channel ?? "",
    minIndependentSources: (q.min_independent_sources ?? 0) >= 2,
    hasNews: q.has_news === true,
    newFactsOnly: q.status === NEW_FACTS_STATUS,
  }), [q]);
  function applyView(f: Partial<NewsFilters>): void {
    const next = { ...EMPTY_FILTERS, ...f };
    setQ((prev) => ({
      ...prev,
      severity: { minSeverity: next.minSeverity, relevantOnly: next.relevantOnly, includeStrategic: next.includeStrategic },
      jp: next.jp || undefined,
      since_hours: Number(next.since) || 0,
      sort: next.sort || undefined,
      category: next.category || undefined,
      feed: next.feed || undefined,
      intent: next.intent || undefined,
      pir: next.pir || undefined,
      actor: next.actor || undefined,
      affected_vendor: next.affectedVendor || undefined,
      channel: MIRROR ? prev.channel : next.channel || undefined,
      min_independent_sources: next.minIndependentSources ? 2 : 0,
      has_news: next.hasNews ? true : undefined,
      status: next.newFactsOnly ? NEW_FACTS_STATUS : undefined,
    }));
    setVendorRaw(next.affectedVendor);
  }

  // 記事側の絞り込みが 1 つでも効いているか (deep-link の pivot だけ、ここで残して示す。
  // 他の軸は FilterBar がチップで示す)。
  const activeFilters = useMemo(() => {
    const out: { key: string; value: string }[] = [];
    if (q.cve) out.push({ key: "cve", value: q.cve });
    if (q.malware) out.push({ key: "malware", value: q.malware });
    if (q.entity_type && q.entity_value) {
      out.push({ key: q.entity_type, value: q.entity_value });
    }
    return out;
  }, [q]);

  return (
    <div className={`${pageContainer("wide")} space-y-4`}>
      <div>
        <h2 className={PAGE_TITLE}>事象ニュース</h2>
        <p className="text-fg-muted text-sm mt-1">
          収集した記事を事象単位で読む。複数媒体が報じた事象は 1 本に統合して生成し、新しい記事が加わると更新される。単独報はその記事をそのまま読める。
          <a href="/app/news" className="text-fg-subtle hover:text-accent ml-1 underline">
            ニュース検索へ →
          </a>
        </p>
      </div>

      <NewsViewTabs currentFilters={currentFilters} onApply={applyView} />

      {/* 絞り込みバー。選択肢はニュース検索と共有 (components/news/facets.tsx)。
          検索だけは Enter 確定 — 打鍵ごとに走らせると記事を最大 2,000 件走査する。 */}
      <FilterBar
        facetOpts={facetOpts}
        searchValue={term}
        onSearchChange={setTerm}
        searchPlaceholder={
          MIRROR
            ? "事象を検索 (Enter) — 見出し・要点のみ (本文は対象外)"
            : "事象を検索 (Enter) — 生成本文と構成記事の本文・タイトル"
        }
        onSearchKeyDown={(e) => {
          if (e.key === "Enter") set({ search: term.trim() || undefined });
        }}
        // 検索モード (キーワード / 意味も含める) はニュース検索と共有する統一
        // コントロール (2026-10-04)。旧「意味検索」単独ボタンはこれに統合し、
        // q.semantic の意味論は変えない (URL param "semantic=1" も後方互換のまま)。
        searchMode={MIRROR ? undefined : q.semantic ? "semantic" : "keyword"}
        onSearchMode={MIRROR ? undefined : (v) => set({ semantic: v === "semantic" ? true : undefined })}
        severity={q.severity}
        onSeverity={(v) => set({ severity: v })}
        relation={relationFromState(q.jp ?? "", q.severity.relevantOnly)}
        onRelation={(v) => {
          const next = applyRelation(v);
          set({ jp: next.jp || undefined, severity: { ...q.severity, relevantOnly: next.relevantOnly } });
        }}
        since={String(q.since_hours ?? 0)}
        onSince={(v) => set({ since_hours: Number(v) || 0 })}
        sort={q.sort ?? ""}
        onSort={(v) => set({ sort: v === "level" ? "level" : undefined })}
        category={q.category ?? ""}
        onCategory={(v) => set({ category: v || undefined })}
        feed={q.feed ?? ""}
        onFeed={(v) => set({ feed: v || undefined })}
        intent={q.intent ?? ""}
        onIntent={(v) => set({ intent: v || undefined })}
        pir={q.pir ?? ""}
        onPir={(v) => set({ pir: v || undefined })}
        actor={q.actor ?? ""}
        onActor={(v) => set({ actor: v || undefined })}
        vendorRaw={vendorRaw}
        onVendorRaw={setVendorRaw}
        vendorApplied={q.affected_vendor ?? ""}
        channel={MIRROR ? undefined : { value: q.channel ?? "", onChange: (v) => set({ channel: v || undefined }) }}
        eventOnly={{
          minSources: (q.min_independent_sources ?? 0) >= 2,
          onMinSources: (v) => set({ min_independent_sources: v ? 2 : 0 }),
          hasNews: q.has_news === true,
          onHasNews: (v) => set({ has_news: v ? true : undefined }),
          newFacts: q.status === NEW_FACTS_STATUS,
          onNewFacts: (v) => set({ status: v ? NEW_FACTS_STATUS : undefined }),
        }}
        trailing={
          data && (
            <span className="text-xs text-fg-subtle">
              {page > 0 ? `${page * PAGE_SIZE + 1}–${page * PAGE_SIZE + items.length} 件目` : `${items.length} 件`}
            </span>
          )
        }
      />

      {/* 効いている絞り込みを明示し、1 クリックで外せるようにする */}
      {activeFilters.length > 0 && (
        <div className="flex flex-wrap items-center gap-1.5 text-xs">
          <span className="text-fg-subtle">絞り込み:</span>
          {activeFilters.map((f) => (
            <span
              key={`${f.key}:${f.value}`}
              className="inline-flex items-center gap-1 bg-accent/10 border border-accent-soft rounded px-2 py-0.5 text-accent"
            >
              {vocabLabel("entity_type", f.key) || f.key}: {f.value}
            </span>
          ))}
          <button
            onClick={() => {
              setTerm("");
              setVendorRaw("");
              setQ({
                severity: q.severity,
                since_hours: q.since_hours,
                // 事象固有の軸は別行の操作なので巻き添えで消さない
                min_independent_sources: q.min_independent_sources,
                has_news: q.has_news,
                status: q.status,
              });
            }}
            className="text-fg-subtle hover:text-accent underline"
          >
            解除
          </button>
        </div>
      )}

      {data?.scan_capped && (
        <div className="text-warning text-xs bg-warning-soft border border-warning/40 rounded px-3 py-2">
          該当する記事が多いため、走査を打ち切っています。絞り込みを足すと取りこぼしがなくなります。
        </div>
      )}

      {isFetching && !data && <div className="text-fg-subtle text-sm">読み込み中…</div>}
      {error && <div className="text-critical text-sm">エラー: {String(error)}</div>}
      {data && items.length === 0 && (
        <div className="text-fg-muted text-sm border border-dashed border-border-default rounded-lg p-6 text-center">
          該当する事象がありません。
        </div>
      )}

      {/* 記事 1 件ずつを箱で囲わず、罫線で区切る (公開サイト・ニュース検索と同じ)。 */}
      <ul className="divide-y divide-border-subtle">
        {items.map((it) => (
          <li
            key={it.id}
            className="flex items-start gap-2 px-1 py-2.5 hover:bg-surface-1"
          >
            <span
              className={`mt-1.5 w-1.5 h-1.5 rounded-full shrink-0 ${
                it.importance === "high" ? "bg-critical" : it.importance === "medium" ? "bg-warning" : "bg-fg-subtle"
              }`}
            />
            <div className="flex-1 min-w-0">
              <button
                onClick={() => setOpenId(it.id)}
                className="block w-full text-left text-[15.5px] font-semibold leading-[1.6] text-fg hover:text-accent hover:underline"
                title="事象の詳細を開く"
              >
                {it.headline}
              </button>
              {it.preview && (
                <p className="text-[13.5px] text-fg-muted leading-[1.75] mt-1 line-clamp-3">{it.preview}</p>
              )}
              <div className="text-[13px] flex flex-wrap items-center gap-x-1.5 gap-y-1 mt-1">
                <SourceChip item={it} />
                <LevelBadge level={it.level} />
                {it.member_count > 1 && (
                  <span className="px-1 rounded bg-surface-2 text-fg-muted">{it.member_count} 記事を統合</span>
                )}
                {it.status === "updated" && (
                  <span className="px-1 rounded bg-accent/15 text-accent">更新</span>
                )}
                {it.best_source_tier && it.best_source_tier !== "news" && (
                  <span className="px-1 rounded bg-surface-2 text-fg-muted">{it.best_source_tier}</span>
                )}
                <span className="ml-auto shrink-0 text-fg-subtle" title="最新報道の時刻">
                  {formatJstCompact(it.last_reported_at)}
                </span>
              </div>
            </div>
          </li>
        ))}
      </ul>

      {/* ページ送り。件数が読めないと「これで全部か」が分からないため、
          次ページの有無は「満杯かどうか」で判断する (総件数の COUNT は打たない)。 */}
      {(page > 0 || items.length === PAGE_SIZE) && (
        <div className="flex items-center gap-2 pt-1">
          <button
            disabled={page === 0}
            onClick={() => setPage((n) => Math.max(0, n - 1))}
            className="text-xs px-3 py-1.5 rounded border border-border-default text-fg-muted hover:text-accent hover:border-accent-soft disabled:opacity-40 disabled:hover:text-fg-muted transition-colors"
          >
            ← 前へ
          </button>
          <span className="text-xs text-fg-subtle tnum">{page + 1} ページ目</span>
          <button
            disabled={items.length < PAGE_SIZE}
            onClick={() => setPage((n) => n + 1)}
            className="text-xs px-3 py-1.5 rounded border border-border-default text-fg-muted hover:text-accent hover:border-accent-soft disabled:opacity-40 disabled:hover:text-fg-muted transition-colors"
          >
            次へ →
          </button>
        </div>
      )}

      <Drawer
        isOpen={openId !== null}
        onClose={() => setOpenId(null)}
        // ⚠ ドロワーの題に見出しを入れない。中身の側にも見出しが出るので
        //    同じ文が 2 回並ぶ (公開ページは「記事」と総称にしている)。
        title="事象ニュース"
        widthClass="md:w-[46rem]"
        mobileGutter
        swipeToClose
      >
        {openId && <EventNewsDetailBody id={openId} onOpenItem={setOpenId} />}
      </Drawer>
    </div>
  );
}
