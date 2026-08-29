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
import { Sel, SINCE_OPTS, useFacetOptions, VendorInput } from "../components/news/facets";
import { EventNewsDetailBody, SourceChip } from "./eventnews/EventNewsDetail";
import { fetchEventNews, type EventNewsQuery } from "../api/eventnews";
import { PAGE_TITLE } from "../components/headings";

// 事象の重要度は複数指定 (カンマ区切り) を使うため、記事側の IMPORTANCE_OPTS とは別定義。
// 既定は high+medium — low まで出すと単独報の低重要度が一覧を埋める。
const IMPORTANCE_OPTS: { value: string; label: string }[] = [
  { value: "high", label: "high のみ" },
  { value: "high,medium", label: "high + medium" },
  { value: "", label: "全重要度" },
];

// 「新事実あり」= status 'updated' のみ。裏取りが増えただけの 'reinforced' は除く。
//
// ⚠ ここを "updated,reinforced" にすると **「統合済み」と完全に同じ集合になる**
// (2026-08-25 実測: current_version>0 が 279 件、updated+reinforced も 279 件で
//  両方向の差分が 0)。同じ結果を出すボタンを 2 つ並べない。
const NEW_FACTS_STATUS = "updated";

const PAGE_SIZE = 60;

/** URL クエリ ⇄ 絞り込み状態。deep-link と戻る操作を壊さない。 */
function readQuery(): EventNewsQuery & { importance: string } {
  const p = new URLSearchParams(window.location.search);
  const num = (k: string) => Number(p.get(k) || 0) || 0;
  return {
    importance: p.get("importance") ?? "high,medium",
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

function writeQuery(q: EventNewsQuery & { importance: string }): void {
  const p = new URLSearchParams();
  if (q.importance !== "high,medium") p.set("importance", q.importance);
  if (q.search) p.set("search", q.search);
  if (q.since_hours) p.set("since_hours", String(q.since_hours));
  if (q.min_independent_sources) p.set("min_sources", String(q.min_independent_sources));
  if (q.has_news) p.set("has_news", "1");
  if (q.semantic) p.set("semantic", "1");
  for (const k of ["category", "channel", "feed", "actor", "cve", "malware", "intent", "pir", "affected_vendor", "status"] as const) {
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
    queryFn: () =>
      fetchEventNews({ ...q, limit: PAGE_SIZE, offset: page * PAGE_SIZE }),
    refetchInterval: 5 * 60 * 1000,
    // ページ送りで一覧が消えないように前ページを保持する
    placeholderData: keepPreviousData,
  });

  const items = data?.items ?? [];
  const openItem = items.find((i) => i.id === openId) ?? null;
  const set = (patch: Partial<typeof q>) => setQ((prev) => ({ ...prev, ...patch }));

  // 記事側の絞り込みが 1 つでも効いているか (解除ボタンの出し分け)
  const activeFilters = useMemo(() => {
    const keys = [
      "search", "category", "channel", "feed", "actor", "cve",
      "malware", "intent", "pir", "affected_vendor",
    ] as const;
    const out: { key: string; value: string }[] = [];
    for (const k of keys) {
      const v = q[k];
      if (v) out.push({ key: k, value: String(v) });
    }
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

      {/* 絞り込みバー。選択肢はニュース検索と共有 (components/news/facets.tsx)。
          検索だけは Enter 確定 — 打鍵ごとに走らせると記事を最大 2,000 件走査する。 */}
      <div className="md:sticky md:top-12 z-20 bg-bg/95 backdrop-blur-md border-b border-border-subtle -mx-4 px-4 md:mx-0 md:px-0 py-2 flex flex-wrap items-center gap-2">
        <input
          value={term}
          onChange={(e) => setTerm(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") set({ search: term.trim() || undefined });
          }}
          placeholder="事象を検索 (Enter) — 生成本文と構成記事の本文・タイトル"
          className="h-8 px-3 bg-surface-2 border border-border-subtle rounded-md text-sm min-w-[180px] flex-1 max-w-[320px] placeholder:text-fg-subtle focus:outline-none focus:border-accent"
        />
        {/* 意味検索。語句検索と OR で足す (言い換え・多言語を拾う)。
            実測: 語句 0 件のクエリでも 20 件出ることがある。 */}
        <button
          onClick={() => set({ semantic: q.semantic ? undefined : true })}
          aria-pressed={q.semantic === true}
          title="言い換えや多言語の記事も拾う (embedding で類似検索)"
          className={`h-8 px-3 rounded-md border text-sm transition-colors ${
            q.semantic
              ? "border-accent text-accent bg-accent/10"
              : "border-border-subtle text-fg-muted hover:text-fg"
          }`}
        >
          意味検索
        </button>
        <Sel value={q.category ?? ""} onChange={(v) => set({ category: v || undefined })} opts={facetOpts.category} />
        <Sel value={q.feed ?? ""} onChange={(v) => set({ feed: v || undefined })} opts={facetOpts.feed} />
        <Sel value={q.channel ?? ""} onChange={(v) => set({ channel: v || undefined })} opts={facetOpts.channel} />
        <Sel value={q.importance} onChange={(v) => set({ importance: v })} opts={IMPORTANCE_OPTS} />
        <Sel value={q.intent ?? ""} onChange={(v) => set({ intent: v || undefined })} opts={facetOpts.intent} />
        <Sel value={q.pir ?? ""} onChange={(v) => set({ pir: v || undefined })} opts={facetOpts.pir} />
        <Sel value={q.actor ?? ""} onChange={(v) => set({ actor: v || undefined })} opts={facetOpts.actor} />
        <VendorInput
          raw={vendorRaw}
          onChange={setVendorRaw}
          applied={q.affected_vendor ?? ""}
          options={facetOpts.vendor}
          listId="eventnews-vendor-list"
        />
        <Sel
          value={String(q.since_hours ?? 0)}
          onChange={(v) => set({ since_hours: Number(v) || 0 })}
          opts={SINCE_OPTS}
        />
      </div>

      {/* 事象固有の軸。記事側には存在しないので facet バーとは分けて置く。
          単独報が 9 割を占めるため、この 3 つが母集団を最も大きく動かす。 */}
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="text-xs text-fg-subtle mr-0.5">事象:</span>
        <Toggle
          on={(q.min_independent_sources ?? 0) >= 2}
          onClick={() => set({ min_independent_sources: (q.min_independent_sources ?? 0) >= 2 ? 0 : 2 })}
          title="独立した 2 媒体以上が報じた事象だけを表示する (同一媒体の連投は数えない)"
        >
          複数媒体
        </Toggle>
        <Toggle
          on={q.has_news === true}
          onClick={() => set({ has_news: q.has_news ? undefined : true })}
          title="kuebiko が複数記事から生成した統合本文を持つ事象だけを表示する"
        >
          統合済み
        </Toggle>
        <Toggle
          on={q.status === NEW_FACTS_STATUS}
          onClick={() => set({ status: q.status === NEW_FACTS_STATUS ? undefined : NEW_FACTS_STATUS })}
          title="初報のあと新しい事実 (新 CVE / 媒体増 / tier 上昇 / 重要度上昇) が加わった事象。裏取りが増えただけのものは含まない"
        >
          新事実あり
        </Toggle>
        {data && (
          <span className="ml-auto self-center text-xs text-fg-subtle">
            {page > 0 ? `${page * PAGE_SIZE + 1}–${page * PAGE_SIZE + items.length} 件目` : `${items.length} 件`}
          </span>
        )}
      </div>

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
                importance: q.importance,
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
        title={openItem?.headline ?? "事象"}
        widthClass="md:w-[46rem]"
        mobileGutter
        swipeToClose
      >
        {openId && <EventNewsDetailBody id={openId} />}
      </Drawer>
    </div>
  );
}

/** 事象固有の軸の on/off。select と違い「効いている状態」が一目で分かる必要がある。 */
function Toggle({
  on,
  onClick,
  title,
  children,
}: {
  on: boolean;
  onClick: () => void;
  title: string;
  children: React.ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      title={title}
      aria-pressed={on}
      className={`text-xs px-3 py-1.5 rounded border transition-colors ${
        on
          ? "border-accent text-accent bg-accent/10"
          : "border-border-default text-fg-muted hover:text-fg"
      }`}
    >
      {children}
    </button>
  );
}
