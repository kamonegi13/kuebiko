// 記事サイドピーク (2026-07-31)。ニュース一覧・ダッシュボード等から記事を「画面遷移せずに」
// 読むための右スライドオーバー。表示は読み取り専用 (判定 / エンティティ / 要約 / 本文 =
// ArticleReadView 共有 SSoT)。コメント等の編集はフル画面 (/app/article/:id) に誘導する。
// パネル外枠は共通 Drawer (ぼかし backdrop / bg-bg / portal / Esc・scroll lock) を再利用し、
// 取込・処理履歴のドロワー等と見た目を統一する。
//
// 起動方式 = グローバル・クリックインターセプト: document で a[href^="/app/article/"] への
// 通常クリックを 1 箇所で捕捉して peek を開く。記事リンクは 18+ ファイルに散在するため、
// 個別改修でなく集約点で一括担保し、将来追加されるリンクも自動で peek 化する。
// - 修飾キー (Cmd/Ctrl/Shift/Alt)・中クリック・target=_blank は素通し (従来の遷移/新タブ)
// - data-peek-ignore 属性を持つリンクは素通し (peek 内の「記事画面で開く」等)

import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ExternalLink } from "lucide-react";
import { Drawer } from "./Drawer";
import { ArticleReadView } from "../pages/article/ArticleReadView";
import { fetchArticleDetail, type ArticleDetailResponse } from "../api/article";

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
  const [articleId, setArticleId] = useState<string | null>(null);

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
      setArticleId(decodeURIComponent(m[1]));
    };
    document.addEventListener("click", onClick);
    return () => document.removeEventListener("click", onClick);
  }, []);

  return (
    <Drawer
      isOpen={articleId !== null}
      onClose={() => setArticleId(null)}
      title="記事プレビュー"
      widthClass="md:w-[46rem]"
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
