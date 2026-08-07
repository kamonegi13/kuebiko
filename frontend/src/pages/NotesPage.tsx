import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { pageContainer } from "../components/Page";
import { formatJst } from "../utils/date";
import { fetchNotes, type NoteListItem } from "../api/article";
import { vocabLabel } from "../hooks/useVocab";

const IMPORTANCE_TONE: Record<string, string> = {
  high: "text-critical",
  medium: "text-warning",
  low: "text-fg-subtle",
};

export function NotesPage() {
  const [bookmarkedOnly, setBookmarkedOnly] = useState(false);
  const [tag, setTag] = useState("");

  const { data, isFetching, error } = useQuery({
    queryKey: ["notes", bookmarkedOnly, tag],
    queryFn: () => fetchNotes({ bookmarkedOnly, tag: tag.trim() || undefined }),
  });

  return (
    <div className={`${pageContainer("wide")} space-y-5`}>
      <div>
        <h2 className="m-0 text-xl font-bold text-fg tracking-tight">ブックマーク・メモ</h2>
        <p className="text-fg-muted text-sm mt-1">
          記事に残した個人メモ・ブックマーク・所見の一覧。記事詳細から編集できる。
        </p>
      </div>

      <div className="flex flex-wrap items-end gap-2 bg-surface-1 border border-border-subtle rounded-lg p-3">
        <button
          onClick={() => setBookmarkedOnly((b) => !b)}
          className={`text-sm px-3 py-1.5 rounded border transition-colors ${
            bookmarkedOnly ? "border-warning text-warning bg-warning/10" : "border-border-default text-fg-muted hover:text-fg"
          }`}
        >
          {bookmarkedOnly ? "ブックマークのみ" : "すべて"}
        </button>
        <label className="flex flex-col gap-1 text-xs text-fg-muted flex-1 min-w-[180px]">
          タグで絞り込み
          <input
            value={tag}
            onChange={(e) => setTag(e.target.value)}
            placeholder="例: apt / 要追跡"
            className="bg-surface-2 border border-border-default rounded px-2 py-1.5 text-sm text-fg"
          />
        </label>
      </div>

      {isFetching && <div className="text-fg-subtle text-sm">読み込み中…</div>}
      {error && <div className="text-critical text-sm">エラー: {String(error)}</div>}

      {data && (
        <div className="bg-surface-1 border border-border-subtle rounded-lg overflow-hidden">
          <div className="px-4 py-2 bg-surface-2 text-fg-muted text-xs uppercase border-b border-border-subtle">
            {data.count} 件
          </div>
          {data.notes.length === 0 && (
            <div className="px-4 py-8 text-center text-fg-subtle text-sm">
              メモ・ブックマークはまだありません。記事詳細ページで残せます。
            </div>
          )}
          <ul className="divide-y divide-border-subtle">
            {data.notes.map((n: NoteListItem) => (
              <li key={n.article_id} className="px-4 py-3 hover:bg-surface-2 transition-colors">
                <div className="flex items-start gap-2">
                  {n.bookmarked && <span className="text-warning shrink-0" title="ブックマーク">●</span>}
                  <a
                    href={`/app/article/${encodeURIComponent(n.article_id)}`}
                    className="text-sm text-fg hover:text-accent font-medium flex-1 min-w-0"
                  >
                    {n.title}
                  </a>
                  {n.updated_at && (
                    <span className="text-fg-subtle text-xs tnum shrink-0">{formatJst(n.updated_at)}</span>
                  )}
                </div>
                {n.note && <p className="text-xs text-fg-muted mt-1 whitespace-pre-wrap">{n.note}</p>}
                <div className="flex flex-wrap items-center gap-2 mt-1.5 text-xs">
                  {n.importance && (
                    <span className={`font-medium ${IMPORTANCE_TONE[n.importance] || "text-fg-subtle"}`}>
                      {vocabLabel("importance", n.importance)}
                    </span>
                  )}
                  {n.category && <span className="text-fg-subtle">{vocabLabel("category", n.category)}</span>}
                  {n.judgment && (
                    <span className="text-accent bg-accent-subtle rounded px-1.5 py-0.5">所見: {n.judgment}</span>
                  )}
                  {n.tags.map((t) => (
                    <button
                      key={t}
                      onClick={() => setTag(t)}
                      className="bg-surface-2 border border-border-default rounded px-1.5 py-0.5 text-fg-muted hover:text-accent hover:border-accent-soft transition-colors"
                    >
                      #{t}
                    </button>
                  ))}
                </div>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
