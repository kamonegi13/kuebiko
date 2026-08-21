// 記事サイドピーク (2026-07-31)。ニュース一覧・ダッシュボード等から記事を「画面遷移せずに」
// 読むための右スライドオーバー。表示は読み取り専用 (判定 / エンティティ / 要約 / 本文 =
// ArticleReadView 共有 SSoT)。コメント等の編集はフル画面 (/app/article/:id) に誘導する。
// パネル外枠は共通 Drawer (ぼかし backdrop / bg-bg / portal / Esc・scroll lock) を再利用し、
// 取込・処理履歴のドロワー等と見た目を統一する。mobileGutter/swipeToClose を有効化し、
// モバイルでの誤操作 (フルスクリーンに見えてオーバーレイと気付かれず、ブラウザバックを
// 押してしまう) 対策としてオーバーレイ視覚化 + 右フリック close を付ける。
//
// 起動方式 = グローバル・クリックインターセプト: document で a[href^="/app/article/"] への
// 通常クリックを 1 箇所で捕捉して peek を開く。記事リンクは 18+ ファイルに散在するため、
// 個別改修でなく集約点で一括担保し、将来追加されるリンクも自動で peek 化する。
// - 修飾キー (Cmd/Ctrl/Shift/Alt)・中クリック・target=_blank は素通し (従来の遷移/新タブ)
// - data-peek-ignore 属性を持つリンクは素通し (peek 内の「記事画面で開く」等)
//
// history 統合 (ブラウザバックで閉じる、2026-08-21): peek を開く瞬間に
// history.pushState で 1 段積む (pathname は変えない — App.tsx の popstate handler は
// 同じ route を再計算するだけの no-op になる)。close は必ず history.back() 経由にし、
// 実際に store を閉じるのは popstate handler だけに一本化する (X ボタン/backdrop/
// フリックのどれで閉じても history が 1 段しかない前提を崩さない — 二重 push/pop の
// 温床になるため close 系コードパスで直接 store.close() を呼ばない)。
// 既に開いている状態で別記事へ切り替えるとき (peek 内から別リンクを踏む等) は
// push しない — 「peek が開いている」で 1 段、という不変量を保つ。
//
// 再入 / 競合対策 (2026-08-21 レビュー対応):
// - history.back() は非同期にしか解決しない (popstate は次 tick 以降) ため、store の
//   articleId を再入 guard に使うと×の二重タップ/backdrop 連打で back() が 2 回呼ばれ、
//   本アプリの実ナビは full page load なので 2 段 pop すると別ページへ飛んでしまう。
//   同期に確定する closeRequestedRef (latch) で guard する — 1 回目の requestClose で
//   即座に立て、対応する popstate が来るまでは以後の requestClose を無視する。
// - latch が立っている間 (close が未解決) に別記事を open しようとすると、その
//   push/close 処理が popstate と競合して壊れる。latch が立っている間の open は
//   pendingOpenRef に積むだけにし、popstate handler が「close → latch clear →
//   pendingOpen があれば open + push」の順で逐次処理する (single-writer 設計を維持)。
// - リロード対策: peek 表示中にページをリロードすると、pushState した
//   `{articlePeek:true}` の marker が history.state に残ったまま JS だけが再初期化される
//   (store は必ず articleId:null で立ち上がる)。この「幽霊 1 段」を放置するとバックが
//   2 回必要になるため、mount 時に history.state を検査して replaceState で潰す。

import { useCallback, useEffect, useRef } from "react";
import { useQuery } from "@tanstack/react-query";
import { ExternalLink } from "lucide-react";
import { Drawer } from "./Drawer";
import { ArticleReadView } from "../pages/article/ArticleReadView";
import { fetchArticleDetail, type ArticleDetailResponse } from "../api/article";
import { useArticlePeekStore } from "../state/articlePeek";

