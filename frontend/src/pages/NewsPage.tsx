// 記事サーフェス (統合): 閲覧・検索・逆引きを 1 画面に集約。
// - 検索 box 空 → 閲覧 (/api/v1/articles の軽い SQL filter + facet)。
// - box にテキスト → 強力検索 (/api/v1/search, hybrid + LLM rerank) を **facet 込み** で実行。
//   入力 debounce では quick (融合スコア順, ~1-2s)、「精密 (LLM)」ON で precise に格上げ。
// - box に CVE/IP/ドメイン/ハッシュ、または entity deep-link / 共起 click → 逆引き (pivot)。
// facet (category/feed/channel/importance/intent/期間 + cve/malware タグ) は閲覧・検索の
// 双方に AND 合成される。フィルタ/検索/pivot は URL クエリに同期し deep-link 可能。

import { useEffect, useMemo, useState, type ReactNode } from "react";
import { Check } from "lucide-react";
import { pageContainer } from "../components/Page";
import { useQuery, keepPreviousData } from "@tanstack/react-query";
import { articlesApi } from "../api/articles";
import { fetchSearch, type SearchFacets } from "../api/search";
import { fetchPivot } from "../api/pivot";
import {
  EMPTY_SEVERITY_FACET, LevelBadge, readSeverityFacet, useFacetOptions,
  severityFacetQueryParams, writeSeverityFacet, type SeverityFacetState,
} from "../components/news/facets";
import { applyRelation, FilterBar, relationFromState } from "../components/news/FilterBar";
import { NewsViewTabs } from "../components/news/NewsViewTabs";
import { EMPTY_FILTERS, type NewsFilters } from "../components/news/views";
import { SearchResults } from "../components/news/SearchResults";
import { PivotResults } from "../components/news/PivotResults";
import { extractCves } from "./dashboard/shared";
import { formatJstCompact } from "../utils/date";
import { usePersistedState } from "../utils/usePersistedState";
import { hasIntent, intentLabel, isHypothesisIntent } from "../utils/diamond";
import { useChannelMeta } from "../components/channel";
import { vocabLabel } from "../hooks/useVocab";
import { sectorLabel } from "../components/geo/sectorColors";
import { countryLabel } from "../utils/countryLabels";
import { PAGE_TITLE } from "../components/headings";

// 写し (Cloudflare Pages) は購読チャンネルの区別を持たない運用面の軸なので、
// 一般公開に近い写しの画面からは外す (ops では「日本との関係」の後ろに残す)。
const MIRROR = import.meta.env.VITE_MIRROR === "1";

// 構造化エンティティ (CVE/IP/ドメイン/ハッシュ) を検出 → 逆引きへ自動ルート。
function detectEntity(raw: string): { type: string; value: string } | null {
  const s = raw.trim();
  if (!s || /\s/.test(s)) return null;
  if (/^CVE-\d{4}-\d{4,7}$/i.test(s)) return { type: "cve", value: s.toUpperCase() };
  if (/^(\d{1,3}\.){3}\d{1,3}$/.test(s)) return { type: "ioc_ip", value: s };
  if (/^[a-f0-9]{64}$/i.test(s)) return { type: "ioc_sha256", value: s.toLowerCase() };
  if (/^[a-f0-9]{40}$/i.test(s)) return { type: "ioc_sha1", value: s.toLowerCase() };
  if (/^[a-f0-9]{32}$/i.test(s)) return { type: "ioc_md5", value: s.toLowerCase() };
  if (/^(?=.{4,253}$)([a-z0-9-]+\.)+[a-z]{2,}$/i.test(s)) return { type: "ioc_domain", value: s.toLowerCase() };
  return null;
}

interface Pivot { type: string; value: string }

interface NewsState {
  category: string; channel: string; severity: SeverityFacetState; feed: string; since: string;
  search: string; malware: string; cve: string; intent: string; pir: string; actor: string; vendor: string;
  body: string; // "" / "stump"(切り株) / "full"(全文取得済)
  jp: string; // "" / "targeted_affected" / "mentioned" (日本との関係)
  sort: string; // "" / "level" (重要度順、2026-10-04)
  mode: "headline" | "summary"; precise: boolean; pivot: Pivot | null;
}

