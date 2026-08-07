// ダッシュボード widget 群の共有プリミティブ (型 + カード枠 + 小物)。
// 各 widget module (widgets/*.tsx) はここに依存し、registry.tsx が widget を束ねる。
// shared は registry / widgets に依存しない (循環回避)。

import type { ReactNode } from "react";
import { AlertTriangle } from "lucide-react";
import { useVocabMap } from "../../hooks/useVocab";

export const INTEL = "/app/intel"; // Intel Graph (深掘り) へのドリルダウン先

// registry の初期幅 (4 カラム基準: 1=¼ .. 4=全)。12 カラム座標グリッドでは ×3 して使う。
export type WidgetSpan = 1 | 2 | 3 | 4;

// ── widget registry 型 ──
// mobile: ページ (useIsMobile) から供給される「狭く縦長」モードフラグ。
// 各 widget は self でフックを呼ばず、この prop でモバイル専用描画に分岐する
// (レイアウト切替と同一の真実源を共有)。
export type WidgetProps = { config?: Record<string, unknown>; mobile?: boolean };
export interface ConfigChoice {
  value: string;
  label: string;
}
export interface ConfigOption {
  key: string;
  label: string;
  // 静的選択肢。dynamic な場合は loadChoices を使う (排他)。
  choices?: ConfigChoice[];
  // 動的選択肢 (アクター一覧など)。編集 UI が fetch して select を埋める。
  loadChoices?: () => Promise<ConfigChoice[]>;
  // 先頭に付ける「未選択」相当の選択肢 (loadChoices と併用)。
  placeholder?: ConfigChoice;
}
// ツールボックスのサムネイル種別 (各 widget が「どんな見た目か」を模式図で示す)。
// フル screenshot でなく軽量な schematic (データ取得なし・陳腐化しない)。
export type ThumbKind =
  | "kpi" // 大数字タイル行
  | "bars" // 横棒ランキング
  | "sparkline" // ラベル + ミニ推移の行
  | "list" // 見出し/記事リスト
  | "status" // 状態バー + バッジ
  | "text" // 段落 (synthesis 等)
  | "trend" // 縦棒の時系列
  | "composition" // 1 本の積上げ構成バー
  | "grid"; // 統計の格子 (holdings)

export interface WidgetDef {
  title: string;
  Component: (props: WidgetProps) => ReactNode;
  defaultSpan: WidgetSpan;
  // 初期配置時の tile 高さの目安 (px)。座標グリッドが行数へ変換する。
  // 未指定 = 汎用既定 (300px 相当)。地図/フィード等の背が高い widget は明示する。
  defaultHeight?: number;
  // true = 塗り潰し型 (h-full で tile を満たす: 地図等)。中身に固有の自然高が無いため
  // 「高さを中身に合わせる (fit)」の対象外にする。
  fill?: boolean;
  // 一言説明 (ツールボックスの hover / カタログで使用)。
  blurb?: string;
  // ツールボックスの模式サムネイル種別。
  thumb?: ThumbKind;
  // true = 設定違いで複数配置する意味がある (記事フィード/アクター別 等)。
  // 省略/false = 全体サマリ等で 1 つあれば十分 (重複配置を UI で抑止)。
  multi?: boolean;
  configOptions?: ConfigOption[]; // 編集モードで select として表示・永続化
  // preset の初期 config。同一 Component を別 preset として登録するのに使う
  // (例: 記事フィードを 脆弱性 / ニュースサマリー 等に固定)。w.config が優先。
  defaultConfig?: Record<string, unknown>;
}

// タイトル中の CVE-YYYY-NNNN... を抽出 (脆弱性 widget の強調用)。
const _CVE_RE = /CVE-\d{4}-\d{4,7}/gi;
export function extractCves(text: string): string[] {
  const m = text.match(_CVE_RE);
  return m ? Array.from(new Set(m.map((c) => c.toUpperCase()))) : [];
}

// config 値の小さなアクセサ (型安全な既定値付き取得)。
export function cfgStr(config: Record<string, unknown> | undefined, key: string, fallback: string): string {
  const v = config?.[key];
  return typeof v === "string" && v !== "" ? v : fallback;
}
export function cfgNum(config: Record<string, unknown> | undefined, key: string, fallback: number): number {
  const v = config?.[key];
  if (typeof v === "number") return v;
  if (typeof v === "string" && v.trim() !== "" && !Number.isNaN(Number(v))) return Number(v);
  return fallback;
}