const ARTICLE_HREF_RE = /^\/app\/article\/(.+?)(?:[?#].*)?$/;

function PeekContent({ articleId }: { articleId: string }) {
  const { data, isLoading, error } = useQuery<ArticleDetailResponse>({
    queryKey: ["article-detail", articleId],
    queryFn: () => fetchArticleDetail(articleId),
    retry: false,
  });

  if (isLoading) return <div className="text-fg-subtle text-sm">読み込み中…</div>;
  if (error || !data) {
    return (
      <div className="text-critical text-sm bg-surface-1 border border-border-subtle rounded-lg px-4 py-3">
        {String(error instanceof Error ? error.message : "記事を取得できませんでした")}
      </div>
    );
  }
  // ピークは本文を個別スクロールさせず全文フロー表示 (パネル全体が単一スクロール)
  return <ArticleReadView data={data} variant="peek" />;
}

/** App 直下に 1 度だけ置く。記事リンククリックの捕捉と peek の表示を担う。 */
export function ArticlePeekHost() {
  const articleId = useArticlePeekStore((s) => s.articleId);

  // close 要求が history.back() を投げてから対応する popstate が来るまでの間 true。
  // 同期に確定するため再入 guard として使える (store.articleId は popstate まで
  // 非同期にしか変わらないので guard には使えない)。
  const closeRequestedRef = useRef(false);
  // latch が立っている間に open が要求された記事 id。popstate で close を処理した
  // 直後にまとめて開く (先に close を消化してから push しないと history が壊れる)。
  const pendingOpenRef = useRef<string | null>(null);

  // マウント時の幽霊 history 段の掃除: peek 表示中にリロードすると、直前セッションで
  // push した {articlePeek:true} marker が history.state に残ったまま JS だけ再初期化
  // される (store は常に articleId:null で立ち上がる)。放置するとバックが 2 回要る。
  useEffect(() => {
    const state = history.state as { articlePeek?: boolean } | null;
    if (state?.articlePeek && useArticlePeekStore.getState().articleId === null) {
      history.replaceState(null, "");
    }
  }, []);

  useEffect(() => {
    const onClick = (e: MouseEvent) => {
      // 修飾キー・中クリック・既に処理済みのイベントは素通し (新タブ等の既定挙動を尊重)
      if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey)
        return;
      const target = e.target as Element | null;
      const anchor = target?.closest?.("a[href]");
      if (!anchor) return;
      if (anchor.hasAttribute("data-peek-ignore")) return;
      if (anchor.getAttribute("target") === "_blank") return;
      const m = ARTICLE_HREF_RE.exec(anchor.getAttribute("href") ?? "");
      if (!m) return;
      e.preventDefault();
      const id = decodeURIComponent(m[1]);
      if (closeRequestedRef.current) {
        // close の popstate 解決待ち。今 open すると push が壊れるので、popstate 処理後に
        // まとめて開く (single-writer 設計を維持)。
        pendingOpenRef.current = id;
        return;
      }
      const store = useArticlePeekStore.getState();
      const wasOpen = store.articleId !== null;
      store.open(id);
      // 「peek が開いている」で history 1 段、という不変量。既に開いていて別記事へ
      // 切り替えるだけなら積み増さない。
      if (!wasOpen) history.pushState({ articlePeek: true }, "");
    };
    document.addEventListener("click", onClick);
    return () => document.removeEventListener("click", onClick);
  }, []);

  // ブラウザバック (および close 系コードパスが呼ぶ history.back()) で積んだ 1 段を
  // 消費したときに実際に閉じる、唯一の実行箇所。pathname は変えていないため
  // App.tsx 側の popstate handler は同じ route を再計算するだけで無害。
  useEffect(() => {
    const onPopState = () => {
      closeRequestedRef.current = false;
      if (useArticlePeekStore.getState().articleId !== null) {
        useArticlePeekStore.getState().close();
      }
      const pending = pendingOpenRef.current;
      if (pending !== null) {
        pendingOpenRef.current = null;
        useArticlePeekStore.getState().open(pending);
        history.pushState({ articlePeek: true }, "");
      }
    };
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, []);

  // X ボタン / backdrop タップ / 右フリックのすべてがここを通る。直接 close() せず
  // history.back() だけを呼び、上の popstate handler に実処理を一本化する。
  // latch で再入を防ぐ (二重タップで back() が 2 回飛ぶと実ナビが 2 段戻ってしまう)。
  // useCallback で参照を安定させる (Drawer の ESC/scroll-lock effect の deps に
  // onClose が入っているため、毎 render 新しい関数だと effect が張り直され続ける)。
  const requestClose = useCallback(() => {
    if (closeRequestedRef.current) return;
    if (useArticlePeekStore.getState().articleId === null) return;
    closeRequestedRef.current = true;
    history.back();
  }, []);

  return (
    <Drawer
      isOpen={articleId !== null}
      onClose={requestClose}
      title="記事プレビュー"
      widthClass="md:w-[46rem]"
      mobileGutter
      swipeToClose
      headerExtra={
        articleId !== null && (
          <a
            href={`/app/article/${encodeURIComponent(articleId)}`}
            data-peek-ignore
            className="inline-flex items-center gap-1.5 bg-accent text-on-accent rounded px-3 py-1 text-xs font-medium hover:opacity-90 transition-opacity"
            title="フル画面で開く (メモ・ブックマーク編集)"
          >
            <ExternalLink className="h-3.5 w-3.5" />
            記事画面で開く
          </a>
        )
      }
    >
      {articleId !== null && <PeekContent articleId={articleId} />}
    </Drawer>
  );
}
