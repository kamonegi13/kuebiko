// ジョブ timeline 用の JST 時刻ユーティリティ。
// ブラウザの timezone に依存せず、常に Asia/Tokyo (JST) の「時刻(分)」に射影する。
// cron の hour/minute は既に JST なのでそのまま。next_run_at は ISO UTC → JST に変換する。

export const MINUTES_PER_DAY = 24 * 60;

const JST_TZ = "Asia/Tokyo" as const;

// en-US + 24h 固定で JST の各 field を安定して取り出す (locale 揺れを避ける)。
const JST_PARTS = new Intl.DateTimeFormat("en-US", {
  timeZone: JST_TZ,
  hour12: false,
  weekday: "short",
  hour: "2-digit",
  minute: "2-digit",
});

// APScheduler の day_of_week 表記 (mon/tue/...) に合わせた曜日キー。
export const DOW_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"] as const;
export type DowKey = (typeof DOW_KEYS)[number];

export const DOW_LABEL: Record<DowKey, string> = {
  mon: "月",
  tue: "火",
  wed: "水",
  thu: "木",
  fri: "金",
  sat: "土",
  sun: "日",
};

const WEEKDAY_TO_KEY: Record<string, DowKey> = {
  Mon: "mon",
  Tue: "tue",
  Wed: "wed",
  Thu: "thu",
  Fri: "fri",
  Sat: "sat",
  Sun: "sun",
};

interface JstFields {
  weekday: DowKey;
  minutesOfDay: number;
}

// 任意の時刻を JST の {曜日, その日の 0..1439 分} に射影。
export function jstFields(input: Date): JstFields {
  const parts = JST_PARTS.formatToParts(input);
  let weekdayRaw = "Mon";
  let hour = 0;
  let minute = 0;
  for (const p of parts) {
    if (p.type === "weekday") weekdayRaw = p.value;
    else if (p.type === "hour") hour = parseInt(p.value, 10) % 24;
    else if (p.type === "minute") minute = parseInt(p.value, 10);
  }
  return {
    weekday: WEEKDAY_TO_KEY[weekdayRaw] ?? "mon",
    minutesOfDay: hour * 60 + minute,
  };
}

// 現在の JST 曜日キー。
export function currentJstDow(now: Date = new Date()): DowKey {
  return jstFields(now).weekday;
}

// 現在の JST での「その日の分」(0..1439)。now-line の位置に使う。
export function currentJstMinutes(now: Date = new Date()): number {
  return jstFields(now).minutesOfDay;
}

// next_run_at (ISO UTC) → JST のその日の分。パース不能なら null。
export function isoToJstMinutes(iso: string | null): number | null {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  return jstFields(d).minutesOfDay;
}

// 分 (0..1439) → "HH:MM"。
export function minutesToHhmm(minutes: number): string {
  const clamped = Math.max(0, Math.min(MINUTES_PER_DAY - 1, Math.round(minutes)));
  const h = Math.floor(clamped / 60);
  const m = clamped % 60;
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}`;
}

// hour/minute → 分 (未指定は 0)。
export function hhmmToMinutes(hour: number | null, minute: number | null): number {
  const h = hour ?? 0;
  const m = minute ?? 0;
  return Math.max(0, Math.min(MINUTES_PER_DAY - 1, h * 60 + m));
}

// 5 分刻みに snap し 00:00-23:59 にクランプ。drag の着地点整形に使う。
export function snapMinutes(minutes: number, step = 5): number {
  const snapped = Math.round(minutes / step) * step;
  return Math.max(0, Math.min(MINUTES_PER_DAY - step, snapped));
}

// 分 → track 上の 0..1 比率。
export function minutesToRatio(minutes: number): number {
  return Math.max(0, Math.min(1, minutes / MINUTES_PER_DAY));
}

// track 上の 0..1 比率 → 分。
export function ratioToMinutes(ratio: number): number {
  return Math.max(0, Math.min(MINUTES_PER_DAY, Math.round(ratio * MINUTES_PER_DAY)));
}

// APScheduler day_of_week フィールド (例 "mon" / "mon,wed" / "1" 等) から
// 該当曜日 set を導出。数値表記や範囲表記は非対応 (このアプリの実データは短名の単一/複数)。
export function parseDowField(field: string | null): Set<DowKey> {
  const out = new Set<DowKey>();
  if (!field) return out;
  for (const raw of field.split(",")) {
    const key = raw.trim().toLowerCase();
    if ((DOW_KEYS as readonly string[]).includes(key)) out.add(key as DowKey);
  }
  return out;
}

// 該当ジョブが「選択曜日に発火するか」。weekly なら day_of_week に選択曜日が含まれる時のみ。
export function firesOnDow(dayOfWeek: string | null, selected: DowKey): boolean {
  const set = parseDowField(dayOfWeek);
  if (set.size === 0) return true; // 毎日
  return set.has(selected);
}
