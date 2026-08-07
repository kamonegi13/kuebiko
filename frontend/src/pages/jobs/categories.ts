// ジョブの機能グループ定義 + カテゴリ別の見た目/要約ロジック。
// timeline swimlane と下部の詳細カードで同じグルーピングを共有する。
// 未知の id は「その他」に落ち、backend が新 job を足しても必ず表示される。

import {
  Radio, Send, BrainCircuit, GraduationCap, Wrench, ListChecks, LayoutGrid,
  type LucideIcon,
} from "lucide-react";
import type { JobView } from "../../api/jobs";

export interface JobCategoryDef {
  key: string;
  title: string;
  ids: string[];
  icon: LucideIcon;
  // カテゴリ accent (Tailwind クラス片)。border/text/bg に流用する。
  accentText: string;
  accentBar: string;
}

export const OTHER_CATEGORY_KEY = "other";
export const OTHER_CATEGORY_TITLE = "その他";

// 収集/配信/分析・総括/学習・辞書/保守 + その他。
export const JOB_CATEGORIES: JobCategoryDef[] = [
  {
    key: "collect",
    title: "収集",
    ids: ["direct-rss-fetch", "web-scraper-watchers", "grok-briefing", "ransomware-live-ingest"],
    icon: Radio,
    accentText: "text-accent",
    accentBar: "bg-accent",
  },
  {
    key: "deliver",
    title: "配信",
    ids: ["morning-brief", "evening-brief"],
    icon: Send,
    accentText: "text-success",
    accentBar: "bg-success",
  },
  {
    key: "analysis",
    title: "分析・総括",
    ids: ["auto-trigger-synthesis", "weekly-recap", "weekly-status-synthesis", "monthly-status-synthesis", "pir-spotlight", "ledger-deep-review"],
    icon: BrainCircuit,
    accentText: "text-accent-hover",
    accentBar: "bg-accent-hover",
  },
  {
    key: "learn",
    title: "学習・辞書",
    ids: ["weekly-taxonomy-review", "mitre-actor-sync", "actor-history-distill"],
    icon: GraduationCap,
    accentText: "text-warning",
    accentBar: "bg-warning",
  },
  {
    key: "maintain",
    title: "保守",
    ids: ["pir-entity-rebuild", "daily-maintenance", "daily-heartbeat"],
    icon: Wrench,
    accentText: "text-fg-muted",
    accentBar: "bg-fg-muted",
  },
  {
    key: "upkeep",
    title: "定常処理",
    // 定常キュー処理 (時刻に運用上の意味がない補助ジョブ)。タイムラインでの集約は
    // backend JobDef.upkeep フラグが SSoT — この ids はカード配色/アイコン用の鏡で、
    // 新しい upkeep ジョブを足す時はここにも 1 行追加する (漏れても「その他」に落ちるだけ)。
    ids: ["pir-judge-hourly", "body-translate-backlog", "job-recovery-watchdog"],
    icon: ListChecks,
    accentText: "text-fg-subtle",
    accentBar: "bg-fg-subtle",
  },
];

export const OTHER_CATEGORY: JobCategoryDef = {
  key: OTHER_CATEGORY_KEY,
  title: OTHER_CATEGORY_TITLE,
  ids: [],
  icon: LayoutGrid,
  accentText: "text-fg-subtle",
  accentBar: "bg-fg-subtle",
};

export function categoryForId(id: string): JobCategoryDef {
  for (const c of JOB_CATEGORIES) {
    if (c.ids.includes(id)) return c;
  }
  return OTHER_CATEGORY;
}

// マーカー配色 (共有 24h 軸に置く pill/dot)。カテゴリ色に揃える。
export interface MarkerColor {
  dot: string; // bg-*
  border: string; // border-*
  text: string; // text-*
}

// accentBar (bg-*) を軸に、同系の border/text を導く。凡例と一致させる。
const MARKER_COLOR_BY_CATEGORY: Record<string, MarkerColor> = {
  collect: { dot: "bg-accent", border: "border-accent/50", text: "text-accent" },
  deliver: { dot: "bg-success", border: "border-success/50", text: "text-success" },
  analysis: { dot: "bg-accent-hover", border: "border-accent/50", text: "text-accent-hover" },
  learn: { dot: "bg-warning", border: "border-warning/50", text: "text-warning" },
  maintain: { dot: "bg-fg-muted", border: "border-border-emphasized", text: "text-fg-muted" },
  upkeep: { dot: "bg-fg-subtle", border: "border-border-subtle", text: "text-fg-subtle" },
  [OTHER_CATEGORY_KEY]: { dot: "bg-fg-subtle", border: "border-border-subtle", text: "text-fg-subtle" },
};

