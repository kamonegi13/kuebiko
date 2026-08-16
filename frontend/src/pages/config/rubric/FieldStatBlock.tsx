// summarizer 判定基準エディタ UI 再設計 (WP-F3): 1 フィールド分の統計描画。
// I-5 (ラベル SSoT): 値ラベルの解決はこのファイルに集約する。他のコンポーネント
// (RubricSectionCard 等) はここを経由せずに label を生成しない。
// ラベル解決順序: 明示 label があればそれ → なければ vocab で解決 → vocab も null なら生値。
//
// vocab は bucket/sub_metric ごとに異なりうる (動的) ため、hooks-in-loop を避けて
// render 外の非フックアクセサ `vocabLabel` (queryClient キャッシュを同期読みする、
// useVocab.ts が公式に提供する非フック版) を使う。boot gate 済みなのでキャッシュは
// 常に暖まっている。
import { Info } from "lucide-react";
import { vocabLabel } from "../../../hooks/useVocab";
import type { FieldStat, StatBucket, StatSubMetric, StatsDays } from "../../../api/promptRubric";
import { newsUrl } from "./format";

interface FieldStatBlockProps {
  stat: FieldStat | undefined;
  loading: boolean;
  days: StatsDays;
  /** true: 折りたたみヘッダに出す 1 行要約のみを描画する。 */
  compact?: boolean;
}

function resolveLabel(explicit: string | null, vocab: string | null, rawValue: string): string {
  if (explicit !== null) return explicit;
  if (vocab) return vocabLabel(vocab, rawValue) || rawValue || "(空)";
  return rawValue || "(空)";
}

function pct(rate: number): number {
  return Math.round(rate * 1000) / 10;
}

function compactSummary(stat: FieldStat): string {
  if (stat.distribution.length > 0) {
    const top = stat.distribution[0];
    return `${resolveLabel(top.label, top.vocab, top.value)} ${pct(top.share)}%`;
  }
  if (stat.coverage) {
    return `充足率 ${pct(stat.coverage.rate)}%`;
  }
  if (stat.sub_metrics.length > 0) {
    const m = stat.sub_metrics[0];
    return `${resolveLabel(m.label, m.vocab, m.key)} ${pct(m.rate)}%`;
  }
  if (stat.average) {
    return `${stat.average.label} ${stat.average.value.toLocaleString()}${stat.average.unit}`;
  }
  return "";
}

function DistributionRow({ bucket }: { bucket: StatBucket }) {
  const label = resolveLabel(bucket.label, bucket.vocab, bucket.value);
  const share = pct(bucket.share);
  return (
    <div className="flex items-center gap-1.5 text-[11px]">
      <span className="w-24 shrink-0 truncate text-fg-muted" title={label}>
        {label}
      </span>
      <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-surface-3">
        <div className="h-full bg-accent" style={{ width: `${Math.min(100, Math.max(0, share))}%` }} />
      </div>
      <span className="w-10 shrink-0 text-right text-fg-subtle tnum">{share}%</span>
      <span className="w-14 shrink-0 text-right text-fg-subtle tnum">{bucket.count.toLocaleString()}</span>
      {bucket.drilldown_query && (
        <a href={newsUrl(bucket.drilldown_query)} className="shrink-0 text-accent hover:underline">
          一覧で見る →
        </a>
      )}
    </div>
  );
}

function SubMetricRow({ metric }: { metric: StatSubMetric }) {
  const label = resolveLabel(metric.label, metric.vocab, metric.key);
  return (
    <div className="flex items-center justify-between gap-2 text-[11px] text-fg-muted">
      <span className="truncate" title={metric.note || label}>
        {label}
      </span>
      <span className="shrink-0 tnum text-fg-subtle">
        {pct(metric.rate)}% ({metric.filled.toLocaleString()}/{metric.total.toLocaleString()})
        {metric.note && ` — ${metric.note}`}
      </span>
    </div>
  );
}

export function FieldStatBlock({ stat, loading, days, compact = false }: FieldStatBlockProps) {
  if (compact) {
    if (loading) return <span className="text-fg-subtle italic">統計取得中…</span>;
    if (!stat || stat.availability === "none") return <span className="text-fg-subtle">統計なし</span>;
    const summary = compactSummary(stat);
    // ⚠ partial は代理指標を含む。畳んだ状態で実測値と同じ見た目にすると数字を誤読する。
    const prefix = stat.availability === "partial" ? "代理指標: " : "";
    return (
      <span className="truncate text-fg-subtle">
        {prefix}
        {summary || "—"}
      </span>
    );
  }

  if (loading) {
    return <p className="m-0 text-[11px] italic text-fg-subtle">直近{days}日の統計を取得中…</p>;
  }
  if (!stat) {
    return <p className="m-0 text-[11px] text-fg-subtle">統計データを取得できませんでした。</p>;
  }
  if (stat.availability === "none") {
    return (
      <p className="m-0 inline-flex items-start gap-1 text-[11px] text-fg-subtle">
        <Info className="mt-0.5 h-3 w-3 shrink-0" />
        <span>統計なし — {stat.source_note}</span>
      </p>
    );
  }

  return (
    <div className="space-y-1.5">
      <p className="m-0 text-[11px] text-fg-subtle">
        直近{days}日の実際の出力{stat.availability === "partial" && "（一部代理指標）"} — {stat.source_note}
      </p>
      {stat.notes.map((n, i) => (
        <p key={i} className="m-0 text-[10.5px] italic text-fg-subtle">
          ※ {n}
        </p>
      ))}
      {stat.coverage && (
        <div className="text-[11px] tnum text-fg-muted">
          充足率 {pct(stat.coverage.rate)}%（{stat.coverage.filled.toLocaleString()} / {stat.coverage.total.toLocaleString()} 件、
          {stat.coverage.scope_label}）
        </div>
      )}
      {stat.distribution.length > 0 && (
        <div className="space-y-1">
          {stat.distribution.map((b, i) => (
            <DistributionRow key={`${b.value}-${i}`} bucket={b} />
          ))}
        </div>
      )}
      {stat.sub_metrics.length > 0 && (
        <div className="space-y-1">
          {stat.sub_metrics.map((m) => (
            <SubMetricRow key={m.key} metric={m} />
          ))}
        </div>
      )}
      {stat.average && (
        <div className="text-[11px] tnum text-fg-muted">
          {stat.average.label}: {stat.average.value.toLocaleString()} {stat.average.unit}
        </div>
      )}
    </div>
  );
}
