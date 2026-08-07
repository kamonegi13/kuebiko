// 記事詳細 (フル画面)。読み取り部は ArticleReadView (サイドピークと共有 SSoT) に一本化し、
// 本ページは編集系 (メモ・ブックマーク = NoteEditor) とページ枠のみを持つ (2026-07-31 分割)。

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { pageContainer } from "../components/Page";
import { ArticleReadView } from "./article/ArticleReadView";
import {
  fetchArticleDetail,
  saveArticleNote,
  type ArticleDetailResponse,
  type ArticleNote,
} from "../api/article";

function NoteEditor({ articleId, initial }: { articleId: string; initial: ArticleNote | null }) {
  const qc = useQueryClient();
  const [bookmarked, setBookmarked] = useState(initial?.bookmarked ?? false);
  const [note, setNote] = useState(initial?.note ?? "");
  const [tags, setTags] = useState((initial?.tags ?? []).join(", "));
  const [judgment, setJudgment] = useState(initial?.judgment ?? "");

  const save = useMutation({
    mutationFn: () =>
      saveArticleNote(articleId, {
        bookmarked,
        note,
        judgment,
        tags: tags.split(",").map((t) => t.trim()).filter(Boolean),
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["article-detail", articleId] }),
  });

  return (
    <div className="bg-surface-1 border border-border-subtle rounded-lg p-4 space-y-3">
      <div className="flex items-center justify-between">
        <div className="text-fg-muted text-xs uppercase">メモ・ブックマーク</div>
        <button
          onClick={() => setBookmarked((b) => !b)}
          className={`text-sm px-2 py-0.5 rounded border transition-colors ${
            bookmarked ? "border-warning text-warning bg-warning/10" : "border-border-default text-fg-subtle hover:text-fg"
          }`}
          title="ブックマーク"
        >
          {bookmarked ? "ブックマーク中" : "ブックマーク"}
        </button>
      </div>
      <textarea
        value={note}
        onChange={(e) => setNote(e.target.value)}
        placeholder="メモ (自由記述) — この記事についての気づき"
        rows={3}
        className="w-full bg-surface-2 border border-border-default rounded px-2 py-1.5 text-sm text-fg resize-y"
      />
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
        <input
          value={tags}
          onChange={(e) => setTags(e.target.value)}
          placeholder="タグ (カンマ区切り: apt, 要追跡)"
          className="bg-surface-2 border border-border-default rounded px-2 py-1.5 text-sm text-fg"
        />
        <input
          value={judgment}
          onChange={(e) => setJudgment(e.target.value)}
          placeholder="所見 (例: 誤検知 / 重要先例 / 要追跡)"
          className="bg-surface-2 border border-border-default rounded px-2 py-1.5 text-sm text-fg"
        />
      </div>
      <div className="flex items-center gap-3">
        <button
          onClick={() => save.mutate()}
          disabled={save.isPending}
          className="bg-accent text-on-accent text-sm font-medium px-4 py-1.5 rounded hover:opacity-90 transition-opacity disabled:opacity-50"
        >
          {save.isPending ? "保存中…" : "保存"}
        </button>
        {save.isSuccess && <span className="text-xs text-accent">保存しました</span>}
        {save.isError && <span className="text-xs text-critical">保存できませんでした (閲覧専用モードの可能性)</span>}
      </div>
    </div>
  );
}

interface ArticleDetailPageProps {
  articleId: string;
}

export function ArticleDetailPage({ articleId }: ArticleDetailPageProps) {
  const { data, isLoading, error } = useQuery<ArticleDetailResponse>({
    queryKey: ["article-detail", articleId],
    queryFn: () => fetchArticleDetail(articleId),
    retry: false,
  });

  if (isLoading) {
    return <div className={`${pageContainer("wide")} text-fg-subtle text-sm`}>読み込み中…</div>;
  }
  if (error || !data) {
    return (
      <div className={`${pageContainer("wide")} space-y-3`}>
        <div className="text-critical text-sm bg-surface-1 border border-border-subtle rounded-lg px-4 py-3">
          {String(error instanceof Error ? error.message : error)}
        </div>
        <a href="/app/news" className="text-accent hover:underline text-sm">→ ニュース・記事に戻る</a>
      </div>
    );
  }

  return (
    <div className={`${pageContainer("wide")} space-y-5`}>
      <ArticleReadView data={data} />
      {/* メモ・ブックマーク — 読了後に記録する流れなので最下部 (2026-07-25 順序是正) */}
      <NoteEditor articleId={articleId} initial={data.note} />
    </div>
  );
}
