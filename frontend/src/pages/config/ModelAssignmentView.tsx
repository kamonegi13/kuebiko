import { History } from "lucide-react";
import type { ModelStepRow, TierMeta } from "../../api/pages";
import { formatJst } from "../../utils/date";

// モデル割当の表示専用ビュー (2026-10-24 利用者承認)。
// 割当の変更は評価の後に手順書どおり API (ローカル) で行う。この画面は
// 「いま実際にどの処理がどのモデルで動くか」を読むためのもので、編集部品を持たない。
// 緊急の戻しは設定の版履歴 (key=model_tiers) へのリンクから行う。

export const TIER_ORDER = ["narrative", "reasoning", "dialog", "fast", "embedding"] as const;

// 版履歴 (履歴・監査タブ) の model_tiers を選んだ状態で開くリンク。
export const MODEL_HISTORY_HREF = "/app/config?key=model_tiers#history";

// 処理の日本語ラベル (UI 文言規約: 生 enum を直接表示しない)。未登録の step は enum 名で出す。
const STEP_LABELS: Record<string, string> = {
  triage: "重要度判定 (triage)",
  triage_geo_rescue: "重要度判定 — 地政学の救済",
  article_summary: "記事要約・翻訳",
  ioc_llm_verify: "IOC の LLM 検証",
  judgment_classify: "統合判断分類器 (タイプ・動機・発生日・主題)",
  article_translate: "本文翻訳",
  dedup_cluster_judge: "重複排除の最終確認",
  pair_judge: "事象ペア同一性判定",
  event_kind: "記事種別分類",
  severity_axes: "深刻度の軸",
  pir_llm_judge: "SIR 主題判定",
  pir_daily_focus: "SIR 日次要点",
  actor_sync: "アクター名同期",
  digest_deep_dive_select: "週次深掘り (選定)",
  digest_deep_dive: "週次深掘り (本文)",
  event_news: "事象ニュース生成",
  pir_spotlight: "SIR Spotlight",
  synthesis_analysis: "台帳 ACH 分析",
  synthesis_detect: "新規事象検知",
  detect_claim_write: "新規事象の claim 書き直し (和集合・束ね)",
  ledger_ach_aux: "台帳 開設の採点・反証・書き直し",
  ledger_deep_review: "夜間精査 (廃止済み)",
};

const SOURCE_LABELS: Record<ModelStepRow["source"], string> = {
  override: "処理別の上書き",
  inherited: "借り元の上書きを継承",
  tier: "ティア割当",
};

interface ModelAssignmentViewProps {
  tiers: Record<string, string>;
  meta: Record<string, TierMeta>;
  steps: ModelStepRow[];
  configVersion: number | null;
  configUpdatedAt: string | null;
}

function Badge({ children, tone }: { children: string; tone: "warn" | "muted" }) {
  const cls =
    tone === "warn" ? "bg-warning-soft text-warning" : "bg-surface-3 text-fg-subtle";
  return (
    <span className={`text-[12px] px-1.5 py-px rounded-sm font-semibold whitespace-nowrap ${cls}`}>
      {children}
    </span>
  );
}

function StepRow({ row }: { row: ModelStepRow }) {
  return (
    <tr data-testid={`step-${row.step}`} className="border-t border-border-subtle align-top">
      <td className="py-1.5 pr-3 text-fg">{STEP_LABELS[row.step] ?? row.step}</td>
      <td className="py-1.5 pr-3 text-fg-subtle">{row.tier}</td>
      <td className="py-1.5 pr-3">
        <div className="flex items-center gap-1.5 flex-wrap">
          <code className="text-fg">{row.model || "(無効)"}</code>
          {row.is_external && <Badge tone="warn">外部</Badge>}
          {row.differs_from_tier && <Badge tone="muted">上書き</Badge>}
        </div>
        {row.differs_from_tier && (
          <div className="text-[12px] text-fg-subtle mt-0.5">
            {SOURCE_LABELS[row.source]} — ティア割当は <code>{row.tier_model || "(無効)"}</code>
          </div>
        )}
      </td>
    </tr>
  );
}

function TierSummary({ tier, model, meta }: { tier: string; model: string; meta?: TierMeta }) {
  const isExternal = model.startsWith("anthropic:") || model.startsWith("claudecode:");
  return (
    <div className="rounded-lg border border-border-subtle bg-surface-2/40 p-3 space-y-1">
      <div className="flex items-center gap-2 flex-wrap">
        <h3 className="m-0 text-sm font-semibold text-fg">{meta?.label ?? tier}</h3>
        {isExternal && <Badge tone="warn">外部</Badge>}
      </div>
      <code className="text-sm text-fg">{model || "(無効)"}</code>
    </div>
  );
}

export function ModelAssignmentView({
  tiers,
  meta,
  steps,
  configVersion,
  configUpdatedAt,
}: ModelAssignmentViewProps) {
  return (
    <div className="bg-surface-1 border border-border-subtle rounded-lg p-5 space-y-4">
      <div className="flex items-center gap-3 flex-wrap">
        <span className="text-sm text-fg">
          現行 <strong className="tnum">{configVersion != null ? `v${configVersion}` : "未保存"}</strong>
        </span>
        {configUpdatedAt && (
          <span className="text-xs text-fg-subtle tnum">最終更新 {formatJst(configUpdatedAt)}</span>
        )}
        <a
          href={MODEL_HISTORY_HREF}
          className="ml-auto inline-flex items-center gap-1 text-xs text-warning hover:underline"
        >
          <History className="h-3.5 w-3.5" /> 版履歴 (緊急の戻し)
        </a>
      </div>
      <p className="m-0 text-xs text-fg-subtle leading-relaxed">
        この画面は<strong className="text-fg-muted">表示のみ</strong>です。割当の変更は評価の後に
        手順書どおり (ローカルの API) で行います。誤った割当を入れたときは右上の版履歴から 1 つ前の版へ戻せます。
      </p>

      <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
        {TIER_ORDER.map((tier) => (
          <TierSummary key={tier} tier={tier} model={tiers[tier] ?? ""} meta={meta[tier]} />
        ))}
      </div>

      <div className="space-y-1.5">
        <h3 className="m-0 text-sm font-semibold text-fg">処理ごとに実際に使われるモデル</h3>
        <p className="m-0 text-[12px] text-fg-subtle">
          処理別の上書きと継承を解決したあとの値です。外部モデルの処理には「外部」、
          ティア割当と別のモデルで動く処理には「上書き」の印が付きます。
        </p>
        <div className="overflow-x-auto">
          <table className="text-xs w-full">
            <thead>
              <tr className="text-fg-subtle text-[12px] text-left">
                <th className="py-1 pr-3 font-medium">処理</th>
                <th className="py-1 pr-3 font-medium">ティア</th>
                <th className="py-1 pr-3 font-medium">実際のモデル</th>
              </tr>
            </thead>
            <tbody>
              {steps.map((row) => (
                <StepRow key={row.step} row={row} />
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
