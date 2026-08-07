// 日時表示の utility。
// ブラウザ timezone に依存せず常に Asia/Tokyo (JST) で表示する。
// 全ページが同じ表示にするため必ずこれを経由する。

const JST_LOCALE = "ja-JP" as const;
const JST_TZ = { timeZone: "Asia/Tokyo" } as const;

export function formatJst(input: string | number | Date | null | undefined): string {
  if (!input) return "—";
  try {
    return new Date(input).toLocaleString(JST_LOCALE, { ...JST_TZ });
  } catch {
    return "—";
  }
}

export function formatJstShort(input: string | number | Date | null | undefined): string {
  if (!input) return "—";
  try {
    return new Date(input).toLocaleString(JST_LOCALE, {
      ...JST_TZ,
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return "—";
  }
}

export function formatJstDate(input: string | number | Date | null | undefined): string {
  if (!input) return "—";
  try {
    return new Date(input).toLocaleDateString(JST_LOCALE, JST_TZ);
  } catch {
    return "—";
  }
}

export function formatJstCompact(input: string | number | Date | null | undefined): string {
  if (!input) return "—";
  try {
    return new Date(input).toLocaleString(JST_LOCALE, {
      ...JST_TZ,
      month: "numeric",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return "—";
  }
}

// 現在からの相対時刻 (「N分前 / N時間後」等)。now-independent な TZ 換算は不要 (差分のみ)。
export function relativeFromNow(iso: string | null | undefined): string {
  if (!iso) return "—";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "—";
  const diffMs = t - Date.now();
  const min = Math.round(Math.abs(diffMs) / 60000);
  const suffix = diffMs >= 0 ? "後" : "前";
  if (min < 1) return "まもなく";
  if (min < 60) return `${min}分${suffix}`;
  const hr = Math.round(min / 60);
  if (hr < 24) return `${hr}時間${suffix}`;
  return `${Math.round(hr / 24)}日${suffix}`;
}
