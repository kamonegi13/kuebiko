// 公開ニュースサイト (匿名 = Tier0 が見る唯一の画面)。
//
// 分析者向けの AppShell (サイドバー・タブ・操作系) は出さない。読み手ができるのは
// 「一覧を読む / 検索する / 記事を開く / 出典へ辿る」の 4 つだけ。
//
// 表示の原則:
// - **出典を必ず見せる**。生成物であることも常に明示する (原記事ではない)
// - 出版社の本文は持っていない (公開 API が返さない)。要約は kuebiko が書いたもの
// - 画像は無いので、写真主体ではなく **文字組みの密度**で読ませる

import { useCallback, useEffect, useState } from "react";
import { useQuery, keepPreviousData } from "@tanstack/react-query";
import { ExternalLink, Search, ChevronLeft } from "lucide-react";
import {
  fetchPublicNews,
  fetchPublicNewsDetail,
  type PublicCitation,
  type PublicNewsItem,
} from "../api/publicNews";
import { formatJstCompact } from "../utils/date";
import { vocabLabel } from "../hooks/useVocab";

const PAGE_SIZE = 30;
const HOME_PATH = "/app/news";

/** カテゴリの表示名。値の定義 (どの category を束ねるか) は backend が持つ。 */
function categoryLabel(key: string): string {
  return vocabLabel("category_group", key) || vocabLabel("category", key) || key;
}

type Route =
  | { kind: "home" }
  | { kind: "category"; key: string }
  | { kind: "detail"; id: string };

function parseRoute(): Route {
  const p = window.location.pathname;
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

  // 公開サイトが持つ path 以外 (/app/dashboard 等) は一覧へ寄せる
  useEffect(() => {
    const p = window.location.pathname;
    if (route.kind === "home" && p !== HOME_PATH) {
      window.history.replaceState(null, "", `${HOME_PATH}${window.location.search}`);
    }
  }, [route]);

  return (
    <div className="min-h-screen bg-surface-1 text-fg flex flex-col">
      <SiteHeader route={route} />
      <main className="flex-1 w-full max-w-[52rem] mx-auto px-4 py-6">
        {route.kind === "detail" ? (
          <NewsDetail id={route.id} />
        ) : (
          <NewsList category={route.kind === "category" ? route.key : undefined} />
        )}
      </main>
      <SiteFooter />
    </div>
  );
}

/** カテゴリの並び。backend が返す順をそのまま使う (定義を frontend に複製しない)。 */
function CategoryNav({ active }: { active?: string }) {
  const { data } = useQuery({
    queryKey: ["public-news-categories"],
    queryFn: () => fetchPublicNews({ limit: 1 }),
    staleTime: 30 * 60 * 1000,
  });
  const keys = data?.categories ?? [];
  if (keys.length === 0) return null;
  return (
    <nav className="flex flex-wrap gap-1.5 -mb-px">
      <CategoryTab href={HOME_PATH} label="新着" active={!active} />
      {keys.map((k) => (
        <CategoryTab
          key={k}
          href={`${HOME_PATH}/c/${encodeURIComponent(k)}`}
          label={categoryLabel(k)}
          active={active === k}
        />
      ))}
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
      className={`px-2.5 py-1.5 text-sm rounded-t border-b-2 transition-colors ${
        active
          ? "border-accent text-accent font-medium"
          : "border-transparent text-fg-muted hover:text-fg"
      }`}
    >
      {label}
    </a>
  );
}

function SiteHeader({ route }: { route: Route }) {
  return (
    <header className="border-b border-border-subtle bg-surface-1/95 backdrop-blur-md sticky top-0 z-20">
      <div className="w-full max-w-[52rem] mx-auto px-4 pt-3">
        <div className="flex items-baseline gap-3">
          <button
            onClick={() => navigate(HOME_PATH)}
            className="text-lg font-bold tracking-tight text-fg hover:text-accent transition-colors"
          >
            kuebiko
          </button>
          <span className="text-xs text-fg-subtle">サイバー脅威ニュース</span>
        </div>
        <div className="mt-2">
          <CategoryNav active={route.kind === "category" ? route.key : undefined} />
        </div>
      </div>
    </header>
  );
}

function SiteFooter() {
  return (
    <footer className="border-t border-border-subtle mt-8">
      <div className="w-full max-w-[52rem] mx-auto px-4 py-5 text-xs text-fg-subtle space-y-1.5">
        <p>
          掲載しているのは kuebiko が公開報道から生成した要約です。原記事そのものではありません。
          詳細は各記事の出典をご確認ください。
        </p>
        <p>
          {/* 運用者向けの導線。控えめに置く (公開サイトの主役ではない) */}
          <a href="/auth/" className="hover:text-accent underline">
            運用者ログイン
          </a>
        </p>
      </div>
    </footer>
  );
}

