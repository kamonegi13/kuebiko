// triage で落とした記事の一覧 (2026-10-02)。落選は既読化されて二度と評価されないため、
// 落とし方が正しいかを確かめる手段はこの一覧だけ。開いたときにだけ読み込む。
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { pagesApi } from "../../api/pages";
import { Spinner } from "../../components/Spinner";
import { vocabLabel } from "../../hooks/useVocab";
import { formatJstDate } from "../../utils/date";

export function TriageRejectionsSection({ feedKey, count }: { feedKey: string; count: number }) {
  const [open, setOpen] = useState(false);
  const { data, isFetching, isError } = useQuery({
    queryKey: ["triage_rejections", feedKey],
    queryFn: () => pagesApi.triageRejections(feedKey),
    enabled: open,
    staleTime: 60_000,
  });

  if (count === 0) return null;
  return (
    <div className="bg-surface-2 rounded p-3 space-y-2">
      <button
        onClick={() => setOpen((v) => !v)}
        className="w-full flex items-center justify-between text-left"
        aria-expanded={open}
      >
        <h4 className="m-0 text-[12.5px] uppercase tracking-wider text-fg-muted font-semibold">
          triage で不採用にした記事 ({count} 件・30 日)
        </h4>
        <span className="text-accent text-[13px]">{open ? "閉じる" : "理由を見る"}</span>
      </button>
      {open && isFetching && (
        <div className="flex items-center gap-2 text-xs text-fg-subtle">
          <Spinner /> 読み込み中…
        </div>
      )}
      {open && isError && <div className="text-xs text-critical">読み込みに失敗しました</div>}
      {open && data && (
        <ul className="m-0 p-0 list-none space-y-2">
          {data.items.map((r) => (
            <li key={`${r.article_id}-${r.ts}`} className="text-xs">
              <a
                href={r.url}
                target="_blank"
                rel="noopener noreferrer"
                className="text-fg hover:underline break-words"
              >
                {r.title}
              </a>
              <div className="text-fg-subtle mt-0.5">
                {formatJstDate(r.ts)} ・ {vocabLabel("importance", r.importance)} ・ {r.reason}
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
