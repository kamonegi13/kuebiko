// 公開ニュースサイト (匿名 = Tier0 が見る唯一の画面)。
//
// 分析者向けの AppShell (サイドバー・タブ・操作系) は出さない。読み手ができるのは
// 「読む / カテゴリで絞る / 検索する / 開く / 出典へ辿る」だけ。
//
// **一覧に出すのは「何の話か」だけ** (カテゴリ → 見出し → 要約 → 日付)。
// 媒体数・出典名・裏取りの内訳は分析者向けの情報なので **開いてから**見せる
// (2026-08-25 利用者指摘:「何件が報道などは最初から見られる必要はない」)。
//
// 画像は持っていないので、写真ではなく **文字の大きさと余白**で階層をつくる。

import { useCallback, useEffect, useState } from "react";
import { useQuery, keepPreviousData } from "@tanstack/react-query";
import { ExternalLink, Search, ChevronLeft } from "lucide-react";
import {
  fetchPublicNews,
  fetchPublicMap,
  fetchPublicNewsDetail,
  type PublicCitation,
  type PublicNewsItem,
} from "../api/publicNews";
import { formatJstDate, relativeFromNow } from "../utils/date";
import { vocabLabel } from "../hooks/useVocab";
import { PublicErrorBoundary } from "./PublicErrorBoundary";
import { Drawer } from "../components/Drawer";
import { PublicMapSection } from "./PublicMapSection";

const PAGE_SIZE = 24;
const FEATURED_COUNT = 3;
const HOME_PATH = "/app/news";

/** カテゴリの表示名。どの category を束ねるかの定義は backend が持つ。 */
function categoryLabel(key: string): string {
  if (!key) return "";
  return vocabLabel("category_group", key) || vocabLabel("category", key) || key;
}

type Route =
  | { kind: "home" }
  | { kind: "category"; key: string }
  | { kind: "map" }
  | { kind: "detail"; id: string };

function parseRoute(): Route {
  const p = window.location.pathname;
  if (/^\/app\/news\/map\/?$/.test(p)) return { kind: "map" };
  const cat = /^\/app\/news\/c\/([^/]+)\/?$/.exec(p);
  if (cat) return { kind: "category", key: decodeURIComponent(cat[1]) };
  const detail = /^\/app\/news\/([^/]+)\/?$/.exec(p);
  return detail ? { kind: "detail", id: decodeURIComponent(detail[1]) } : { kind: "home" };
}

function navigate(path: string): void {
  window.history.pushState(null, "", path);
  window.dispatchEvent(new PopStateEvent("popstate"));
}

export function PublicNewsSite() {
  const [route, setRoute] = useState(parseRoute);

  useEffect(() => {
    const handler = () => setRoute(parseRoute());
    window.addEventListener("popstate", handler);
    return () => window.removeEventListener("popstate", handler);
  }, []);

  useEffect(() => {
    const p = window.location.pathname;
    if (route.kind === "home" && p !== HOME_PATH) {
      window.history.replaceState(null, "", `${HOME_PATH}${window.location.search}`);
    }
  }, [route]);

  // ⚠ 背後のスクロールロックは **Drawer が持っている** (scrollbar 幅の補正込み)。
  // ここで二重に掛けると、閉じるときに互いの復元が競合して `overflow: hidden` が
  // 残り、**一覧がスクロールできなくなる** (2026-08-25 に実際に起きた)。
  // グローバルなスタイルの持ち主は 1 つに保つ。

  return (
    <div className="min-h-screen bg-surface-1 text-fg flex flex-col">
      <SiteHeader route={route} />
      <main className="flex-1 w-full max-w-[72rem] mx-auto px-5 py-8">
        {/* 描画で落ちてもヘッダ・カテゴリ・フッタは残す (他の記事へ移れるように) */}
        <PublicErrorBoundary onReset={() => navigate(HOME_PATH)}>
          {route.kind === "map" ? (
            <PublicMapSection onCountry={(iso) => navigate(`${HOME_PATH}?country=${iso}`)} />
          ) : (
            /* PC は本文 + 右レールの 2 カラム。モバイルは 1 カラムのまま
               (レールは本文の後ろへ回して読む順序を壊さない)。 */
            <div className="lg:grid lg:grid-cols-[minmax(0,1fr)_17rem] lg:gap-10 lg:items-start">
              <NewsList
                category={route.kind === "category" ? route.key : undefined}
                /* 記事を開いていても一覧は裏に残す (閉じたとき位置が戻らないように) */
                openedId={route.kind === "detail" ? route.id : undefined}
              />
              <CountryRail onCountry={(iso) => navigate(`${HOME_PATH}?country=${iso}`)} />
            </div>
          )}
        </PublicErrorBoundary>
      </main>
      <SiteFooter />

      {/* 記事はドロワーで読む。URL は /app/news/{id} のまま維持するので、
          共有もブラウザバックもそのまま効く (2026-08-25 利用者提案)。 */}
      <Drawer
        isOpen={route.kind === "detail"}
        onClose={() => window.history.back()}
        title="記事"
        widthClass="md:w-[48rem]"
        mobileGutter
        swipeToClose
      >
        {route.kind === "detail" && (
          <PublicErrorBoundary onReset={() => window.history.back()}>
            <NewsDetail id={route.id} />
          </PublicErrorBoundary>
        )}
      </Drawer>
    </div>
  );
}

