// 事象ニュース一覧。**ニュース検索と同じ絞り込み語彙** を持つ (docs/event_news_design.md)。
// 記事側の条件 (pivot / カテゴリ / チャンネル / 期間 / 全文) は backend で既存のニュース検索
// 経路にそのまま渡され、**1 件でも該当メンバーを含む事象** が返る。条件を二重実装しない。
//
// URL クエリに同期して deep-link 可能にする — エンティティ chip からの逆引き (pivot) が
// この画面へ来られるようにするため。

import { useEffect, useMemo, useState } from "react";
import { useQuery, keepPreviousData } from "@tanstack/react-query";
import { pageContainer } from "../components/Page";
import { Drawer } from "../components/Drawer";
import { formatJstCompact } from "../utils/date";
import { vocabLabel } from "../hooks/useVocab";
import { EventNewsDetailBody, SourceChip } from "./eventnews/EventNewsDetail";
import { fetchEventNews, type EventNewsQuery } from "../api/eventnews";

const IMPORTANCE_FILTERS: { key: string; label: string }[] = [
  { key: "high", label: "high のみ" },
  { key: "high,medium", label: "high + medium" },
  { key: "", label: "すべて" },
];

const PERIOD_FILTERS: { hours: number; label: string }[] = [
  { hours: 0, label: "全期間" },
  { hours: 24, label: "24 時間" },
  { hours: 24 * 7, label: "7 日" },
  { hours: 24 * 30, label: "30 日" },
];

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
  };
}

function writeQuery(q: EventNewsQuery & { importance: string }): void {
  const p = new URLSearchParams();
  if (q.importance !== "high,medium") p.set("importance", q.importance);
  if (q.search) p.set("search", q.search);
  if (q.since_hours) p.set("since_hours", String(q.since_hours));
  for (const k of ["category", "channel", "actor", "cve", "malware", "intent", "pir", "affected_vendor"] as const) {
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
  const [openId, setOpenId] = useState<string | null>(null);
  const [q, setQ] = useState(readQuery);
  const [term, setTerm] = useState(q.search ?? "");
  const [page, setPage] = useState(0);

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
      "search", "category", "channel", "actor", "cve",
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
        <h2 className="m-0 text-xl font-bold text-fg tracking-tight">事象ニュース</h2>
        <p className="text-fg-muted text-sm mt-1">
          収集した記事を事象単位で読む。複数媒体が報じた事象は 1 本に統合して生成し、新しい記事が加わると更新される。単独報はその記事の要約を表示する。
        </p>
      </div>

      {/* 検索 (記事本文まで含めてニュース検索と同じ経路で解決される) */}
      <div className="flex flex-wrap gap-2">
        <input
          value={term}
          onChange={(e) => setTerm(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") set({ search: term.trim() || undefined });
          }}
          placeholder="事象を検索 (Enter) — 構成記事の本文・タイトルを対象"
          className="flex-1 min-w-[16rem] bg-surface-1 border border-border-default rounded px-3 py-1.5 text-sm text-fg placeholder:text-fg-subtle focus:border-accent outline-none"
        />
        <button
          onClick={() => set({ search: term.trim() || undefined })}
          className="text-xs px-3 py-1.5 rounded border border-border-default text-fg-muted hover:text-accent hover:border-accent-soft transition-colors"
        >
          検索
        </button>
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
              setQ({ importance: q.importance, since_hours: q.since_hours });
            }}
            className="text-fg-subtle hover:text-accent underline"
          >
            解除
          </button>
        </div>
      )}

      <div className="flex flex-wrap gap-1.5">
        {PERIOD_FILTERS.map((f) => (
          <button
            key={f.hours}
            onClick={() => set({ since_hours: f.hours })}
            className={`text-xs px-3 py-1.5 rounded border transition-colors ${
              (q.since_hours ?? 0) === f.hours
                ? "border-accent text-accent bg-accent/10"
                : "border-border-default text-fg-muted hover:text-fg"
            }`}
          >
            {f.label}
          </button>
        ))}
      </div>

      <div className="flex gap-1.5">
        {IMPORTANCE_FILTERS.map((f) => (
          <button
            key={f.key}
            onClick={() => set({ importance: f.key })}
            className={`text-xs px-3 py-1.5 rounded border transition-colors ${
              q.importance === f.key
                ? "border-accent text-accent bg-accent/10"
                : "border-border-default text-fg-muted hover:text-fg"
            }`}
          >
            {f.label}
          </button>
        ))}
        {data && (
          <span className="ml-auto self-center text-xs text-fg-subtle">
            {page > 0 ? `${page * PAGE_SIZE + 1}–${page * PAGE_SIZE + items.length} 件目` : `${items.length} 件`}
          </span>
        )}
      </div>

      {data?.scan_capped && (
        <div className="text-warning text-xs bg-warning-soft border border-warning/40 rounded px-3 py-2">
          該当する記事が多いため、走査を打ち切っています。絞り込みを足すと取りこぼしがなくなります。
        </div>
      )}

      {isFetching && !data && <div className="text-fg-subtle text-sm">読み込み中…</div>}
      {error && <div className="text-critical text-sm">エラー: {String(error)}</div>}
      {data && items.length === 0 && (
        <div className="text-fg-muted text-sm bg-surface-1 border border-border-subtle rounded-lg p-4">
          該当する事象がありません。
        </div>
      )}

      <ul className="space-y-1.5">
        {items.map((it) => (
          <li
            key={it.id}
            className="flex items-start gap-2 bg-surface-1 border border-border-subtle rounded-lg px-3 py-2.5"
          >
            <span
              className={`mt-1.5 w-1.5 h-1.5 rounded-full shrink-0 ${
                it.importance === "high" ? "bg-critical" : it.importance === "medium" ? "bg-warning" : "bg-fg-subtle"
              }`}
            />
            <div className="flex-1 min-w-0">
              <button
                onClick={() => setOpenId(it.id)}
                className="block w-full text-left text-sm font-medium leading-snug text-fg hover:text-accent hover:underline"
                title="事象の詳細を開く"
              >
                {it.headline}
              </button>
              {it.preview && (
                <p className="text-xs text-fg-muted leading-relaxed mt-1 line-clamp-4">{it.preview}</p>
              )}
              <div className="text-[11px] flex flex-wrap items-center gap-x-1.5 gap-y-1 mt-1">
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