// 件数 (per/limit) の共通選択肢。
export const COUNT_CHOICES: ConfigChoice[] = [
  { value: "2", label: "2件" },
  { value: "3", label: "3件" },
  { value: "5", label: "5件" },
  { value: "8", label: "8件" },
  { value: "12", label: "12件" },
];

// ── カード枠 ──
export function WidgetCard({ title, href, linkLabel, children }: { title: string; href?: string; linkLabel?: string; children: ReactNode }) {
  // h-full + flex-col で枠 (tile) 高さを満たし、本文 (flex-1) が残りを埋める。
  // スクロールは本文の内側 (overflow-y-auto): カードの背景/枠は tile に固定されたまま
  // 中身だけが流れる (tile 側でスクロールさせるとカード背景ごと動いて破綻する)。
  //
  // 横 padding はカードではなく **中の要素**に持たせる (2026-08-02)。カード側に付けると
  // スクロール要素がその内側に閉じ込められ、スクロールバーが右端から padding 分だけ
  // 内側に描画されて他ページ (ページ全体スクロール = 右端) と食い違って見える。
  // スクロール要素をカード幅いっぱいにし、内容だけを px で inset する。
  return (
    <div className="bg-surface-1 border border-border-subtle rounded-lg py-4 h-full flex flex-col min-h-0">
      <div className="flex items-baseline justify-between mb-3 shrink-0 px-4">
        <h3 className="m-0 text-sm font-semibold text-fg">{title}</h3>
        {href && <a href={href} className="text-accent text-xs hover:underline shrink-0 ml-2">{linkLabel || "詳細 →"}</a>}
      </div>
      <div className="flex-1 min-h-0 overflow-y-auto px-4">{children}</div>
    </div>
  );
}
export function WidgetShell({ children }: { children: ReactNode }) {
  // h-full: 座標グリッドの tile (固定高さ) を枠まで満たす。auto 文脈 (mobile) では中身の高さ。
  // 超過分は内部スクロール (背景/枠は tile に固定)。
  return <div className="bg-surface-1 border border-border-subtle rounded-lg p-3 h-full overflow-y-auto">{children}</div>;
}
export function Loading() {
  return <div className="text-fg-subtle text-sm italic">読み込み中…</div>;
}
export function Empty({ children }: { children: ReactNode }) {
  return <div className="text-fg-subtle text-sm italic">{children}</div>;
}
// fetch 失敗を「データ無し」と区別して明示する (silent failure 防止)。
// CTI 用途で「異常なし」と「監視系ダウン」の誤認を防ぐため必須。
export function WidgetError() {
  return (
    <div className="text-critical text-sm italic flex items-center gap-1.5">
      <AlertTriangle className="h-3.5 w-3.5" /> データ取得に失敗しました (バックエンド/DB 要確認)
    </div>
  );
}

// ── 小物 ──
export function Stat({ label, value, danger }: { label: string; value: number | string; danger?: boolean }) {
  // 可読化: 10px uppercase の極小ラベルをやめ、数値を主役に (15px)。
  return (
    <span className="inline-flex items-baseline gap-1.5">
      <span className="text-xs text-fg-muted">{label}</span>
      <span className={`text-[15px] font-semibold tnum ${danger ? "text-critical" : "text-fg"}`}>{value}</span>
    </span>
  );
}
export function Holding({ label, value }: { label: string; value: number }) {
  // 可読化: 数値を主役に (17px)、ラベルは読める xs。
  return (
    <div>
      <div className="text-xs text-fg-subtle truncate" title={label}>{label}</div>
      <div className="text-fg text-lg font-semibold tnum leading-tight">{value.toLocaleString()}</div>
    </div>
  );
}