function SiteHeader({ route }: { route: Route }) {
  return (
    <header className="border-b border-border-subtle bg-surface-1/95 backdrop-blur-md sticky top-0 z-20">
      <div className="w-full max-w-[72rem] mx-auto px-5">
        <div className="flex items-baseline gap-2.5 pt-4 pb-3">
          <button
            onClick={() => navigate(HOME_PATH)}
            className="text-[17px] font-bold tracking-tight text-fg hover:text-accent transition-colors"
          >
            kuebiko
          </button>
          <span className="text-[11px] text-fg-subtle">サイバー脅威ニュース</span>
        </div>
        <CategoryNav
          active={route.kind === "category" ? route.key : undefined}
          onMap={route.kind === "map"}
        />
      </div>
    </header>
  );
}

function SiteFooter() {
  return (
    <footer className="border-t border-border-subtle mt-12">
      <div className="w-full max-w-[72rem] mx-auto px-5 py-6 text-[11px] leading-relaxed text-fg-subtle space-y-2">
        <p>
          掲載しているのは kuebiko が公開報道から生成した要約です。原記事そのものではありません。
          各記事の出典をご確認ください。
        </p>
        <p>
          <a href="/auth/" className="hover:text-accent underline underline-offset-2">
            運用者ログイン
          </a>
        </p>
      </div>
    </footer>
  );
}

/** カテゴリの並びは backend が返す順をそのまま使う (定義を frontend に複製しない)。 */
function CategoryNav({ active, onMap }: { active?: string; onMap: boolean }) {
  const { data } = useQuery({
    queryKey: ["public-news-categories"],
    queryFn: () => fetchPublicNews({ limit: 1 }),
    staleTime: 30 * 60 * 1000,
  });
  const keys = data?.categories ?? [];
  if (keys.length === 0) return null;
  return (
    <nav className="flex flex-wrap gap-x-4 gap-y-1 overflow-x-auto">
      <CategoryTab href={HOME_PATH} label="新着" active={!active && !onMap} />
      {keys.map((k) => (
        <CategoryTab
          key={k}
          href={`${HOME_PATH}/c/${encodeURIComponent(k)}`}
          label={categoryLabel(k)}
          active={active === k}
        />
      ))}
      <CategoryTab href={`${HOME_PATH}/map`} label="地図" active={onMap} />
    </nav>
  );
}

function CategoryTab({ href, label, active }: { href: string; label: string; active: boolean }) {
  return (
    <a
      href={href}
      onClick={(e) => {
        e.preventDefault();
        navigate(href);
      }}
      className={`shrink-0 pb-2.5 text-[13px] border-b-2 -mb-px transition-colors ${
        active
          ? "border-accent text-fg font-semibold"
          : "border-transparent text-fg-subtle hover:text-fg"
      }`}
    >
      {label}
    </a>
  );
}

/**
 * 右レール (PC のみ)。被害国の上位と地図への導線。
 *
 * 一覧の左右が余る PC で、このツールの特徴 (地図) を入口から見せる。
 * ⚠ ここでも **置けなかった件数** を書く。上位だけ見せると「これが全部」と読まれる。
 * モバイルでは本文の後ろに 1 カラムで続く (読む順序を壊さない)。
 */