function readState(): NewsState {
  const p = new URLSearchParams(typeof window !== "undefined" ? window.location.search : "");
  const pt = p.get("pivot_type");
  const pv = p.get("pivot_value");
  // 深刻さ・関連性・戦略上の重み (2026-10-04)。既定は「すべて」— ニュース検索は絞らない
  // ところから探索を始める (事象ニュースの既定「注意以上」とは違う)。旧 level_filter /
  // importance (high/medium/low) の deep-link は新 facet が無いときだけ移行する。
  const severity = readSeverityFacet(p, EMPTY_SEVERITY_FACET);
  return {
    category: p.get("category") ?? "",
    channel: p.get("channel") ?? "",
    severity,
    feed: p.get("feed") ?? "",
    since: p.get("since") ?? "0",
    search: p.get("search") ?? "",
    malware: p.get("malware") ?? "",
    cve: p.get("cve") ?? "",
    intent: p.get("intent") ?? "",
    pir: p.get("pir") ?? "",
    actor: p.get("actor") ?? "",
    vendor: p.get("affected_vendor") ?? "",
    body: p.get("body") ?? "",
    jp: p.get("jp") ?? "",
    sort: p.get("sort") === "level" ? "level" : "",
    mode: p.get("mode") === "summary" ? "summary" : "headline",
    precise: p.get("precise") === "1",
    pivot: pt && pv ? { type: pt, value: pv } : null,
  };
}

function writeState(s: NewsState): void {
  if (typeof window === "undefined") return;
  const q = new URLSearchParams();
  if (s.category) q.set("category", s.category);
  if (s.channel) q.set("channel", s.channel);
  writeSeverityFacet(q, s.severity);
  if (s.feed) q.set("feed", s.feed);
  if (s.since && s.since !== "0") q.set("since", s.since);
  if (s.search) q.set("search", s.search);
  if (s.malware) q.set("malware", s.malware);
  if (s.cve) q.set("cve", s.cve);
  if (s.intent) q.set("intent", s.intent);
  if (s.pir) q.set("pir", s.pir);
  if (s.actor) q.set("actor", s.actor);
  if (s.vendor) q.set("affected_vendor", s.vendor);
  if (s.body) q.set("body", s.body);
  if (s.jp) q.set("jp", s.jp);
  if (s.sort) q.set("sort", s.sort);
  if (s.mode === "summary") q.set("mode", "summary");
  if (s.precise) q.set("precise", "1");
  if (s.pivot) { q.set("pivot_type", s.pivot.type); q.set("pivot_value", s.pivot.value); }
  const qs = q.toString();
  window.history.replaceState(null, "", qs ? `/app/news?${qs}` : "/app/news");
}


// 公開から取得までの遅れがこれを超えたら取得時刻も併記する。実測 (直近 30 日 6,343 件)
// では中央値 0.9h・6h 超が 10% なので、常時併記は 90% の記事で無駄な情報になる。
const LATE_INGEST_HOURS = 6;

function isLateIngest(published?: string | null, created?: string | null): boolean {
  if (!published || !created) return false;
  const lag = new Date(created).getTime() - new Date(published).getTime();
  return lag > LATE_INGEST_HOURS * 3600_000;
}