export function markerColorForId(id: string): MarkerColor {
  const def = categoryForId(id);
  return MARKER_COLOR_BY_CATEGORY[def.key] ?? MARKER_COLOR_BY_CATEGORY[OTHER_CATEGORY_KEY];
}

// timeline ピルの短い日本語略称 (一覧性のため)。無い id は title 先頭2字 fallback。
export const SHORT_LABELS: Record<string, string> = {
  "morning-brief": "朝ブ",
  "evening-brief": "夕ブ",
  "weekly-recap": "深掘",
  "weekly-status-synthesis": "週総",
  "monthly-status-synthesis": "月総",
  "pir-spotlight": "スポット",
  "weekly-taxonomy-review": "分類",
  "mitre-actor-sync": "MITRE",
  "pir-entity-rebuild": "PIR整",
  "grok-briefing": "Grok",
  "auto-trigger-synthesis": "自動総括",
  "direct-rss-fetch": "RSS",
  "web-scraper-watchers": "スクレイパ",
  "ransomware-live-ingest": "ランサム",
  "daily-maintenance": "保守",
  "daily-heartbeat": "死活",
  "pir-judge-hourly": "PIR判定",
  "job-recovery-watchdog": "自動復旧",
  "ledger-deep-review": "夜間精査",
  "body-translate-backlog": "翻訳",
  "body-refetch-backlog": "再取得",
  "ua-health-check": "UA修復",
  "actor-history-distill": "行動史",
};

// job id → 短い略称。未登録は title の先頭 2 文字 (無ければ id) を使う。
export function shortLabelForId(id: string, title: string): string {
  const known = SHORT_LABELS[id];
  if (known) return known;
  const t = title.trim();
  return t.length > 0 ? t.slice(0, 2) : id;
}

// ---- 実行状態の分類 (last_run.status / JobRunRecord.status 共通) ----------
// タイムラインのフラグ、ヘッダ集計、履歴リストの状態ドットで共有する SSoT。
export type RunHealth = "ok" | "failed" | "running" | "none";

export function runHealth(status?: string | null): RunHealth {
  if (!status) return "none";
  const s = status.trim().toLowerCase();
  if (s === "succeeded" || s === "success" || s === "ok" || s === "completed") return "ok";
  if (s === "failed" || s === "error") return "failed";
  if (s === "running") return "running";
  return "none";
}

// RunHealth → token 配色 (ok=success, failed=critical, running=accent)。
export interface RunHealthColor {
  dot: string; // bg-*
  text: string; // text-*
}

const RUN_HEALTH_COLOR: Record<RunHealth, RunHealthColor> = {
  ok: { dot: "bg-success", text: "text-success" },
  failed: { dot: "bg-critical", text: "text-critical" },
  running: { dot: "bg-accent", text: "text-accent" },
  none: { dot: "bg-fg-faint", text: "text-fg-subtle" },
};

export function runHealthColor(health: RunHealth): RunHealthColor {
  return RUN_HEALTH_COLOR[health];
}

export interface CategoryGroup {
  def: JobCategoryDef;
  jobs: JobView[];
}

// 宣言順を保ちつつ空カテゴリを除外して束ねる。
export function groupByCategory(jobs: JobView[]): CategoryGroup[] {
  const buckets = new Map<string, JobView[]>();
  for (const c of JOB_CATEGORIES) buckets.set(c.key, []);
  buckets.set(OTHER_CATEGORY.key, []);
  for (const job of jobs) {
    const c = categoryForId(job.id);
    buckets.get(c.key)?.push(job);
  }
  const defByKey = new Map<string, JobCategoryDef>(
    [...JOB_CATEGORIES, OTHER_CATEGORY].map((c) => [c.key, c]),
  );
  const out: CategoryGroup[] = [];
  for (const [key, arr] of buckets.entries()) {
    if (arr.length === 0) continue;
    const def = defByKey.get(key);
    if (def) out.push({ def, jobs: arr });
  }
  return out;
}

