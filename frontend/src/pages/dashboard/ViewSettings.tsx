// 閲覧モードの widget 個別設定 (⚙ ポップオーバー)。
//
// カスタマイズモード (配置 + 共有既定の編集) を要さずに「見え方」(期間・件数・表示形式)
// を即時変更する UI。変更は widgetView.ts (端末ローカルの view overrides) へ書かれ、
// 保存ボタン無しで反映される。上書き有効時はギアにアクセント点、期間固定時は窓チップを
// 常時表示して「共有窓との divergence」を必ず可視化する (発見6 の精神)。
import { useQuery } from "@tanstack/react-query";
import { RotateCcw, SlidersHorizontal } from "lucide-react";
import { useState } from "react";
import type { ConfigOption, WidgetDef } from "./shared";
import { clearWidgetView, setWidgetViewValue } from "./widgetView";

// config option の編集 UI (カスタマイズモードの TileChrome と共用)。
// 静的 choices はそのまま、動的 (loadChoices) は fetch して埋める。
export function ConfigOptionEditor({
  opt,
  value,
  onChange,
}: {
  opt: ConfigOption;
  value: string;
  onChange: (v: string) => void;
}) {
  const { data: dynamic } = useQuery({
    // key+label で識別 (同名 key の別 option が将来出ても choices が衝突しない)
    queryKey: ["widget-config-choices", opt.key, opt.label],
    queryFn: () => opt.loadChoices!(),
    enabled: !!opt.loadChoices,
    staleTime: 5 * 60_000,
  });
  const choices = opt.loadChoices
    ? [...(opt.placeholder ? [opt.placeholder] : []), ...(dynamic ?? [])]
    : (opt.choices ?? []);
  const current = value !== "" ? value : (choices[0]?.value ?? "");
  return (
    <label className="flex items-center gap-1 text-[11px] text-fg-muted">
      {opt.label}:
      <select
        value={current}
        onChange={(e) => onChange(e.target.value)}
        className="bg-surface-2 border border-border-subtle rounded px-1 py-0.5 text-[11px] max-w-[180px]"
      >
        {opt.loadChoices && !dynamic && <option value={current}>{value || "読み込み中…"}</option>}
        {choices.map((ch) => (
          <option key={ch.value} value={ch.value}>
            {ch.label}
          </option>
        ))}
      </select>
    </label>
  );
}

// 期間固定時の短い窓ラベル (連動時は null = チップ非表示)。
function windowChipLabel(v: string): string | null {
  if (v === "" || v === "auto") return null;
  if (v === "0") return "全期間";
  if (v === "1") return "24h";
  const n = Number(v);
  return Number.isFinite(n) ? `${n}日` : null;
}

export function ViewSettingsPill({
  def,
  cfg,
  uid,
  overridden,
  mobile,
  intro,
}: {
  def: WidgetDef;
  cfg: Record<string, unknown>;
  uid: string;
  overridden: boolean;
  mobile?: boolean;
  // ページ表示直後の数秒だけ全ピルを見せる (hover 依存の発見性を補う)
  intro?: boolean;
}) {
  const [open, setOpen] = useState(false);
  if (!def.configOptions || def.configOptions.length === 0) return null;

  // 「概観の窓に連動」choice を持つ option (= 期間) が固定されていれば窓チップを出す
  const hasWindowOpt = def.configOptions.some(
    (o) => o.key === "days" && o.choices?.some((c) => c.value === "auto"),
  );
  const chip = hasWindowOpt ? windowChipLabel(String(cfg["days"] ?? "")) : null;
  const alwaysVisible = mobile || open || overridden || !!chip || !!intro;

  return (
    <div
      className={`absolute -top-2.5 right-3 z-30 transition-opacity ${
        alwaysVisible ? "opacity-100" : "opacity-0 group-hover:opacity-100 focus-within:opacity-100"
      }`}
    >
      <button
        onClick={() => setOpen((o) => !o)}
        className="inline-flex items-center gap-1 rounded-full border border-border-emphasized bg-surface-2 px-1.5 py-0.5 text-[10px] text-fg-muted hover:text-fg shadow-sm"
        title="このウィジェットの表示設定 (この端末のみ・即時反映)"
      >
        <SlidersHorizontal className="h-3 w-3" />
        {chip && <span className="font-semibold">{chip}</span>}
        {overridden && <span className="h-1.5 w-1.5 rounded-full bg-accent" />}
      </button>
      {open && (
        <>
          {/* 外側クリックで閉じる backdrop */}
          <div className="fixed inset-0 z-40" onClick={() => setOpen(false)} />
          <div className="absolute right-0 top-full mt-1 z-50 w-64 rounded-lg border border-border-emphasized bg-surface-2 shadow-2xl p-2.5 space-y-2">
            <div className="text-[10px] uppercase tracking-wider text-fg-subtle">
              {def.title} の表示設定
            </div>
            {def.configOptions.map((opt) => (
              <ConfigOptionEditor
                key={opt.key}
                opt={opt}
                value={String(cfg[opt.key] ?? "")}
                onChange={(v) => setWidgetViewValue(uid, opt.key, v)}
              />
            ))}
            <div className="flex items-center justify-between gap-2 pt-1.5 border-t border-border-subtle">
              <span className="text-[10px] text-fg-subtle">この端末のみ・即時反映</span>
              {overridden && (
                <button
                  onClick={() => clearWidgetView(uid)}
                  className="inline-flex items-center gap-1 text-[10px] text-accent hover:underline shrink-0"
                >
                  <RotateCcw className="h-3 w-3" /> 全体の初期設定に戻す
                </button>
              )}
            </div>
          </div>
        </>
      )}
    </div>
  );
}
