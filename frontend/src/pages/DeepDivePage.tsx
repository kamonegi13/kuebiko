// 週次深掘り (2026-09-27)。本文は「ブリーフ・振り返り」の週次表示に埋もれていて、
// 読む場所が実質無かった。週ごとに本文と、選んだ記事 (得点の高い順) を並べる。

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { PageHeader, pageContainer } from "../components/Page";
import { MarkdownText } from "../components/MarkdownText";
import { fetchDeepDives, type DeepDiveWeek } from "../api/deepDives";
import { formatJst } from "../utils/date";
import { vocabLabel } from "../hooks/useVocab";

const IMPORTANCE_TONE: Record<string, string> = {
  high: "text-critical",
  medium: "text-warning",
  low: "text-fg-subtle",
};

function Selections({ week }: { week: DeepDiveWeek }) {
  if (week.selections.length === 0) return null;
  return (
    <details className="mt-3">
      <summary className="cursor-pointer text-sm text-fg-muted hover:text-fg">
        選んだ記事 {week.selections.length} 件 (候補 {week.candidate_count} 件から)
      </summary>
      <ol className="mt-2 space-y-1.5 pl-5 list-decimal">
        {week.selections.map((s) => (
          <li key={s.article_id} className="text-sm">
            <a href={`/app/article/${encodeURIComponent(s.article_id)}`} className="text-fg hover:text-accent">
              {s.title || s.article_id}
            </a>
            <span className="ml-2 text-[12px] text-fg-subtle">
              {s.feed_title}
              {s.importance && (
                <span className={`ml-2 ${IMPORTANCE_TONE[s.importance] ?? ""}`}>
                  {vocabLabel("importance", s.importance)}
                </span>
              )}
              <span className="ml-2 tnum" title="選定の得点 (SIR 関連度・調べる価値、各 0-5)">
                得点 {s.composite.toFixed(1)} (SIR {s.pir} / 価値 {s.roi})
              </span>
            </span>
          </li>
        ))}
      </ol>
    </details>
  );
}

function WeekCard({ week }: { week: DeepDiveWeek }) {
  const empty = week.recap_text.trim().length < 100;
  return (
    <article className="bg-surface-1 border border-border-subtle rounded-lg p-4">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h3 className="m-0 text-base font-semibold text-fg">{week.period_label}</h3>
        <span className="text-[12px] text-fg-subtle">生成 {formatJst(week.generated_at)}</span>
      </div>
      {empty ? (
        <p className="mt-2 text-sm text-fg-subtle">この週は本文が生成されませんでした。</p>
      ) : (
        <MarkdownText className="mt-3">{week.recap_text}</MarkdownText>
      )}
      <Selections week={week} />
    </article>
  );
}

export function DeepDivePage() {
  const [weeks, setWeeks] = useState(8);
  const { data, isFetching, error } = useQuery({
    queryKey: ["deep-dives", weeks],
    queryFn: () => fetchDeepDives(weeks),
  });

  return (
    <div className={`${pageContainer("narrow")} space-y-5`}>
      <PageHeader
        title="週次深掘り"
        subtitle="速報では扱わなかったが知っておくべき記事を、週ごとに主題で束ねて解説したもの。"
      />
      {isFetching && !data && <div className="text-fg-subtle text-sm">読み込み中…</div>}
      {error && <div className="text-critical text-sm">読み込みに失敗しました。</div>}
      {data && data.items.length === 0 && (
        <div className="text-fg-subtle text-sm p-10 text-center bg-surface-1 border border-border-subtle rounded-lg">
          まだ深掘りはありません。
        </div>
      )}
      {data?.items.map((w) => <WeekCard key={`${w.period_label}-${w.generated_at}`} week={w} />)}
      {data && data.items.length >= weeks && (
        <button
          onClick={() => setWeeks((n) => n + 8)}
          className="w-full py-2 rounded border border-border-default text-fg-muted hover:text-fg text-sm"
        >
          さらに前の週を表示
        </button>
      )}
    </div>
  );
}