// ---- last-run status 判定 (成功/失敗) ----------------------------------
export function lastRunOk(job: JobView): boolean | null {
  if (!job.last_run) return null;
  const s = job.last_run.status.toLowerCase();
  if (s === "success" || s === "ok" || s === "completed") return true;
  return false;
}

function relativeFromNow(iso: string | null): string {
  if (!iso) return "—";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "—";
  const diffMs = t - Date.now();
  const abs = Math.abs(diffMs);
  const min = Math.round(abs / 60000);
  const suffix = diffMs >= 0 ? "後" : "前";
  if (min < 1) return "まもなく";
  if (min < 60) return `${min}分${suffix}`;
  const hr = Math.round(min / 60);
  if (hr < 24) return `${hr}時間${suffix}`;
  const day = Math.round(hr / 24);
  return `${day}日${suffix}`;
}

// 最も新しい last_run の相対時刻。
function mostRecentLastRun(jobs: JobView[]): string | null {
  let best: string | null = null;
  let bestT = -Infinity;
  for (const j of jobs) {
    if (!j.last_run) continue;
    const t = new Date(j.last_run.last_run_at).getTime();
    if (!Number.isNaN(t) && t > bestT) {
      bestT = t;
      best = j.last_run.last_run_at;
    }
  }
  return best;
}

// 最も早い next_run_at。
function soonestNextRun(jobs: JobView[]): JobView | null {
  let best: JobView | null = null;
  let bestT = Infinity;
  for (const j of jobs) {
    if (!j.next_run_at) continue;
    const t = new Date(j.next_run_at).getTime();
    if (!Number.isNaN(t) && t < bestT) {
      bestT = t;
      best = j;
    }
  }
  return best;
}

function disabledCount(jobs: JobView[]): number {
  return jobs.filter((j) => !j.enabled).length;
}

// カテゴリ別の 1 行要約 (その性質に応じた指標)。
export interface CategorySummary {
  text: string;
  // 注意を促す時 (critical 停止など) は true。
  warn: boolean;
}

export function categorySummary(def: JobCategoryDef, jobs: JobView[]): CategorySummary {
  const off = disabledCount(jobs);
  const offSuffix = off > 0 ? ` · ${off} 停止中` : "";

  switch (def.key) {
    case "collect": {
      const recent = mostRecentLastRun(jobs);
      return {
        text: `毎時稼働 · 最終取得 ${relativeFromNow(recent)}${offSuffix}`,
        warn: off > 0,
      };
    }
    case "deliver": {
      const soonest = soonestNextRun(jobs);
      const next = soonest
        ? `次の配信 ${soonest.title} (${relativeFromNow(soonest.next_run_at)})`
        : "次の配信 未定";
      return { text: `${next}${offSuffix}`, warn: off > 0 };
    }
    case "analysis": {
      const auto = jobs.find((j) => j.id === "auto-trigger-synthesis");
      const autoState = auto ? (auto.enabled ? "ON" : "OFF") : "—";
      const scheduled = jobs.filter((j) => j.id !== "auto-trigger-synthesis");
      const soonest = soonestNextRun(scheduled);
      const next = soonest
        ? `次の定時 ${soonest.title} (${relativeFromNow(soonest.next_run_at)})`
        : "次の定時 未定";
      return {
        text: `自動更新 ${autoState} · ${next}${offSuffix}`,
        warn: off > 0 || (!!auto && !auto.enabled),
      };
    }
    case "learn": {
      const soonest = soonestNextRun(jobs);
      const next = soonest
        ? `次 ${soonest.title} (${relativeFromNow(soonest.next_run_at)})`
        : "次 未定";
      return { text: `週次 · ${next}${offSuffix}`, warn: off > 0 };
    }
    case "maintain": {
      const critical = jobs.filter((j) => j.protection === "critical");
      const criticalOn = critical.filter((j) => j.enabled).length;
      const anyCriticalOff = criticalOn < critical.length;
      return {
        text: `critical ${criticalOn}/${critical.length} 稼働${offSuffix}`,
        warn: anyCriticalOff,
      };
    }
    default: {
      const on = jobs.length - off;
      return { text: `${on}/${jobs.length} 稼働${offSuffix}`, warn: off > 0 };
    }
  }
}