// 重要度ドット (high=critical / medium=warning / else=subtle)。視認できる最小サイズに。
export function ImportanceDot({ importance, className = "" }: { importance: string | null; className?: string }) {
  const color = importance === "high" ? "bg-critical" : importance === "medium" ? "bg-warning" : "bg-fg-subtle";
  return <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${color} ${className}`} />;
}

// 重要度の左バー色 (1px dot より即判別しやすい)。
function importanceBar(importance: string | null): string {
  return importance === "high" ? "border-critical" : importance === "medium" ? "border-warning" : "border-border-emphasized";
}

export function HeadlineLine({ title, url, importance, feed }: { title: string; url: string; importance: string | null; feed: string }) {
  // 可読化: 1px dot → 重要度の左バー、本文 13.5px・行間ゆとり・上下 padding。
  return (
    <li className={`border-l-2 ${importanceBar(importance)} pl-2.5 py-1`}>
      <a href={url} target="_blank" rel="noopener noreferrer"
        className="block min-w-0 text-[13.5px] leading-snug font-medium text-fg hover:text-accent hover:underline line-clamp-2" title={`${title} — ${feed}`}>
        {title}
      </a>
    </li>
  );
}

// セクション見出し (PMESII軸 / PIR / カテゴリ)。左の accent バーで「ここは見出し」を明示。
export function HeadlineGroup({ label, badge, children }: { label: string; badge?: ReactNode; children: ReactNode }) {
  return (
    <div className="min-w-0">
      <div className="mb-1.5 flex items-center gap-1.5 truncate">
        <span className="w-0.5 h-3.5 rounded-full bg-accent/60 shrink-0" aria-hidden />
        <span className="text-xs font-semibold text-fg-muted truncate">{label}</span>
        {badge}
      </div>
      {children}
    </div>
  );
}

// 横棒 (ラベル + バー + 数値)。importance_mix / pmesii_balance / source_contribution で再利用。
export function HBar({ label, value, max, tone = "accent", suffix }: {
  label: string; value: number; max: number; tone?: "accent" | "critical" | "warning" | "success"; suffix?: ReactNode;
}) {
  const pct = max > 0 ? Math.max(2, (value / max) * 100) : 0;
  const barColor = { accent: "bg-accent/60", critical: "bg-critical/70", warning: "bg-warning/70", success: "bg-success/70" }[tone];
  return (
    <div className="flex items-center gap-2.5 text-[12px]">
      <span className="w-24 shrink-0 truncate text-fg-muted" title={label}>{label}</span>
      <div className="flex-1 h-3 bg-surface-3 rounded-sm overflow-hidden">
        <div className={`h-full ${barColor} rounded-sm`} style={{ width: `${pct}%` }} />
      </div>
      <span className="tnum text-fg w-9 text-right shrink-0">{value.toLocaleString()}</span>
      {suffix}
    </div>
  );
}

export function StatusBadge({ status }: { status: string }) {
  const map: Record<string, string> = {
    success: "bg-success-soft text-success",
    succeeded: "bg-success-soft text-success",
    failed: "bg-critical-soft text-critical",
    error: "bg-critical-soft text-critical",
    running: "bg-accent-soft text-accent-hover",
    pending: "bg-surface-3 text-fg-muted",
    ok: "bg-success-soft text-success",
    warning: "bg-warning-soft text-warning",
    partial_failure: "bg-warning-soft text-warning",
    posted: "bg-success-soft text-success",
    summarized: "bg-accent-soft text-accent-hover",
    extract_failed: "bg-critical-soft text-critical",
    summarize_failed: "bg-critical-soft text-critical",
    post_failed: "bg-critical-soft text-critical",
    skipped_duplicate: "bg-surface-3 text-fg-muted",
  };
  // このバッジは実行 / 死活 / 記事の 3 系統の status を 1 つで扱うため、共通 SSoT を
  // 合成して使う (個別に書くと「一部失敗/部分失敗」のような訳語ドリフトが起きる)。
  // 3 系統とも backend 配信 vocab (run_status / health_status / article_status) を useVocabMap で解決。
  const runStatus = useVocabMap("run_status");
  const health = useVocabMap("health_status");
  const article = useVocabMap("article_status");
  const LABEL: Record<string, string> = {
    ...runStatus,
    ...health,
    ...article,
    success: "成功", // legacy 別名 (succeeded と同義)
    pending: "待機",
  };
  const cls = map[status] || "bg-surface-3 text-fg-muted";
  return <span className={`inline-flex items-center px-2 py-0.5 rounded text-xs font-semibold ${cls}`}>{LABEL[status] ?? status}</span>;
}
