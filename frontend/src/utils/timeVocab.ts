// 時刻の語彙 (SSoT は src/storage/event_time.py の TIME_VOCAB。一致は tests/unit/test_time_vocab.py が固定)。
//   取込時刻   = 取り込んだ時刻 (新着の窓にだけ使う)
//   報道時刻   = 公開時刻 (取込を超えない)。いつ報じられたか
//   発生日     = 事象が起きた日 (報道日の写しは含めない)
//   報道日のみ = 発生日が報道日の写しだけのもの
export const TIME_VOCAB = {
  ingested: "取込時刻",
  reported: "報道時刻",
  occurred: "発生日",
  reported_only: "報道日のみ",
  month_only: "月のみ",
  unknown: "不明",
} as const;

/** 発生時刻モードの見出し (トグル用)。 */
export const OCCURRED_MODE_LABEL = "発生時刻";

export interface TimeCoverageBase {
  dated: number;
  total: number;
  occurred?: number;
  month_only?: number;
  reported_only?: number;
  unknown?: number;
}

export interface TimeCoverage extends TimeCoverageBase {
  event_in_window: number;
}

/** 被覆の 1 行: 「発生日 N / 全 M・月のみ K・不明 U」。 */
export function formatTimeCoverage(c: TimeCoverageBase): string {
  const n = c.occurred ?? c.dated;
  const k = c.month_only ?? 0;
  const u = c.unknown ?? c.total - c.dated;
  return `${TIME_VOCAB.occurred} ${n} / 全 ${c.total}・${TIME_VOCAB.month_only} ${k}・${TIME_VOCAB.unknown} ${u}`;
}
