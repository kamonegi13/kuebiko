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
  fetchPublicNewsDetail,
  type PublicCitation,
  type PublicNewsItem,
} from "../api/publicNews";
import { formatJstDate, relativeFromNow } from "../utils/date";
import { vocabLabel } from "../hooks/useVocab";
import { loginUrl } from "../hooks/useRuntimeFlags";
import { staticCategoryLabels } from "../api/publicNewsStatic";
import { PublicErrorBoundary } from "./PublicErrorBoundary";
import { Drawer } from "../components/Drawer";
import { PublicMapSection } from "./PublicMapSection";
import { categoryColor } from "./categoryColors";
import { buildSections } from "./sections";

const PAGE_SIZE = 24;
const FEATURED_COUNT = 3;
// トップに出す件数。多すぎると「入口」でなく一覧になってしまう
/** 開いたままのタブを更新する間隔。**一覧だけでなくトップページも更新する** —
 *  主導線であるトップが古いまま残ると、更新されていないサイトに見える
 *  (2026-08-26 利用者指摘)。記事詳細は開いている間に書き換わると読みにくいので除く。 */
const REFETCH_MS = 10 * 60 * 1000;

const PORTAL_LATEST_COUNT = 6;
const PORTAL_CATEGORY_COUNT = 4;
/** 公開面の基底パス。
 *
 * 運用者の PC で配信するとき (`/app/news`) と、Cloudflare Pages で配信するとき
 * (`/news`) で変わる。**経路の組み立てはすべてここを通す** — 直書きすると
 * 配信先を変えたときに一部のリンクだけ壊れる。 */
const HOME_PATH = import.meta.env.VITE_PUBLIC_BASE || "/app/news";

/** 運用者ログインの着地点。
 *
 * 静的配信 (Cloudflare Pages) の公開サイトには運用画面が無いので、tunnel 側の
 * ホストを指す。同一オリジンで配信しているとき (運用者の PC) は相対のままでよい。 */
function operatorLoginUrl(): string {
  return loginUrl(import.meta.env.VITE_OPERATOR_ORIGIN || "");
}

/** カテゴリの表示名。どの category を束ねるかの定義も表示名も backend が持つ。
 *
 * 静的配信 (Cloudflare Pages) では語彙 API を読めないので、書き出しに同梱された
 * ラベルを使う。**入れ忘れると内部 key がそのまま画面に出る**
 * (2026-08-26 実測: タブが vuln / incident_breach と英語で表示された)。 */
function categoryLabel(key: string): string {
  if (!key) return "";
  return (
    staticCategoryLabels[key] ||
    vocabLabel("category_group", key) ||
    vocabLabel("category", key) ||
    key
  );
}

type Route =
  | { kind: "home" }
  | { kind: "latest" }
  | { kind: "category"; key: string }
  | { kind: "map" }
  | { kind: "detail"; id: string };

/** 基底パスから経路の正規表現を組む (基底を直書きしない)。 */
function routePattern(suffix: string): RegExp {
  const base = HOME_PATH.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return new RegExp(`^${base}${suffix}/?$`);
}

function parseRoute(): Route {
  const p = window.location.pathname;
  if (routePattern("/map").test(p)) return { kind: "map" };
  if (routePattern("/latest").test(p)) return { kind: "latest" };
  const cat = routePattern("/c/([^/]+)").exec(p);
  if (cat) return { kind: "category", key: decodeURIComponent(cat[1]) };
  const detail = routePattern("/([^/]+)").exec(p);
  return detail ? { kind: "detail", id: decodeURIComponent(detail[1]) } : { kind: "home" };
}