/** 出典の要約表示。媒体名を並べ、件数が多ければ残数を出す。 */
function SourceLine({ citations, total }: { citations: PublicCitation[]; total: number }) {
  const shown = citations.map((c) => c.source || "出典不明").filter(Boolean);
  const rest = total - citations.length;
  return (
    <span className="text-fg-subtle">
      {shown.join(" / ")}
      {rest > 0 && ` ほか ${rest} 媒体`}
    </span>
  );
}

function NewsList({ category }: { category?: string }) {
  const [term, setTerm] = useState(() => new URLSearchParams(window.location.search).get("q") ?? "");
  const [search, setSearch] = useState(term);
  const [page, setPage] = useState(0);
  const basePath = category ? `${HOME_PATH}/c/${encodeURIComponent(category)}` : HOME_PATH;

  // カテゴリを移ったら検索とページを持ち越さない (別の条件の続きを見せない)
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

  const { data, isFetching, error } = useQuery({
    queryKey: ["public-news", category ?? "", search, page],
    queryFn: () =>
      fetchPublicNews({
        limit: PAGE_SIZE,
        offset: page * PAGE_SIZE,
        search: search || undefined,
        category,
      }),
    placeholderData: keepPreviousData,
    refetchInterval: 10 * 60 * 1000,
  });

  const items = data?.items ?? [];
  // 注目は「新着」の 1 ページ目・検索なしのときだけ (絞り込み中に別条件の記事を混ぜない)
  const showFeatured = !category && !search && page === 0;

  return (
    <div className="space-y-5">
      {showFeatured && <FeaturedStrip />}
      <div className="flex gap-2">
        <div className="relative flex-1">
          <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 w-4 h-4 text-fg-subtle" />
          <input
            value={term}
            onChange={(e) => setTerm(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && submit()}
            placeholder="キーワードで探す"
            className="w-full h-9 pl-8 pr-3 bg-surface-2 border border-border-subtle rounded-md text-sm placeholder:text-fg-subtle focus:outline-none focus:border-accent"
          />
        </div>
        <button
          onClick={submit}
          className="h-9 px-4 rounded-md border border-border-default text-sm text-fg-muted hover:text-accent hover:border-accent-soft transition-colors"
        >
          検索
        </button>
      </div>

      {search && (
        <p className="text-xs text-fg-subtle">
          「{search}」の検索結果
          <button onClick={() => { setTerm(""); setSearch(""); setPage(0); window.history.replaceState(null, "", basePath); }} className="ml-2 underline hover:text-accent">
            解除
          </button>
        </p>
      )}

      {isFetching && !data && <p className="text-sm text-fg-subtle">読み込み中…</p>}
      {error && <p className="text-sm text-critical">読み込みに失敗しました。時間をおいてお試しください。</p>}
      {data && items.length === 0 && (
        <p className="text-sm text-fg-muted bg-surface-2 border border-border-subtle rounded-lg p-4">
          該当する記事がありません。
        </p>
      )}

      <ul className="divide-y divide-border-subtle">
        {items.map((it) => (
          <NewsListItem key={it.id} item={it} />
        ))}
      </ul>

      {(page > 0 || items.length === PAGE_SIZE) && (
        <div className="flex items-center gap-2 pt-2">
          <button
            disabled={page === 0}
            onClick={() => { setPage((n) => Math.max(0, n - 1)); window.scrollTo(0, 0); }}
            className="text-xs px-3 py-1.5 rounded border border-border-default text-fg-muted hover:text-accent disabled:opacity-40 transition-colors"
          >
            ← 新しい記事
          </button>
          <span className="text-xs text-fg-subtle tnum">{page + 1} ページ目</span>
          <button
            disabled={items.length < PAGE_SIZE}
            onClick={() => { setPage((n) => n + 1); window.scrollTo(0, 0); }}
            className="text-xs px-3 py-1.5 rounded border border-border-default text-fg-muted hover:text-accent disabled:opacity-40 transition-colors"
          >
            古い記事 →
          </button>
        </div>
      )}
    </div>
  );
}

/** 注目 = 複数媒体が報じ、かつ統合本文がある事象。裏取りのある話題を先頭に置く。 */
function FeaturedStrip() {
  const { data } = useQuery({
    queryKey: ["public-news-featured"],
    queryFn: () => fetchPublicNews({ limit: 3, featured: true }),
    staleTime: 10 * 60 * 1000,
  });
  const items = data?.items ?? [];
  if (items.length === 0) return null;
  return (
    <section className="space-y-2.5 pb-4 border-b border-border-subtle">
      <h2 className="text-xs font-semibold text-fg-muted tracking-wide">注目</h2>
      <ul className="space-y-3">
        {items.map((it) => (
          <li key={it.id}>
            <button
              onClick={() => navigate(`${HOME_PATH}/${encodeURIComponent(it.id)}`)}
              className="block w-full text-left group"
            >
              <h3 className="text-[15px] font-semibold leading-snug text-fg group-hover:text-accent transition-colors">
                {it.headline}
              </h3>
            </button>
            <div className="mt-1 flex flex-wrap items-center gap-x-2 text-[11px]">
              <span className="px-1.5 py-0.5 rounded bg-accent/10 text-accent border border-accent-soft">
                {it.independent_sources} 媒体が報道
              </span>
              <SourceLine citations={it.citations} total={it.sources} />
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}

function NewsListItem({ item }: { item: PublicNewsItem }) {
  return (
    <li className="py-4 first:pt-0">
      <button
        onClick={() => navigate(`${HOME_PATH}/${encodeURIComponent(item.id)}`)}
        className="block w-full text-left group"
      >
        <h2 className="text-base font-semibold leading-snug text-fg group-hover:text-accent transition-colors">
          {item.headline}
        </h2>
        {item.summary && (
          <p className="mt-1.5 text-sm leading-relaxed text-fg-muted line-clamp-3">{item.summary}</p>
        )}
      </button>
      <div className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px]">
        {item.independent_sources >= 2 && (
          <span className="px-1.5 py-0.5 rounded bg-accent/10 text-accent border border-accent-soft">
            {item.independent_sources} 媒体が報道
          </span>
        )}
        <SourceLine citations={item.citations} total={item.sources} />
        <span className="ml-auto text-fg-subtle shrink-0">{formatJstCompact(item.published_at)}</span>
      </div>
    </li>
  );
}

function NewsDetail({ id }: { id: string }) {
  const { data, isFetching, error } = useQuery({
    queryKey: ["public-news-detail", id],
    queryFn: () => fetchPublicNewsDetail(id),
  });

  useEffect(() => { window.scrollTo(0, 0); }, [id]);

  if (isFetching && !data) return <p className="text-sm text-fg-subtle">読み込み中…</p>;
  if (error || !data) {
    return (
      <div className="space-y-3">
        <p className="text-sm text-fg-muted">記事が見つかりませんでした。</p>
        <button onClick={() => navigate(HOME_PATH)} className="text-sm text-accent hover:underline">
          ← 一覧へ戻る
        </button>
      </div>
    );
  }

  return (
    <article className="space-y-5">
      <button
        onClick={() => navigate(HOME_PATH)}
        className="inline-flex items-center gap-1 text-xs text-fg-muted hover:text-accent transition-colors"
      >
        <ChevronLeft className="w-3.5 h-3.5" />
        一覧へ戻る
      </button>

      <header className="space-y-2">
        <h1 className="text-xl font-bold leading-snug text-fg">{data.headline}</h1>
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-fg-subtle">
          <span>{formatJstCompact(data.published_at)}</span>
          {data.independent_sources >= 2 && (
            <span className="px-1.5 py-0.5 rounded bg-accent/10 text-accent border border-accent-soft">
              独立 {data.independent_sources} 媒体
            </span>
          )}
        </div>
      </header>

      {data.bluf && <p className="text-[15px] leading-loose text-fg">{data.bluf}</p>}

      {data.facts.length > 0 && (
        <section className="space-y-1.5">
          <h2 className="text-xs font-semibold text-fg-muted">報じられている内容</h2>
          <ul className="space-y-1.5">
            {data.facts.map((f, i) => (
              <li key={i} className="text-sm leading-relaxed text-fg-muted">
                {f.text}
                {f.source_index > 0 && (
                  <sup className="ml-0.5 text-accent">[{f.source_index}]</sup>
                )}
              </li>
            ))}
          </ul>
        </section>
      )}

      {data.discrepancies.length > 0 && (
        <section className="space-y-1.5">
          <h2 className="text-xs font-semibold text-warning">媒体間で食い違う点</h2>
          <ul className="space-y-1 text-sm text-fg-muted">
            {data.discrepancies.map((d, i) => <li key={i}>{d}</li>)}
          </ul>
        </section>
      )}

      {data.unknowns.length > 0 && (
        <section className="space-y-1.5">
          <h2 className="text-xs font-semibold text-fg-muted">わかっていない点</h2>
          <ul className="space-y-1 text-sm text-fg-muted">
            {data.unknowns.map((u, i) => <li key={i}>{u}</li>)}
          </ul>
        </section>
      )}

      <section className="space-y-2 pt-2 border-t border-border-subtle">
        <h2 className="text-xs font-semibold text-fg-muted">出典 ({data.citations.length})</h2>
        <ol className="space-y-2">
          {data.citations.map((c) => (
            <li key={c.index} className="text-sm leading-relaxed">
              <span className="text-fg-subtle mr-1">[{c.index}]</span>
              <a
                href={c.url}
                target="_blank"
                rel="noopener noreferrer nofollow"
                className="text-fg hover:text-accent underline decoration-border-default underline-offset-2"
              >
                {c.title}
                <ExternalLink className="inline w-3 h-3 ml-0.5 align-baseline" />
              </a>
              {c.source && <span className="text-fg-subtle"> — {c.source}</span>}
            </li>
          ))}
        </ol>
      </section>

      <p className="text-[11px] text-fg-subtle leading-relaxed pt-2">{data.note}</p>
    </article>
  );
}