function CountryRail({ onCountry }: { onCountry: (iso: string) => void }) {
  const { data } = useQuery({
    queryKey: ["public-map"],
    queryFn: () => fetchPublicMap(30),
    staleTime: 10 * 60 * 1000,
  });
  if (!data || data.nodes.length === 0) return null;
  return (
    <aside className="mt-10 lg:mt-0 lg:sticky lg:top-[7.5rem] space-y-3">
      <div className="flex items-baseline gap-2">
        <h2 className="text-[13px] font-semibold text-fg-muted">被害国</h2>
        <a
          href={`${HOME_PATH}/map`}
          onClick={(e) => {
            e.preventDefault();
            navigate(`${HOME_PATH}/map`);
          }}
          className="ml-auto text-[11px] text-fg-subtle hover:text-accent underline underline-offset-2"
        >
          地図で見る
        </a>
      </div>
      <ul className="space-y-1">
        {data.nodes.slice(0, 8).map((n) => (
          <li key={n.iso}>
            <button
              onClick={() => onCountry(n.iso)}
              className="w-full flex items-baseline gap-2 text-left text-[13px] hover:text-accent transition-colors"
            >
              <span className="text-fg-muted">{n.label}</span>
              <span className="flex-1 border-b border-dotted border-border-subtle" />
              <span className="tnum text-fg-subtle">{n.count}</span>
            </button>
          </li>
        ))}
      </ul>
      <p className="text-[11px] leading-relaxed text-fg-subtle">
        直近 {data.window_days} 日の掲載 {data.total} 件のうち被害国を特定できた {data.placed} 件の
        内訳です ({data.unplaced} 件は特定できず)。
      </p>
    </aside>
  );
}

/** カテゴリバッジ。一覧で「何の話か」を最初に示す唯一のメタ情報。 */
function CategoryBadge({ category }: { category: string }) {
  const label = categoryLabel(category);
  if (!label) return null;
  return <span className="text-[11px] font-semibold tracking-wide text-accent">{label}</span>;
}

function NewsList({ category, openedId }: { category?: string; openedId?: string }) {
  const country = new URLSearchParams(window.location.search).get("country") ?? undefined;
  const [term, setTerm] = useState(() => new URLSearchParams(window.location.search).get("q") ?? "");
  const [search, setSearch] = useState(term);
  const [page, setPage] = useState(0);
  const basePath = category ? `${HOME_PATH}/c/${encodeURIComponent(category)}` : HOME_PATH;

  useEffect(() => {
    setTerm("");
    setSearch("");
    setPage(0);
  }, [category]);

  const submit = useCallback(() => {
    const next = term.trim();
    setSearch(next);
    setPage(0);
    const p = new URLSearchParams();
    if (next) p.set("q", next);
    window.history.replaceState(null, "", `${basePath}${p.toString() ? `?${p}` : ""}`);
  }, [term, basePath]);

  const showFeatured = !category && !search && !country && page === 0;

  const { data: featured } = useQuery({
    queryKey: ["public-news-featured"],
    queryFn: () => fetchPublicNews({ limit: FEATURED_COUNT, featured: true }),
    staleTime: 10 * 60 * 1000,
    enabled: showFeatured,
  });

  const { data, isFetching, error } = useQuery({
    queryKey: ["public-news", category ?? "", country ?? "", search, page],
    queryFn: () =>
      fetchPublicNews({
        limit: PAGE_SIZE,
        offset: page * PAGE_SIZE,
        search: search || undefined,
        category,
        country,
      }),
    placeholderData: keepPreviousData,
    refetchInterval: 10 * 60 * 1000,
  });

  const featuredItems = showFeatured ? (featured?.items ?? []) : [];
  const featuredIds = new Set(featuredItems.map((i) => i.id));
  // 注目に出したものを下でもう一度出さない (同じ見出しが 2 回並ぶと読みにくい)
  const items = (data?.items ?? []).filter((i) => !featuredIds.has(i.id));

  return (
    <div className="space-y-8">
      {featuredItems.length > 0 && (
        <section className="space-y-5">
          <div className="flex items-baseline gap-2">
            <h2 className="text-[11px] font-semibold tracking-widest text-fg-subtle">注目</h2>
            {/* 何を基準に選んでいるかを読み手に示す (順位の根拠を隠さない) */}
            <span className="text-[11px] text-fg-subtle">
              直近 72 時間で多くの媒体が報じた事案
            </span>
          </div>
          <LeadStory item={featuredItems[0]} />
          {featuredItems.length > 1 && (
            <ul className="grid gap-x-8 gap-y-5 md:grid-cols-2 pt-6 border-t border-border-subtle">
              {featuredItems.slice(1).map((it) => (
                <NewsCard key={it.id} item={it} />
              ))}
            </ul>
          )}
        </section>
      )}

      <section className="space-y-5">
        <div className="flex items-center gap-3 pt-4 border-t border-border-subtle">
          <h2 className="text-[13px] font-semibold text-fg-muted">
            {search
              ? `「${search}」の検索結果`
              : country
                ? `被害国: ${country}`
                : category
                  ? categoryLabel(category)
                  : "新着"}
          </h2>
          <div className="ml-auto">
            <SearchBox
              term={term}
              onChange={setTerm}
              onSubmit={submit}
              onClear={() => {
                setTerm("");
                setSearch("");
                setPage(0);
                window.history.replaceState(null, "", basePath);
              }}
              active={Boolean(search)}
            />
          </div>
        </div>

        {isFetching && !data && <p className="text-sm text-fg-subtle">読み込み中…</p>}
        {error && (
          <p className="text-sm text-critical">
            読み込みに失敗しました。時間をおいてお試しください。
          </p>
        )}
        {data && items.length === 0 && (
          <p className="text-sm text-fg-muted py-6">該当する記事がありません。</p>
        )}

        {/* PC は 2 列。カードは高さがまちまちなので grid で行を揃える */}
        <ul className="grid gap-x-8 gap-y-7 md:grid-cols-2">
          {items.map((it) => (
            <NewsCard key={it.id} item={it} opened={it.id === openedId} />
          ))}
        </ul>

        {(page > 0 || (data?.items.length ?? 0) === PAGE_SIZE) && (
          <div className="flex items-center gap-3 pt-6 border-t border-border-subtle">
            <button
              disabled={page === 0}
              onClick={() => {
                setPage((n) => Math.max(0, n - 1));
                window.scrollTo(0, 0);
              }}
              className="text-[13px] text-fg-muted hover:text-accent disabled:opacity-40 disabled:hover:text-fg-muted transition-colors"
            >
              ← 新しい記事
            </button>
            <span className="text-[11px] text-fg-subtle tnum">{page + 1}</span>
            <button
              disabled={(data?.items.length ?? 0) < PAGE_SIZE}
              onClick={() => {
                setPage((n) => n + 1);
                window.scrollTo(0, 0);
              }}
              className="text-[13px] text-fg-muted hover:text-accent disabled:opacity-40 disabled:hover:text-fg-muted transition-colors"
            >
              古い記事 →
            </button>
          </div>
        )}
      </section>
    </div>
  );
}

