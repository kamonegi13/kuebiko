// summarizer 判定基準エディタ UI 再設計 (WP-F4): 目次 (グループ + フィールド)・検索ボックス・
// 一括開閉・統計期間セレクタ・ジャンプ発火。ここに表示するグループ/フィールドの一覧は常に
// 全件 (検索フィルタの影響を受けない) — 本文側 (RubricGroupSection) だけが検索で絞られる。

import { Search } from "lucide-react";
import type { RubricGroupId, StatsDays } from "../../../api/promptRubric";

export interface TocGroup {
  id: RubricGroupId;
  label: string;
  fields: { fieldId: string; title: string; changed: boolean; errorCount: number; warnCount: number }[];
  errorCount: number;
  warnCount: number;
}

interface RubricTocProps {
  groups: TocGroup[];
  exampleCount: number;
  exampleErrorCount: number;
  exampleWarnCount: number;
  query: string;
  onQueryChange: (q: string) => void;
  statsDays: StatsDays;
  onStatsDaysChange: (d: StatsDays) => void;
  onJump: (anchorId: string) => void;
  onExpandAll: () => void;
  onCollapseAll: () => void;
}

const STATS_DAY_OPTS: StatsDays[] = [7, 30, 90];

function IssueMark({ errorCount, warnCount }: { errorCount: number; warnCount: number }) {
  if (errorCount > 0) return <span className="text-critical">⚠</span>;
  if (warnCount > 0) return <span className="text-warning">⚠</span>;
  return null;
}

export function RubricToc({
  groups,
  exampleCount,
  exampleErrorCount,
  exampleWarnCount,
  query,
  onQueryChange,
  statsDays,
  onStatsDaysChange,
  onJump,
  onExpandAll,
  onCollapseAll,
}: RubricTocProps) {
  return (
    // ⚠ ここで sticky にしない — 呼び出し側の aside ごと sticky にする。nav だけを
    // sticky にすると、スクロール時に nav が兄弟要素 (分母の注記) の上へスライドして
    // 文字が重なる。
    <nav className="space-y-3 text-sm">
      <div className="relative">
        <Search className="absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-fg-subtle" />
        <input
          value={query}
          onChange={(e) => onQueryChange(e.target.value)}
          placeholder="フィールドを絞る…"
          className="w-full rounded border border-border-subtle bg-surface-2 py-1.5 pl-7 pr-2 text-xs text-fg placeholder:text-fg-subtle"
        />
      </div>

      <div className="flex items-center gap-2 text-[11px]">
        <button type="button" onClick={onExpandAll} className="text-accent hover:underline">
          すべて開く
        </button>
        <span className="text-fg-subtle">/</span>
        <button type="button" onClick={onCollapseAll} className="text-accent hover:underline">
          すべて閉じる
        </button>
      </div>

      <div className="space-y-1">
        <div className="text-[10.5px] uppercase tracking-wide text-fg-subtle">統計の対象期間</div>
        <div className="inline-flex rounded-md border border-border-subtle bg-surface-2 p-0.5">
          {STATS_DAY_OPTS.map((d) => (
            <button
              key={d}
              type="button"
              onClick={() => onStatsDaysChange(d)}
              className={`rounded-sm px-2.5 py-1 text-[11px] font-medium transition-colors ${
                statsDays === d ? "bg-surface-overlay text-fg" : "text-fg-muted hover:text-fg"
              }`}
            >
              {d}日
            </button>
          ))}
        </div>
      </div>

      <ul className="space-y-2.5">
        {groups.map((g) => (
          <li key={g.id}>
            <button
              type="button"
              onClick={() => onJump(`group:${g.id}`)}
              className="flex w-full items-center gap-1.5 text-left text-xs font-semibold text-fg hover:text-accent"
            >
              <span className="truncate">{g.label}</span>
              <span className="tnum font-normal text-fg-subtle">{g.fields.length}</span>
              <IssueMark errorCount={g.errorCount} warnCount={g.warnCount} />
            </button>
            {g.fields.length > 0 && (
              <ul className="mt-1 space-y-0.5 border-l border-border-subtle pl-2">
                {g.fields.map((f) => (
                  <li key={f.fieldId}>
                    <button
                      type="button"
                      onClick={() => onJump(`field:${f.fieldId}`)}
                      className="flex w-full items-center gap-1.5 text-left text-[11.5px] text-fg-muted hover:text-accent"
                    >
                      {f.changed && <span className="shrink-0 text-accent">●</span>}
                      <span className="truncate">{f.title}</span>
                      <span className="ml-auto shrink-0">
                        <IssueMark errorCount={f.errorCount} warnCount={f.warnCount} />
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </li>
        ))}
        <li>
          <button
            type="button"
            onClick={() => onJump("examples")}
            className="flex w-full items-center gap-1.5 text-left text-xs font-semibold text-fg hover:text-accent"
          >
            <span>出力例</span>
            <span className="tnum font-normal text-fg-subtle">{exampleCount}</span>
            <IssueMark errorCount={exampleErrorCount} warnCount={exampleWarnCount} />
          </button>
        </li>
      </ul>
    </nav>
  );
}