export function NewsPage() {
  const chMeta = useChannelMeta();
  // facet 選択肢は事象ニュースと共有する (ラベル辞書を 2 箇所に持たない)。
  const facetOpts = useFacetOptions();
  const init = readState();
  const [category, setCategory] = useState(init.category);
  const [channel, setChannel] = useState(init.channel);
  const [severity, setSeverity] = useState<SeverityFacetState>(init.severity);
  const [feed, setFeed] = useState(init.feed);
  const [since, setSince] = useState(init.since);
  const [mode, setMode] = useState<"headline" | "summary">(init.mode);
  const [malware, setMalware] = useState(init.malware);
  const [cve, setCve] = useState(init.cve);
  const [intent, setIntent] = useState(init.intent);
  const [pir, setPir] = useState(init.pir);
  const [actor, setActor] = useState(init.actor);
  const [body, setBody] = useState(init.body);
  const [jp, setJp] = useState(init.jp);
  const [sort, setSort] = useState(init.sort);
  const [vendorRaw, setVendorRaw] = useState(init.vendor);
  const [vendor, setVendor] = useState(init.vendor);
  const [precise, setPrecise] = useState(init.precise);
  const [pivot, setPivot] = useState<Pivot | null>(init.pivot);
  const [searchRaw, setSearchRaw] = useState(init.search);
  const [search, setSearch] = useState(init.search);
  const [limit, setLimit] = useState(30);
  // W2: 「前回確認以降の新着」surface。cursor は localStorage 永続 (desktop triage 主体、
  // readonly/mobile は write 不可で silent fall-back)。newOnly はモード sticky。
  const [lastSeen, setLastSeen] = usePersistedState<string | null>("news-last-seen", null);
  const [newOnly, setNewOnly] = usePersistedState<boolean>("news-new-only", false);

  useEffect(() => {
    const t = setTimeout(() => setSearch(searchRaw.trim()), 300);
    return () => clearTimeout(t);
  }, [searchRaw]);
  useEffect(() => {
    const t = setTimeout(() => setVendor(vendorRaw.trim()), 300);
    return () => clearTimeout(t);
  }, [vendorRaw]);
  useEffect(() => { setLimit(30); }, [category, channel, severity, feed, since, search, malware, cve, intent, pir, actor, vendor, body, jp, sort, newOnly, lastSeen]);
  useEffect(() => {
    writeState({ category, channel, severity, feed, since, search, malware, cve, intent, pir, actor, vendor, body, jp, sort, mode, precise, pivot });
  }, [category, channel, severity, feed, since, search, malware, cve, intent, pir, actor, vendor, body, jp, sort, mode, precise, pivot]);

  // facet (閲覧・検索で共有する AND 条件)。空値は undefined にして送らない。
  const facets: SearchFacets = useMemo(() => ({
    ...severityFacetQueryParams(severity),
    category: category || undefined,
    feed: feed || undefined,
    channel: channel || undefined,
    cve: cve || undefined,
    malware: malware || undefined,
    intent: intent || undefined,
    pir: pir || undefined,
    actor: actor || undefined,
    affected_vendor: vendor || undefined,
    body: body || undefined,
    jp: (jp as SearchFacets["jp"]) || undefined,
    since_hours: Number(since) || undefined,
  }), [severity, category, feed, channel, cve, malware, intent, pir, actor, vendor, body, jp, since]);

  // ビュー判定: 明示 pivot > box の構造化エンティティ自動逆引き > テキスト検索 > 閲覧。
  const autoPivot = useMemo(() => (pivot == null && search ? detectEntity(search) : null), [pivot, search]);
  const activePivot = pivot ?? autoPivot;
  const view: "browse" | "search" | "pivot" = activePivot ? "pivot" : search ? "search" : "browse";

  // W2: 新着モードの時間絞り込み。cursor あり→絶対 since(=「前回確認以降」)、cursor 未設定→
  // 直近 24h を暫定表示 (『ここまで既読』で基準を作るまでの足場)。新着 off→従来の since_hours。
  const browseSince = newOnly ? (lastSeen ?? undefined) : undefined;
  const browseSinceHours = newOnly ? (lastSeen ? undefined : 24) : facets.since_hours;

  // --- 閲覧 (browse) ---
  // sort は閲覧 (/api/v1/articles) のみ対応。検索 (/api/v1/search) は hybrid
  // retrieval + rerank の融合スコア順が前提のため、並び替えの対象にしない。
  const browseQ = useQuery({
    queryKey: ["news-browse", facets, mode, sort, limit, newOnly, lastSeen],
    queryFn: () => articlesApi.list({
      ...facets,
      since_hours: browseSinceHours,
      since: browseSince,
      status: "posted",
      sort: sort === "level" ? "level" : undefined,
      include_summary: mode === "summary",
      limit,
    }),
    enabled: view === "browse",
    refetchInterval: 2 * 60_000,
    // 「さらに読み込む」(limit 変更) で旧データを保持し、リストが一瞬空になって
    // スクロール位置が先頭に飛ぶのを防ぐ (先頭 N 件の DOM が安定 → 続けて読める)。
    placeholderData: keepPreviousData,
  });
  const arts = browseQ.data?.articles ?? [];
  const canLoadMore = view === "browse" && arts.length >= limit && limit < 200;

  // --- 検索 (search, progressive quick→precise) ---
  const quickQ = useQuery({
    queryKey: ["news-search-quick", search, facets],
    queryFn: () => fetchSearch(search, "quick", facets),
    enabled: view === "search",
    retry: false,
  });
  const preciseQ = useQuery({
    queryKey: ["news-search-precise", search, facets],
    queryFn: () => fetchSearch(search, "precise", facets),
    enabled: view === "search" && precise,
    retry: false,
  });
  const searchData = precise && preciseQ.data ? preciseQ.data : quickQ.data;
  const precising = precise && view === "search" && (preciseQ.isFetching || preciseQ.isPending) && !preciseQ.data;

  // --- 逆引き (pivot) ---
  const pivotQ = useQuery({
    queryKey: ["news-pivot", activePivot?.type ?? "", activePivot?.value ?? ""],
    queryFn: () => fetchPivot(activePivot!.type, activePivot!.value),
    enabled: view === "pivot",
    retry: false,
  });

  function onSearchInput(v: string): void {
    setSearchRaw(v);
    if (pivot) setPivot(null); // テキスト入力は明示 pivot を解除 (新しい意図)
  }
  function runPivot(type: string, value: string): void {
    setSearchRaw(""); setSearch(""); setPivot({ type, value }); // 共起 click → 再 pivot
  }
  function clearPivot(): void { setPivot(null); }
  // W2: 「ここまで既読」= 新着カーソルを現在時刻へ進める (= ここまでレビュー済)。
  function markCaughtUp(): void { setLastSeen(new Date().toISOString()); }

  const hasActiveTag = Boolean(malware || cve);
  // ビュー保存・一致判定用の中間表現 (docs/news_filter_ux.md §3)。
  const currentFilters: NewsFilters = useMemo(() => ({
    ...EMPTY_FILTERS,
    search: "", // 検索語はビューに含めない (絞り込みの組とは別物として扱う)
    minSeverity: severity.minSeverity,
    relevantOnly: severity.relevantOnly,
    includeStrategic: severity.includeStrategic,
    jp: jp as NewsFilters["jp"],
    since,
    sort: sort as NewsFilters["sort"],
    category, feed, intent, pir, actor,
    affectedVendor: vendor,
    body, channel,
  }), [severity, jp, since, sort, category, feed, intent, pir, actor, vendor, body, channel]);
  function applyView(f: Partial<NewsFilters>): void {
    const next = { ...EMPTY_FILTERS, ...f };
    setSeverity({ minSeverity: next.minSeverity, relevantOnly: next.relevantOnly, includeStrategic: next.includeStrategic });
    setJp(next.jp);
    setSince(next.since);
    setSort(next.sort);
    setCategory(next.category);
    setFeed(next.feed);
    setIntent(next.intent);
    setPir(next.pir);
    setActor(next.actor);
    setVendorRaw(next.affectedVendor);
    setVendor(next.affectedVendor);
    setBody(next.body);
    if (!MIRROR) setChannel(next.channel);
  }
  const headerCount =
    view === "browse" ? arts.length : view === "search" ? (searchData?.count ?? 0) : (pivotQ.data?.article_count ?? 0);
  const busy = view === "browse" ? browseQ.isFetching : view === "search" ? quickQ.isFetching : pivotQ.isFetching;

  return (
    <div className={`${pageContainer("wide")} space-y-4`}>
      <div className="flex items-baseline justify-between gap-2 flex-wrap">
        <h2 className={PAGE_TITLE}>ニュース・検索</h2>
        <span className="text-xs text-fg-subtle">{headerCount} 件{busy ? " · 更新中…" : ""}</span>
      </div>

      <NewsViewTabs currentFilters={currentFilters} onApply={applyView} />

      <FilterBar
        facetOpts={facetOpts}
        searchValue={searchRaw}
        onSearchChange={onSearchInput}
        searchPlaceholder="検索 (CVE/IP/ドメインは逆引き)…"
        severity={severity}
        onSeverity={setSeverity}
        relation={relationFromState(jp, severity.relevantOnly)}
        onRelation={(v) => {
          const next = applyRelation(v);
          setJp(next.jp);
          setSeverity((s) => ({ ...s, relevantOnly: next.relevantOnly }));
        }}
        since={since}
        onSince={setSince}
        sort={view === "browse" ? sort : undefined}
        onSort={view === "browse" ? setSort : undefined}
        category={category}
        onCategory={setCategory}
        feed={feed}
        onFeed={setFeed}
        intent={intent}
        onIntent={setIntent}
        pir={pir}
        onPir={setPir}
        actor={actor}
        onActor={setActor}
        vendorRaw={vendorRaw}
        onVendorRaw={setVendorRaw}
        vendorApplied={vendor}
        body={{ value: body, onChange: setBody }}
        channel={MIRROR ? undefined : { value: channel, onChange: setChannel }}
        trailing={
          view === "search" ? (
            <div className="inline-flex bg-surface-2 border border-border-subtle rounded-md p-0.5 h-8 items-center" title="精密 = LLM で関連度を並べ直す (~25-35s)">
              {([["quick", "クイック"], ["precise", "精密 (LLM)"]] as const).map(([k, label]) => (
                <button key={k} onClick={() => setPrecise(k === "precise")}
                  className={`px-2.5 h-7 rounded-sm text-xs font-medium transition-all ${
                    (precise ? "precise" : "quick") === k ? "bg-surface-overlay text-fg" : "text-fg-muted hover:text-fg"
                  }`}>
                  {label}
                </button>
              ))}
            </div>
          ) : (
            <div className="inline-flex bg-surface-2 border border-border-subtle rounded-md p-0.5 h-8 items-center">
              {(["headline", "summary"] as const).map((m) => (
                <button key={m} onClick={() => setMode(m)}
                  className={`px-2.5 h-7 rounded-sm text-xs font-medium transition-all ${
                    mode === m ? "bg-surface-overlay text-fg" : "text-fg-muted hover:text-fg"
                  }`}>
                  {m === "headline" ? "見出し" : "要約"}
                </button>
              ))}
            </div>
          )
        }
      />

      {/* 逆引き中バナー (明示 pivot / 自動逆引き) */}
      {view === "pivot" && activePivot && (
        <div className="flex flex-wrap items-center gap-2 text-xs">
          <span className="text-fg-subtle">逆引き中:</span>
          <button onClick={clearPivot}
            className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-accent-soft text-accent-hover border border-accent/40 hover:bg-accent/20">
            <span className="text-fg-subtle">{vocabLabel("entity_type", activePivot.type)}:</span>
            <span className="font-mono">{activePivot.value}</span>
            <span className="text-fg-subtle">×</span>
          </button>
          {autoPivot && !pivot && <span className="text-fg-subtle">(入力を自動逆引き)</span>}
        </div>
      )}

      {/* malware/cve はインライン chip 専用 (記事一覧の個別タグ click で付く絞り込み)。
          category/feed/intent/pir/actor/vendor/body/channel/severity の絞り込みチップは
          FilterBar が描く (詳細な絞り込みに畳んでいる条件も含めて一元化)。 */}
      {hasActiveTag && (
        <div className="flex flex-wrap items-center gap-2 text-xs">
          <span className="text-fg-subtle">タグ絞り込み:</span>
          {malware && (
            <button onClick={() => setMalware("")}
              className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-accent-soft text-accent-hover border border-accent/40 hover:bg-accent/20">
              {malware} <span className="text-fg-subtle">×</span>
            </button>
          )}
          {cve && (
            <button onClick={() => setCve("")}
              className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-critical-soft text-critical border border-critical/40 hover:bg-critical/20 font-mono">
              {cve} <span className="text-fg-subtle">×</span>
            </button>
          )}
        </div>
      )}

      {/* W2: 新着 (前回確認以降) surface — web-only に回す firehose を効率レビュー */}
      {view === "browse" && (
        <div className="flex flex-wrap items-center gap-2 text-xs">
          <button
            onClick={() => setNewOnly((v) => !v)}
            title="前回『ここまで既読』以降に取得した記事のみを表示"
            className={`inline-flex items-center gap-1 px-2.5 h-7 rounded-md border font-medium transition-colors ${
              newOnly
                ? "bg-accent-soft text-accent-hover border-accent/40"
                : "bg-surface-2 text-fg-muted border-border-subtle hover:text-fg"
            }`}
          >
            新着のみ
          </button>
          {newOnly && (
            <span className="text-fg-subtle">
              {lastSeen ? `前回確認: ${formatJstCompact(lastSeen)} 以降` : "基準未設定 — 直近24hを表示中"}
            </span>
          )}
          <button
            onClick={markCaughtUp}
            title="現在地までを既読にする"
            className="ml-auto inline-flex items-center gap-1 px-2.5 h-7 rounded-md border border-border-subtle bg-surface-2 text-fg-muted hover:text-fg font-medium"
          >
            ここまで既読にする
          </button>
        </div>
      )}

      {/* ビュー本体 */}
      {view === "search" && (
        <>
          {quickQ.isFetching && !searchData && <div className="text-fg-subtle text-sm">検索中…</div>}
          {quickQ.isError && !searchData && <div className="text-critical text-sm">検索エラー: {String(quickQ.error)}</div>}
          {searchData && <SearchResults data={searchData} precising={precising} />}
        </>
      )}

      {view === "pivot" && (
        <>
          {pivotQ.isFetching && <div className="text-fg-subtle text-sm">逆引き中…</div>}
          {pivotQ.error && <div className="text-critical text-sm">エラー: {String(pivotQ.error)}</div>}
          {pivotQ.data && <PivotResults data={pivotQ.data} onPivot={runPivot} />}
        </>
      )}

      {view === "browse" && (
        arts.length === 0 ? (
          <div className="text-fg-subtle text-sm p-10 text-center">
            {browseQ.isFetching ? (
              <span className="italic">読み込み中…</span>
            ) : newOnly ? (
              <span className="inline-flex items-center gap-1.5">
                <Check className="h-4 w-4 text-accent" /> 新着なし — すべて確認済みです。
              </span>
            ) : (
              <span className="italic">該当記事がありません。フィルタを緩めてください。</span>
            )}
          </div>
        ) : (
          // 記事 1 件ずつを箱で囲わず、罫線で区切る (公開サイトと同じ)。
          // 30 件が箱で並ぶと枠のほうが目立ち、見出しが沈む。
          <ul className="divide-y divide-border-subtle">
            {arts.map((a) => {
              const cves = a.title ? extractCves(a.title) : [];
              return (
                <li key={a.id ?? a.article_id} className="flex items-start gap-2 px-1 py-2.5 hover:bg-surface-1">
                  <span className={`mt-1.5 w-1.5 h-1.5 rounded-full shrink-0 ${
                    a.importance === "high" ? "bg-critical" : a.importance === "medium" ? "bg-warning" : "bg-fg-subtle"
                  }`} />
                  <div className="flex-1 min-w-0">
                    {/* タイトル = 記事詳細 (分析結果) に統一 — 検索/pivot モード・他全画面と一致。
                        元記事は末尾の ↗ で併置 (2026-07-25)。article_id 無しの旧レコードのみ外部直行。 */}
                    {a.article_id ? (
                      <a href={`/app/article/${encodeURIComponent(a.article_id)}`}
                        className="block text-[15.5px] font-semibold leading-[1.6] text-fg hover:text-accent hover:underline"
                        title="記事の分析結果 (Diamond 判定・IoC・逆引き) を表示">
                        {a.title}
                      </a>
                    ) : (
                      <a href={a.url} target="_blank" rel="noopener noreferrer"
                        className="block text-[15.5px] font-semibold leading-[1.6] text-fg hover:text-accent hover:underline" title={a.title}>
                        {a.title}
                      </a>
                    )}
                    {mode === "summary" && a.summary && (
                      <p className="text-xs text-fg-muted leading-relaxed mt-1 line-clamp-4">{a.summary}</p>
                    )}
                    <div className="text-[13px] flex flex-wrap items-center gap-x-1.5 gap-y-1 mt-1">
                      {cves.slice(0, 4).map((c) => (
                        <Chip key={c} tone="critical" mono active={cve === c} onClick={() => setCve(cve === c ? "" : c)}>{c}</Chip>
                      ))}
                      {(a.malware_families ?? []).slice(0, 3).map((mw) => (
                        <Chip key={mw} tone="accent" active={malware === mw} onClick={() => setMalware(malware === mw ? "" : mw)}>{mw}</Chip>
                      ))}
                      {a.category && (
                        <Chip tone="muted" active={category === a.category} onClick={() => setCategory(category === a.category ? "" : a.category!)}>{vocabLabel("category", a.category)}</Chip>
                      )}
                      <LevelBadge level={a.level} />
                      {a.victim_sector && <span className="px-1 rounded bg-surface-2 text-fg-muted">{sectorLabel(a.victim_sector)}</span>}
                      {a.victim_country && <span className="px-1 rounded bg-surface-2 text-fg-muted">{countryLabel(a.victim_country)}</span>}
                      {a.socio_political_intent && hasIntent(a.socio_political_intent) && (
                        <Chip tone="accent" active={intent === a.socio_political_intent}
                          onClick={() => setIntent(intent === a.socio_political_intent ? "" : a.socio_political_intent!)}>
                          {intentLabel(a.socio_political_intent)}
                          {isHypothesisIntent(a.intent_confidence) && <span className="opacity-70"> (仮説)</span>}
                        </Chip>
                      )}
                      <span className="text-fg-subtle truncate">{a.feed_title}</span>
                      {a.posted_channel && <span className="text-fg-subtle">{chMeta(a.posted_channel).label}</span>}
                      <a href={a.url} target="_blank" rel="noopener noreferrer"
                        className="text-fg-subtle hover:text-accent" title="元記事を開く">元記事 ↗</a>
                      {(a.published_at ?? a.created_at) && (
                        <span className="ml-auto shrink-0 text-fg-subtle" title={a.published_at ? "公開時刻" : "取得時刻 (公開時刻不明)"}>
                          {formatJstCompact(a.published_at ?? a.created_at)}
                          {/* 一覧は公開時刻で並ぶ。取得が大きく遅れた記事 (ANSSI の
                              まとめ配信等、実測 10%) は「なぜ古い記事が新着に居るのか」が
                              分からなくなるため、そのときだけ取得時刻を添える。 */}
                          {isLateIngest(a.published_at, a.created_at) && (
                            <span className="ml-1 text-fg-subtle/70" title="取得時刻 (公開から遅れて配信された記事)">
                              取得 {formatJstCompact(a.created_at)}
                            </span>
                          )}
                        </span>
                      )}
                    </div>
                    {a.technical_axis_summary && (
                      <p className="text-[13px] text-fg-subtle leading-relaxed mt-1 flex items-start gap-1"
                        title="Diamond Model の技術面 (使用ツールと基盤の結びつき)">
                        <span className="line-clamp-2">{a.technical_axis_summary}</span>
                      </p>
                    )}
                  </div>
                </li>
              );
            })}
          </ul>
        )
      )}

      {canLoadMore && (
        <div className="text-center pt-1">
          <button onClick={() => setLimit((l) => Math.min(l + 30, 200))} disabled={browseQ.isFetching}
            className="px-4 py-1.5 rounded-md border border-border-subtle text-fg hover:bg-surface-2 text-sm font-medium disabled:opacity-50">
            {browseQ.isFetching ? "読み込み中…" : "さらに読み込む"}
          </button>
        </div>
      )}
    </div>
  );
}

const CHIP_TONE: Record<string, string> = {
  critical: "bg-critical-soft text-critical hover:bg-critical/20",
  accent: "bg-accent-soft text-accent-hover hover:bg-accent/20",
  muted: "bg-surface-2 text-fg-muted hover:bg-surface-3 hover:text-fg",
};

function Chip({ children, tone, mono, active, onClick }: {
  children: ReactNode;
  tone: "critical" | "accent" | "muted";
  mono?: boolean;
  active?: boolean;
  onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      className={`px-1.5 rounded transition-colors ${CHIP_TONE[tone]} ${mono ? "font-mono" : ""} ${
        active ? "ring-1 ring-accent" : ""
      }`}
    >
      {children}
    </button>
  );
}