/** 背後に出す面。詳細は「一覧の上に重なる」ので、その一覧が何かを決める。 */
function isPortal(route: Route): boolean {
  if (route.kind === "home") return true;
  if (route.kind !== "detail") return false;
  const q = new URLSearchParams(window.location.search);
  return !q.get("q") && !q.get("country");
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
            <PublicMapSection onCountry={(iso) => navigate(`${HOME_PATH}/latest?country=${iso}`)} />
          ) : isPortal(route) ? (
            /* トップは各カテゴリの最新を少数ずつ並べた **入口**。
               全件を追うのは「新着」タブ (/app/news/latest)。 */
            <Portal openedId={route.kind === "detail" ? route.id : undefined} />
          ) : (
            <NewsList
              category={route.kind === "category" ? route.key : undefined}
              /* 記事を開いていても一覧は裏に残す (閉じたとき位置が戻らないように) */
              openedId={route.kind === "detail" ? route.id : undefined}
            />
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
    <>
      {/* 題字は **スクロールで流す**。読み始めたら要らない (2026-08-25 利用者指摘)。 */}
      <header className="w-full max-w-[72rem] mx-auto px-5">
        <div className="flex items-baseline gap-2.5 pt-4 pb-3">
          <button
            onClick={() => navigate(HOME_PATH)}
            className="text-[17px] font-bold tracking-tight text-fg [@media(hover:hover)]:hover:text-accent transition-colors"
          >
            kuebiko
          </button>
          <span className="text-[11px] text-fg-subtle">サイバー脅威ニュース</span>
        </div>
      </header>
      {/* 追従するのは **ナビだけ**。地図の Leaflet が z-index 400+ を使うので z-20 を保つ */}
      <div className="sticky top-0 z-20 border-b border-border-subtle bg-surface-1/95 backdrop-blur-md">
        {/* 追従時にタブが画面の上端へ貼り付かないよう、内側に上余白を取る
            (2026-08-25 利用者指摘)。iOS のブラウザ枠の直下でも詰まって見えない。 */}
        <div className="w-full max-w-[72rem] mx-auto px-5 pt-3">
          <CategoryNav
            active={route.kind === "category" ? route.key : undefined}
            onMap={route.kind === "map"}
            home={isPortal(route)}
            latest={route.kind === "latest"}
          />
        </div>
      </div>
    </>
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
          {/* ⚠ 着地点は `/auth/login`。`/auth/` は Access の保護対象ではあるが
              アプリにルートが無く、**認証を通過した直後に 404 になる**
              (2026-08-26 実測)。運用画面は別ホスト (tunnel 経由) にあるので
              絶対 URL で指す — 静的配信の公開サイトには運用画面が無い。 */}
          <a
            href={operatorLoginUrl()}
            className="hover:text-accent underline underline-offset-2"
          >
            運用者ログイン
          </a>
        </p>
      </div>
    </footer>
  );
}

