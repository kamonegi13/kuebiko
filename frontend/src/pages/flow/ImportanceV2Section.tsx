// 重要度の再設計 (2026-10-03、記録のみ) — いまの重要度と、深刻さ × 関連性の対応表。
// 下流はまだいまの重要度で動く。ここで差を読み、決まりごとを直してから下流を移す
// (docs/importance_relevance_redesign.md の M1・M2)。

import { useQuery } from "@tanstack/react-query";
import { importanceV2Api, type ImportanceV2Cell } from "../../api/flow";
import { vocabLabel } from "../../hooks/useVocab";

const PERIOD_DAYS = 30;
const ROWS = ["high", "medium", "low"] as const;

interface Column {
  key: string;
  label: string;
  match: (c: ImportanceV2Cell) => boolean;
}

const COLUMNS: Column[] = [
  { key: "S3", label: "S3 重大", match: (c) => c.severity === "S3" },
  { key: "S2", label: "S2 注意", match: (c) => c.severity === "S2" },
  { key: "S1", label: "S1 参考", match: (c) => c.severity === "S1" },
  { key: "heavy", label: "戦略 重", match: (c) => c.strategic_weight === "heavy" },
  { key: "moderate", label: "戦略 中", match: (c) => c.strategic_weight === "moderate" },
  { key: "light", label: "戦略 軽", match: (c) => c.strategic_weight === "light" },
  {
    key: "derivative",
    label: "派生記事",
    match: (c) => c.severity === null && c.strategic_weight === null,
  },
];

function sum(cells: ImportanceV2Cell[]): { total: number; relevant: number } {
  return cells.reduce(
    (acc, c) => ({ total: acc.total + c.count, relevant: acc.relevant + (c.relevant ? c.count : 0) }),
    { total: 0, relevant: 0 },
  );
}

export function ImportanceV2Section() {
  const { data, error } = useQuery({
    queryKey: ["flow-importance-v2", PERIOD_DAYS],
    queryFn: () => importanceV2Api.get(PERIOD_DAYS),
  });
  if (error) return <p className="text-sm text-critical">新しい重要度の記録を読めませんでした。</p>;
  if (!data) return null;
  const rows = ROWS.filter((imp) => data.cells.some((c) => c.importance === imp));

  return (
    <section className="mt-10 space-y-3">
      <header className="space-y-1">
        <h2 className="m-0 text-base font-semibold">新しい重要度の記録 (比較のみ)</h2>
        <p className="m-0 text-[13px] text-fg-subtle">
          事象の深刻さと、日本・注視国・SIR との関連性を分けて記録しています。配信やブリーフはまだいまの重要度で動きます。
          直近 {data.period_days} 日・決まりごとの版 {data.rule_version}。各欄は「件数 (うち関連性あり)」です。
        </p>
      </header>
      {rows.length === 0 ? (
        <p className="text-sm text-fg-subtle">まだ記録がありません (深刻度の軸が付いた記事から毎時記録します)。</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full border-collapse text-sm">
            <thead>
              <tr className="text-left text-fg-subtle">
                <th className="py-1 pr-3 font-normal">いまの重要度</th>
                {COLUMNS.map((col) => (
                  <th key={col.key} className="px-2 py-1 text-right font-normal">
                    {col.label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((imp) => (
                <tr key={imp} className="border-t border-border">
                  <td className="py-1 pr-3">{vocabLabel("importance", imp)}</td>
                  {COLUMNS.map((col) => {
                    const s = sum(data.cells.filter((c) => c.importance === imp && col.match(c)));
                    return (
                      <td key={col.key} className="px-2 py-1 text-right tabular-nums">
                        {s.total === 0 ? "—" : `${s.total} (${s.relevant})`}
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