function SearchBox({
  term,
  onChange,
  onSubmit,
  onClear,
  active,
}: {
  term: string;
  onChange: (v: string) => void;
  onSubmit: () => void;
  onClear: () => void;
  active: boolean;
}) {
  return (
    <div className="flex items-center gap-1.5">
      <div className="relative">
        <Search className="absolute left-2 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-fg-subtle" />
        <input
          value={term}
          onChange={(e) => onChange(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && onSubmit()}
          placeholder="検索"
          aria-label="記事を検索"
          className="h-8 w-[9rem] focus:w-[13rem] pl-7 pr-2 bg-surface-2 border border-border-subtle rounded text-[13px] placeholder:text-fg-subtle focus:outline-none focus:border-accent transition-all"
        />
      </div>
      {active && (
        <button onClick={onClear} className="text-[11px] text-fg-subtle hover:text-accent underline">
          解除
        </button>
      )}
    </div>
  );
}

/** メタ行。一覧では **日付だけ**。媒体数や出典名は開いてから見せる。 */
function CardMeta({ item }: { item: PublicNewsItem }) {
  return (
    <div className="flex items-center gap-2 text-[11px] text-fg-subtle">
      <time dateTime={item.published_at}>{formatJstDate(item.published_at)}</time>
      <span>{relativeFromNow(item.published_at)}</span>
    </div>
  );
}

/** 先頭記事。画像が無いので見出しを一段大きくして階層をつくる。 */
function LeadStory({ item }: { item: PublicNewsItem }) {
  return (
    <article>
      <button
        onClick={() => navigate(`${HOME_PATH}/${encodeURIComponent(item.id)}`)}
        className="block w-full text-left group space-y-2"
      >
        <CategoryBadge category={item.category} />
        <h2 className="text-[22px] lg:text-[26px] font-bold leading-[1.4] text-fg group-hover:text-accent transition-colors">
          {item.headline}
        </h2>
        {item.summary && (
          <p className="text-[14px] lg:text-[15px] leading-[1.9] text-fg-muted line-clamp-3">
            {item.summary}
          </p>
        )}
      </button>
      <div className="mt-2.5">
        <CardMeta item={item} />
      </div>
    </article>
  );
}

function NewsCard({ item, opened }: { item: PublicNewsItem; opened?: boolean }) {
  return (
    <li>
      <article className={opened ? "opacity-60" : undefined}>
        <button
          onClick={() => navigate(`${HOME_PATH}/${encodeURIComponent(item.id)}`)}
          className="block w-full text-left group space-y-1.5"
        >
          <CategoryBadge category={item.category} />
          <h3 className="text-[16px] font-semibold leading-[1.5] text-fg group-hover:text-accent transition-colors">
            {item.headline}
          </h3>
          {item.summary && (
            <p className="text-[13px] leading-[1.85] text-fg-muted line-clamp-2">{item.summary}</p>
          )}
        </button>
        <div className="mt-2">
          <CardMeta item={item} />
        </div>
      </article>
    </li>
  );
}

/** 出典の並び。**詳細でだけ** 見せる。 */
function Citations({ citations }: { citations: PublicCitation[] }) {
  return (
    <section className="space-y-3 pt-6 border-t border-border-subtle">
      <h2 className="text-[13px] font-semibold text-fg-muted">出典 ({citations.length})</h2>
      <ol className="space-y-2.5">
        {citations.map((c) => (
          <li key={c.index} className="text-[13px] leading-[1.7]">
            <span className="text-fg-subtle mr-1.5 tnum">[{c.index}]</span>
            <a
              href={c.url}
              target="_blank"
              rel="noopener noreferrer nofollow"
              className="text-fg hover:text-accent underline decoration-border-default underline-offset-2"
            >
              {c.title}
              <ExternalLink className="inline w-3 h-3 ml-1 align-baseline" />
            </a>
            {c.source && <div className="text-[11px] text-fg-subtle mt-0.5">{c.source}</div>}
          </li>
        ))}
      </ol>
    </section>
  );
}

function NewsDetail({ id }: { id: string }) {
  const { data, isFetching, error } = useQuery({
    queryKey: ["public-news-detail", id],
    queryFn: () => fetchPublicNewsDetail(id),
  });

  useEffect(() => {
    window.scrollTo(0, 0);
  }, [id]);

  if (isFetching && !data) return <p className="text-sm text-fg-subtle">読み込み中…</p>;
  if (error || !data) {
    return (
      <div className="space-y-3">
        <p className="text-sm text-fg-muted">記事が見つかりませんでした。</p>
        <button
          onClick={() => window.history.back()}
          className="inline-flex items-center gap-1 text-sm text-accent hover:underline"
        >
          <ChevronLeft className="w-3.5 h-3.5" />
          閉じる
        </button>
      </div>
    );
  }

  return (
    <article className="space-y-6 max-w-[42rem]">
      <header className="space-y-2.5">
        <CategoryBadge category={data.category} />
        <h1 className="text-[24px] font-bold leading-[1.45] text-fg">{data.headline}</h1>
        <div className="flex flex-wrap items-center gap-2 text-[11px] text-fg-subtle">
          <time dateTime={data.published_at}>{formatJstDate(data.published_at)}</time>
          {/* 裏取りの内訳は分析的な情報なので、開いた人にだけ見せる */}
          {data.independent_sources >= 2 && <span>独立 {data.independent_sources} 媒体が報道</span>}
        </div>
      </header>

      {data.bluf && <p className="text-[15px] leading-[2] text-fg">{data.bluf}</p>}

      {data.facts.length > 0 && (
        <section className="space-y-2">
          <h2 className="text-[13px] font-semibold text-fg-muted">報じられている内容</h2>
          <ul className="space-y-2">
            {data.facts.map((f, i) => (
              <li key={i} className="text-[14px] leading-[1.9] text-fg-muted">
                {f.text}
                {f.source_index > 0 && (
                  <sup className="ml-0.5 text-accent tnum">[{f.source_index}]</sup>
                )}
              </li>
            ))}
          </ul>
        </section>
      )}

      {data.discrepancies.length > 0 && (
        <section className="space-y-2">
          <h2 className="text-[13px] font-semibold text-warning">媒体間で食い違う点</h2>
          <ul className="space-y-1.5 text-[14px] leading-[1.9] text-fg-muted">
            {data.discrepancies.map((d, i) => (
              <li key={i}>
                {d.text}
                {d.source_index > 0 && (
                  <sup className="ml-0.5 text-accent tnum">[{d.source_index}]</sup>
                )}
              </li>
            ))}
          </ul>
        </section>
      )}

      {data.unknowns.length > 0 && (
        <section className="space-y-2">
          <h2 className="text-[13px] font-semibold text-fg-muted">わかっていない点</h2>
          <ul className="space-y-1.5 text-[14px] leading-[1.9] text-fg-muted">
            {data.unknowns.map((u, i) => (
              <li key={i}>{u}</li>
            ))}
          </ul>
        </section>
      )}

      <Citations citations={data.citations} />

      <p className="text-[11px] leading-relaxed text-fg-subtle">{data.note}</p>
    </article>
  );
}
