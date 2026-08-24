// 事象単位の memo / bookmark。記事単位のメモ (article_notes) とは **粒度が違う** —
// 「この事象を継続監視する」という判断は、構成記事 1 本に付けるものではない。
//
// write は readonly instance では middleware が 403 で block する。公開面では
// 保存ボタンを押しても失敗するため、失敗を握りつぶさず理由を出す。

import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Bookmark } from "lucide-react";
import { fetchEventNote, saveEventNote, type EventNote } from "../../api/eventnews";

export function EventNoteEditor({ itemId }: { itemId: string }) {
  const qc = useQueryClient();
  const { data } = useQuery({
    queryKey: ["eventnote", itemId],
    queryFn: () => fetchEventNote(itemId),
  });
  const [note, setNote] = useState("");
  const [tags, setTags] = useState("");
  const [bookmarked, setBookmarked] = useState(false);
  const [dirty, setDirty] = useState(false);

  // サーバ値で初期化する。編集中 (dirty) は上書きしない — 保存前の入力を消さないため。
  useEffect(() => {
    if (!data || dirty) return;
    setNote(data.note);
    setTags(data.tags.join(", "));
    setBookmarked(data.bookmarked);
  }, [data, dirty]);

  const save = useMutation({
    mutationFn: (): Promise<EventNote> =>
      saveEventNote(itemId, {
        bookmarked,
        note,
        tags: tags.split(",").map((t) => t.trim()).filter(Boolean),
        judgment: data?.judgment ?? "",
      }),
    onSuccess: (saved) => {
      qc.setQueryData(["eventnote", itemId], saved);
      setDirty(false);
    },
  });

  const touched = (fn: () => void) => {
    setDirty(true);
    fn();
  };

  return (
    <div className="bg-surface-1 border border-border-subtle rounded-lg p-4 space-y-2">
      <div className="flex items-center justify-between gap-2">
        <div className="text-fg-muted text-xs uppercase">メモ・継続監視</div>
        <button
          onClick={() => touched(() => setBookmarked((b) => !b))}
          title={bookmarked ? "継続監視を外す" : "継続監視に入れる"}
          className={`inline-flex items-center gap-1 rounded px-2 py-0.5 text-xs border transition-colors ${
            bookmarked
              ? "bg-accent/10 border-accent-soft text-accent"
              : "bg-surface-2 border-border-default text-fg-muted hover:text-accent"
          }`}
        >
          <Bookmark className={`h-3.5 w-3.5 ${bookmarked ? "fill-current" : ""}`} />
          継続監視
        </button>
      </div>
      <textarea
        value={note}
        onChange={(e) => touched(() => setNote(e.target.value))}
        rows={3}
        placeholder="この事象についての所見・追跡メモ"
        className="w-full bg-surface-2 border border-border-default rounded px-2.5 py-1.5 text-sm text-fg placeholder:text-fg-subtle focus:border-accent outline-none resize-y"
      />
      <div className="flex flex-wrap items-center gap-2">
        <input
          value={tags}
          onChange={(e) => touched(() => setTags(e.target.value))}
          placeholder="タグ (カンマ区切り)"
          className="flex-1 min-w-[10rem] bg-surface-2 border border-border-default rounded px-2.5 py-1 text-xs text-fg placeholder:text-fg-subtle focus:border-accent outline-none"
        />
        <button
          onClick={() => save.mutate()}
          disabled={save.isPending || !dirty}
          className="bg-accent text-on-accent rounded px-3 py-1 text-xs font-medium hover:opacity-90 disabled:opacity-40 transition-opacity"
        >
          {save.isPending ? "保存中…" : dirty ? "保存" : "保存済み"}
        </button>
      </div>
      {save.isError && (
        <div className="text-critical text-xs">
          保存に失敗しました（公開用の閲覧専用インスタンスからは編集できません）
        </div>
      )}
      {data?.updated_at && !dirty && (
        <div className="text-fg-subtle text-[11px]">最終更新 {data.updated_at.slice(0, 16).replace("T", " ")}</div>
      )}
    </div>
  );
}