/** カテゴリの並びは backend が返す順をそのまま使う (定義を frontend に複製しない)。 */
function CategoryNav({
  active,
  onMap,
  home,
  latest,
}: {
  active?: string;
  onMap: boolean;
  home: boolean;
  latest: boolean;
}) {
  const { data } = useQuery({
    queryKey: ["public-news-categories"],
    queryFn: () => fetchPublicNews({ limit: 1 }),
    staleTime: 30 * 60 * 1000,
    refetchInterval: REFETCH_MS,
  });
  const keys = data?.categories ?? [];
  if (keys.length === 0) return null;
  return (
    /* ⚠ `flex-wrap` と `overflow-x-auto` を併用しない。CSS では片方の軸が visible で
       なくなるともう一方も auto になるため、折り返した瞬間に **縦のスクロールバー**が
       出る (2026-08-25 実機で発生)。横 1 列 + 横スクロールに固定し、バーは隠す。 */
    <nav className="flex flex-nowrap gap-x-4 overflow-x-auto overflow-y-hidden [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
      <CategoryTab href={HOME_PATH} label="ホーム" active={home} />
      <CategoryTab href={`${HOME_PATH}/latest`} label="新着" active={latest} />
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
 * トップページ = 各カテゴリへの **入口**。
 *
 * 全件を時系列で追う面 (新着タブ) とは役割を分ける。トップに無限の一覧を置くと
 * 「今どの分野で何が起きているか」が掴めないため、カテゴリごとに最新を少数ずつ
 * 並べ、それぞれの一覧へ送る (参照サイト cyber.nexsight.co と同じ構成)。
 */
function Portal({ openedId }: { openedId?: string }) {
  const { data: featured } = useQuery({
    queryKey: ["public-news-featured"],
    queryFn: () => fetchPublicNews({ limit: FEATURED_COUNT, featured: true }),
    staleTime: 10 * 60 * 1000,
    refetchInterval: REFETCH_MS,
  });
  const { data: latest } = useQuery({
    queryKey: ["public-news-portal-latest"],
    queryFn: () => fetchPublicNews({ limit: PORTAL_LATEST_COUNT + FEATURED_COUNT }),
    staleTime: 5 * 60 * 1000,
    refetchInterval: REFETCH_MS,
  });

  const featuredItems = featured?.items ?? [];
  const seen = new Set(featuredItems.map((i) => i.id));
  const latestItems = (latest?.items ?? []).filter((i) => !seen.has(i.id)).slice(0, PORTAL_LATEST_COUNT);
  const categories = latest?.categories ?? [];

  return (
    <div className="space-y-12">
      {featuredItems.length > 0 && (
        <section className="space-y-5">
          <div className="flex items-center gap-2">
            <span
              className="w-[3px] h-[15px] rounded-full shrink-0"
              style={{ background: "var(--color-accent, #e0603a)" }}
              aria-hidden
            />
            <h2 className="text-[15px] font-bold tracking-wide text-fg">注目</h2>
            <span className="text-[11px] text-fg-subtle">
              直近 72 時間で多くの媒体が報じた事案
            </span>
            <span className="flex-1 border-b border-border-subtle" />
          </div>
          <LeadStory item={featuredItems[0]} />
          {featuredItems.length > 1 && (
            <ul className="grid gap-x-8 gap-y-5 md:grid-cols-2 pt-6 border-t border-border-subtle">
              {featuredItems.slice(1).map((it) => (
                <li key={it.id}>
                  <NewsCard item={it} opened={it.id === openedId} />
                </li>
              ))}
            </ul>
          )}
        </section>
      )}

      <PortalSection title="新着" href={`${HOME_PATH}/latest`}>
        <ul className="grid gap-x-8 gap-y-6 md:grid-cols-2">
          {latestItems.map((it) => (
            <li key={it.id}>
              <NewsCard item={it} opened={it.id === openedId} />
            </li>
          ))}
        </ul>
      </PortalSection>

      {/* カテゴリごとの入口。PC は 2 列に並べて幅を使う */}
      <div className="grid gap-x-10 gap-y-12 lg:grid-cols-2">
        {categories.map((key) => (
          <CategoryTeaser key={key} categoryKey={key} openedId={openedId} />
        ))}
      </div>
    </div>
  );
}

/** 節の枠 (見出し + 一覧への導線 + 罫線)。 */
function PortalSection({
  title,
  href,
  color,
  boxed,
  children,
}: {
  title: string;
  href: string;
  /** 節の識別色。省略時はアクセント色 (注目・新着など分類でない節)。 */
  color?: string;
  /** true でカード状の面に載せる。**分類の区画だけ**に使う
   *  (注目・新着まで面にすると 6 個の箱が並んで、かえって区切りが読めなくなる)。 */
  boxed?: boolean;
  children: React.ReactNode;
}) {
  return (
    /* ⚠ 節を **淡い面** で囲って区画にする。全部が地の色だとどこで節が切り替わった
       のか分からない (利用者指摘)。ただし塗りは薄く保つ — 4 区画が濃く塗られると
       今度は面同士がうるさくなり、記事の見出しが沈む。 */
    <section
      className={
        boxed
          ? "rounded-lg border border-border-subtle bg-surface-2/40 p-4 space-y-4"
          : "space-y-4 pt-2"
      }
    >
      <div className="flex items-center gap-2">
        <span
          className="w-[3px] h-[15px] rounded-full shrink-0"
          style={{ background: color ?? "var(--color-accent, #e0603a)" }}
          aria-hidden
        />
        <h2 className="text-[15px] font-bold tracking-wide text-fg">{title}</h2>
        <span className="flex-1 border-b border-border-subtle" />
        <a
          href={href}
          onClick={(e) => {
            e.preventDefault();
            navigate(href);
          }}
          className="shrink-0 text-[11px] text-fg-subtle hover:text-accent"
        >
          一覧へ →
        </a>
      </div>
      {children}
    </section>
  );
}

/** 1 カテゴリの入口: 先頭 1 本を大きく + 残りを見出しだけ。 */
function CategoryTeaser({ categoryKey, openedId }: { categoryKey: string; openedId?: string }) {
  const { data } = useQuery({
    queryKey: ["public-news-teaser", categoryKey],
    queryFn: () => fetchPublicNews({ limit: PORTAL_CATEGORY_COUNT, category: categoryKey }),
    staleTime: 5 * 60 * 1000,
    refetchInterval: REFETCH_MS,
  });
  const items = data?.items ?? [];
  if (items.length === 0) return null;
  const [head, ...rest] = items;
  return (
    <PortalSection
      title={categoryLabel(categoryKey)}
      href={`${HOME_PATH}/c/${encodeURIComponent(categoryKey)}`}
      color={categoryColor(categoryKey)}
      boxed
    >
      <div className="space-y-5">
        {/* 節の見出しが既にカテゴリを示しているのでバッジは出さない (重複) */}
        <NewsCard item={head} opened={head.id === openedId} hideCategory />
        {rest.length > 0 && (
          /* ⚠ 先頭カードの **要約と同じ色・同じ字送り**にしない。13px の text-fg-muted
             だと本文の続きに見えて、見出しの一覧だと分からなくなる (利用者指摘)。
             行頭記号 + 通常色 + 中太 で「別の記事の見出し」だと分かる形にする。 */
          <ul className="divide-y divide-border-subtle border-t border-border-subtle">
            {rest.map((it) => (
              <li key={it.id}>
                <button
                  onClick={() => navigate(`${HOME_PATH}/${encodeURIComponent(it.id)}`)}
                  className="group w-full flex items-baseline gap-2 py-2.5 text-left transition-colors"
                >
                  <span className="text-[9px] text-fg-subtle [@media(hover:hover)]:group-hover:text-accent shrink-0">
                    ●
                  </span>
                  <span
                    className={`flex-1 text-[13.5px] font-medium leading-[1.6] [@media(hover:hover)]:group-hover:text-accent ${
                      it.id === openedId ? "text-fg-subtle" : "text-fg"
                    }`}
                  >
                    {it.headline}
                  </span>
                  <time
                    dateTime={it.published_at}
                    className="shrink-0 text-[10px] text-fg-subtle tnum"
                  >
                    {formatJstDate(it.published_at)}
                  </time>
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </PortalSection>
  );
}

/** カテゴリバッジ。一覧で「何の話か」を最初に示す唯一のメタ情報。 */
function CategoryBadge({ category }: { category: string }) {
  const label = categoryLabel(category);
  if (!label) return null;
  // 節の識別色と同じ色を使い、「どの区画の記事か」を一覧でも保つ
  return (
    <span
      className="inline-flex items-center gap-1.5 text-[11px] font-semibold tracking-wide text-fg-muted"
    >
      <span
        className="w-1.5 h-1.5 rounded-full"
        style={{ background: categoryColor(category) }}
        aria-hidden
      />
      {label}
    </span>
  );
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
    refetchInterval: REFETCH_MS,
  });

  const items = data?.items ?? [];

  return (
    <div className="space-y-8">
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
            <li key={it.id}>
              <NewsCard item={it} opened={it.id === openedId} />
            </li>
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
        <h2 className="text-[22px] lg:text-[26px] font-bold leading-[1.4] text-fg [@media(hover:hover)]:group-hover:text-accent transition-colors">
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

function NewsCard({
  item,
  opened,
  hideCategory,
}: {
  item: PublicNewsItem;
  opened?: boolean;
  hideCategory?: boolean;
}) {
  // ⚠ ここで <li> を返さない。カテゴリ節では <ul> の外 (先頭記事) にも置くため、
  // 裸の <li> がブラウザ既定のマーカー (●) を出してしまう (2026-08-25 利用者指摘)。
  // リストに入れるのは呼び手の責務。
  return (
    <article className={opened ? "opacity-60" : undefined}>
      <button
        onClick={() => navigate(`${HOME_PATH}/${encodeURIComponent(item.id)}`)}
        className="block w-full text-left group space-y-1.5"
      >
        {!hideCategory && <CategoryBadge category={item.category} />}
        <h3 className="text-[16px] font-semibold leading-[1.5] text-fg [@media(hover:hover)]:group-hover:text-accent transition-colors">
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
  );
}

/** 読了目安 (日本語は 1 分あたり約 500 字)。参照サイトと同じく目安として出す。 */
function readingMinutes(data: {
  bluf: string;
  key_points?: string[];
  facts: { text: string }[];
}): number {
  const chars =
    data.bluf.length +
    (data.key_points ?? []).reduce((n, p) => n + p.length, 0) +
    data.facts.reduce((n, f) => n + f.text.length, 0);
  return Math.max(1, Math.round(chars / 500));
}

/**
 * 記事冒頭の要約 (BLUF)。本文と地の色を変え、拾い読みでも目に入るようにする。
 *
 * ⚠ ラベルは **「要約」**。中身は 2〜3 文の散文で、結論を先に述べたもの。
 * 参照サイトの "Key points" は箇条書きの要点だが、こちらは形が違う。
 * **要点と要約は別物**なので「要点」とは呼ばない (2026-08-25 利用者指摘)。
 * サイトの他の箇所 (フッタ・注記) も「生成した要約」で統一している。
 */
function LeadSummary({ text }: { text: string }) {
  return (
    <div className="rounded-lg border border-border-subtle bg-surface-2 px-4 py-3.5">
      <p className="text-[11px] font-semibold tracking-wide text-fg-subtle mb-1.5">要約</p>
      <p className="text-[15px] leading-[1.95] text-fg">{text}</p>
    </div>
  );
}

/** 拾い読み用の**要点**。要約 (散文) とは別物で、参照サイトの "Key points" に当たる。
 *
 * 2026-08-26 まで生成側は要点を作っていたが、識別子関門が draft を組み直すときに
 * 渡し忘れており **本番 542 版すべてでキーごと消えていた**。過去の版には無いので
 * 空配列で必ず落ちること (`key_points` が無い版は何も描かない)。
 */
function KeyPoints({ points }: { points: string[] }) {
  if (points.length === 0) return null;
  return (
    <div className="rounded-lg border border-accent/25 bg-accent/[0.06] px-4 py-3.5">
      <p className="text-[11px] font-semibold tracking-wide text-accent mb-2">要点</p>
      <ul className="space-y-1.5">
        {points.map((point, i) => (
          <li key={i} className="flex gap-2 text-[14px] leading-[1.85] text-fg">
            <span aria-hidden className="mt-[0.55em] size-1.5 shrink-0 rounded-full bg-accent/70" />
            <span>{point}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

/** 事実行 1 段落。**同じ段落の文は連結して散文にする** (行ごとに割らない)。
 *
 * ⚠ 出典が 1 件しかない記事では **番号を出さない**。全部 [1] になり情報を持たない
 * うえ、読みの邪魔になる (2026-08-26 利用者指摘)。データ側の `source_index` は
 * 検証のためそのまま保持し、**表示だけ抑制する**。
 */
function FactParagraph({
  facts,
  showCitations,
}: {
  facts: { text: string; source_index: number }[];
  showCitations: boolean;
}) {
  return (
    <p className="text-[14px] leading-[2] text-fg-muted indent-[1em]">
      {facts.map((f, i) => (
        <span key={i}>
          {f.text}
          {showCitations && f.source_index > 0 && (
            <sup className="ml-0.5 text-accent tnum">[{f.source_index}]</sup>
          )}
        </span>
      ))}
    </p>
  );
}

/** 記事本文の節。見出しと罫線で区切り、節どうしの間隔を広めに取る。 */
function ArticleSection({
  title,
  tone,
  children,
}: {
  title: string;
  tone?: "warning";
  children: React.ReactNode;
}) {
  return (
    <section className="space-y-3 pt-5 border-t border-border-subtle">
      <h2
        className={`text-[13px] font-semibold ${tone === "warning" ? "text-warning" : "text-fg-muted"}`}
      >
        {title}
      </h2>
      {children}
    </section>
  );
}

/** 事実行を paragraph 番号でまとめる (順序は生成時のまま)。 */
function groupByParagraph<T extends { paragraph: number }>(facts: T[]): T[][] {
  const out: T[][] = [];
  let current = -1;
  for (const f of facts) {
    if (f.paragraph !== current) {
      out.push([]);
      current = f.paragraph;
    }
    out[out.length - 1].push(f);
  }
  return out;
}

/** 出典の並び。**詳細でだけ** 見せる。 */
function Citations({ citations }: { citations: PublicCitation[] }) {
  // 1 件なら番号を振らない (本文側でも番号を出していないため対応が付かない)
  const numbered = citations.length > 1;
  return (
    <ArticleSection title={numbered ? `出典 (${citations.length})` : "出典"}>
      <ol className={numbered ? "space-y-2.5" : "space-y-2.5 list-none"}>
        {citations.map((c) => (
          <li
            key={c.index}
            className={`text-[13px] leading-[1.7] ${numbered ? "pl-7 -indent-7" : ""}`}
          >
            {numbered && <span className="text-fg-subtle mr-1.5 tnum">[{c.index}]</span>}
            <a
              href={c.url}
              target="_blank"
              rel="noopener noreferrer nofollow"
              className="text-fg hover:text-accent underline decoration-border-default underline-offset-2"
            >
              {c.title}
              <ExternalLink className="inline w-3 h-3 ml-1 align-baseline" />
            </a>
            {c.source && (
              <div className="text-[11px] text-fg-subtle mt-0.5 indent-0">{c.source}</div>
            )}
          </li>
        ))}
      </ol>
    </ArticleSection>
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

  const paragraphs = data ? groupByParagraph(data.facts) : [];
  // v4 以降は節を持つ。持たない記事 (既存) は節見出し無しで従来どおり描く
  const sections = data ? buildSections(data.facts) : [];
  // 出典が 1 件なら番号は情報を持たない (全部 [1] になる)
  const showCitations = (data?.citations.length ?? 0) > 1;

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
      <header className="space-y-3 pb-1">
        <CategoryBadge category={data.category} />
        <h1 className="text-[24px] lg:text-[27px] font-bold leading-[1.45] text-fg">
          {data.headline}
        </h1>
        {/* メタ行: 日付 / 読了目安 / 裏取り。分析的な情報は開いた人にだけ見せる */}
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-fg-subtle">
          <time dateTime={data.published_at}>{formatJstDate(data.published_at)}</time>
          <span>約 {readingMinutes(data)} 分で読めます</span>
          {data.independent_sources >= 2 && <span>独立 {data.independent_sources} 媒体が報道</span>}
        </div>
      </header>

      {data.bluf && <LeadSummary text={data.bluf} />}
      <KeyPoints points={data.key_points ?? []} />

      {sections.length > 0
        ? sections.map((sec) => (
            <ArticleSection key={sec.key} title={sec.label}>
              <div className="space-y-4">
                {sec.paragraphs.map((facts, i) => (
                  <FactParagraph key={i} facts={facts} showCitations={showCitations} />
                ))}
              </div>
            </ArticleSection>
          ))
        : paragraphs.length > 0 && (
            /* 節を持たない記事 (v4 より前に生成) は従来どおり段落だけで描く */
            <ArticleSection title="報じられている内容">
              <div className="space-y-4">
                {paragraphs.map((facts, i) => (
                  <FactParagraph key={i} facts={facts} showCitations={showCitations} />
                ))}
              </div>
            </ArticleSection>
          )}

      {/* 原文が付けた但し書き。**本文の直後・食い違いより前**に置く —
          数字の読み方を限定する情報なので、数字を読んだ直後に目に入る必要がある。 */}
      {(data.caveats ?? []).length > 0 && (
        <ArticleSection title="読むうえでの但し書き">
          <ul className="space-y-2 text-[14px] leading-[1.9] text-fg-muted">
            {(data.caveats ?? []).map((c, i) => (
              <li key={i} className="flex gap-2">
                <span aria-hidden className="mt-[0.7em] size-1 shrink-0 rounded-full bg-fg-subtle" />
                <span>{c.text}</span>
              </li>
            ))}
          </ul>
        </ArticleSection>
      )}

      {data.discrepancies.length > 0 && (
        <ArticleSection title="媒体間で食い違う点" tone="warning">
          <ul className="space-y-2 text-[14px] leading-[1.9] text-fg-muted">
            {data.discrepancies.map((d, i) => (
              <li key={i} className="pl-4 -indent-4">
                <span className="text-fg-subtle">・</span>
                {d.text}
                {showCitations && d.source_index > 0 && (
                  <sup className="ml-0.5 text-accent tnum">[{d.source_index}]</sup>
                )}
              </li>
            ))}
          </ul>
        </ArticleSection>
      )}

      {data.unknowns.length > 0 && (
        <ArticleSection title="わかっていない点">
          <ul className="space-y-2 text-[14px] leading-[1.9] text-fg-muted">
            {data.unknowns.map((u, i) => (
              <li key={i} className="pl-4 -indent-4">
                <span className="text-fg-subtle">・</span>
                {u}
              </li>
            ))}
          </ul>
        </ArticleSection>
      )}

      <Citations citations={data.citations} />

      <p className="text-[11px] leading-relaxed text-fg-subtle">{data.note}</p>
    </article>
  );
}
