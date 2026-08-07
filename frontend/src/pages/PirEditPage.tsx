// PIR 作成 / 編集画面 (/app/pir/edit/{id?})。
//
// Flow (7 step):
//   Step 1: description 入力 (Level 1)
//   Step 2: [AI で構造化] click
//   Step 3: LLM 処理待ち (progress)
//   Step 4: 6 カテゴリ別 structured 提示 (chip + confidence)
//   Step 5: user 修正
//   Step 6: [Preview] でマッチ件数確認
//   Step 7: [Save] で確定

import { useEffect, useState } from "react";
import { AlertTriangle, Bot, Eye, Save } from "lucide-react";
import { pageContainer } from "../components/Page";
import { PirMatchTree } from "../components/PirMatchTree";
import { vocabLabel } from "../hooks/useVocab";
import { useMutation, useQuery } from "@tanstack/react-query";
import {
  pirApi,
  newEmptyPir,
  type Pir,
  type DerivedSignal,
  type RoutingImportance,
  type SpotlightWindow,
  type ConfidenceLevel,
} from "../api/pir";
import { pagesApi } from "../api/pages";

// F1 (アクター辞書 Phase2): PIR actors の辞書解決チェック。評価側
// (src/pir/evaluator.py _resolve_pir_actor_names) の双方向 word-boundary 照合の
// クライアント近似 — 解決されない名前はタイトル一致のみに縮退するため、入力時に警告する。
// 評価ロジック自体は不変 (表示と補完のみ)。
function isResolvedByDictionary(chip: string, dictNamesLower: string[]): boolean {
  const c = chip.trim().toLowerCase();
  if (c.length < 3) return false;
  return dictNamesLower.some((n) => n === c || n.includes(c) || c.includes(n));
}

// B2: 派生 signal 条件の選択肢 (DerivedSignal と同期)
const DERIVED_SIGNAL_OPTIONS: { value: DerivedSignal; label: string }[] = [
  { value: "kev", label: "KEV / 実環境悪用" },
  { value: "zero_day", label: "0day" },
  { value: "japan_critical", label: "日本重要インフラ標的" },
  { value: "japan_relevant", label: "日本関連脅威" },
  { value: "known_apt", label: "既知 APT 言及" },
  { value: "apt_leak", label: "APT 内部リーク" },
  { value: "security_relevant", label: "広義セキュリティ関連" },
];

export function PirEditPage({ pirId }: { pirId: string | null }) {
  const isNew = !pirId;
  const [pir, setPir] = useState<Pir>(() => newEmptyPir());
  const [confidence, setConfidence] = useState<Record<string, ConfidenceLevel>>({});
  const [rationale, setRationale] = useState<string>("");
  const [warnings, setWarnings] = useState<string[]>([]);
  const [previewCount, setPreviewCount] = useState<number | null>(null);
  const [previewSamples, setPreviewSamples] = useState<{ title: string; channel: string | null }[]>([]);
  const [saveError, setSaveError] = useState<string | null>(null);
  // 照合条件の JSON 直編集 (上級者向け)。null = 非編集中 (pir.match を表示)。
  const [matchJsonDraft, setMatchJsonDraft] = useState<string | null>(null);
  const [matchJsonError, setMatchJsonError] = useState<string | null>(null);

  // Load existing PIR for edit
  const { data: existing } = useQuery({
    queryKey: ["pir-edit-load", pirId],
    queryFn: () => pirApi.get(pirId!),
    enabled: !isNew,
  });

  // F1 (アクター辞書 Phase2): 攻撃主体入力の辞書補完 + 解決チェック用。
  // ["actors"] は辞書ページ/CommandPalette と共有キャッシュ。
  const { data: actorDict } = useQuery({
    queryKey: ["actors"],
    queryFn: () => pagesApi.getActors(),
  });
  const actorDictNames = (actorDict?.actors ?? [])
    .flatMap((a) => [a.canonical, ...(a.aliases ?? [])])
    .filter(Boolean);
  const actorDictNamesLower = actorDictNames.map((n) => n.toLowerCase());

  useEffect(() => {
    if (existing) {
      setPir(existing);
      setRationale(existing.metadata.rationale);
    }
  }, [existing]);

  const compileMut = useMutation({
    mutationFn: () => pirApi.compile(pir.title, pir.description, pir.id || undefined),
    onSuccess: (resp) => {
      // Replace structured fields with compiled, keep id/title/description from user
      setPir({
        ...resp.pir,
        id: pir.id || resp.pir.id,
        title: pir.title,
        description: pir.description,
        enabled: pir.enabled,
        // 照合条件: compile が新しいツリーを返したら置換 (再生成の意味論)。
        // 返せなかったら既存を保全する — 黙って旧方式に退行させない (P1 修正 2026-07-23)。
        match: resp.pir.match ?? pir.match,
        llm_judge: resp.pir.match ? resp.pir.llm_judge : pir.llm_judge,
        // Spotlight は編集時は既存設定を保全 (compile の提案は新規作成時のみ採用)
        spotlight: isNew ? resp.pir.spotlight : pir.spotlight,
      });
      setConfidence(resp.confidence);
      setRationale(resp.rationale);
      // 調整済みの照合条件を AI 生成が置き換えた場合は明示警告する — 既存ツリーは
      // 実測 A/B で調整されていることがあり、無自覚な置換は精度退行になり得る。
      const replaced = !isNew && pir.match && resp.pir.match;
      setWarnings(
        replaced
          ? [
              "調整済みの照合条件を AI 生成で置き換えました。試算で件数を確認してから保存してください (キャンセルで破棄できます)。",
              ...resp.warnings,
            ]
          : resp.warnings,
      );
      setMatchJsonDraft(null);
    },
  });

  const previewMut = useMutation({
    mutationFn: () => pirApi.preview(pir, 7 * 24),
    onSuccess: (resp) => {
      setPreviewCount(resp.match_count);
      setPreviewSamples(resp.samples.slice(0, 5).map((s) => ({ title: s.title, channel: s.posted_channel })));
    },
  });

  const saveMut = useMutation({
    mutationFn: () => pirApi.save(pir, isNew),
    onSuccess: (resp) => {
      window.location.href = `/app/pir/${encodeURIComponent(resp.id)}`;
    },
    onError: (e: Error) => setSaveError(e.message),
  });

  const updatePir = (patch: Partial<Pir>) => setPir((p) => ({ ...p, ...patch }));
  const updateStrong = (cat: keyof Pir["strong_signals"], items: string[]) =>
    setPir((p) => ({ ...p, strong_signals: { ...p.strong_signals, [cat]: items } }));
  const toggleSignal = (sig: DerivedSignal) =>
    setPir((p) => {
      const cur = p.strong_signals.signals ?? [];
      const next = cur.includes(sig) ? cur.filter((s) => s !== sig) : [...cur, sig];
      return { ...p, strong_signals: { ...p.strong_signals, signals: next } };
    });

  return (
    <div className={`${pageContainer("wide")} space-y-4`}>
      <div className="flex items-baseline gap-2 flex-wrap">
        <a href={pirId ? `/app/pir/${encodeURIComponent(pirId)}` : "/app/pir"} className="text-fg-muted text-sm no-underline hover:text-fg">
          ← {pirId ? "詳細" : "一覧"}に戻る
        </a>
        <h2 className="m-0 text-xl font-bold text-fg tracking-tight">
          {isNew ? "新規 PIR 作成" : `PIR 編集: ${pir.id}`}
        </h2>
      </div>

      {/* Step 1: 基本情報 */}
      <Section title="ステップ1: 基本情報 (説明文から作成)">
        <div className="space-y-3">
          {isNew && (
            <div>
              <label className="block text-[10px] uppercase tracking-wider text-fg-muted mb-1">PIR id (英数 + アンダースコア)</label>
              <input
                type="text"
                value={pir.id}
                onChange={(e) => updatePir({ id: e.target.value.replace(/[^a-zA-Z0-9_]/g, "_") })}
                placeholder="pir_my_new_priority"
                className="w-full h-8 px-2 bg-surface-2 border border-border-subtle rounded text-sm font-mono"
              />
            </div>
          )}
          <div>
            <label className="block text-[10px] uppercase tracking-wider text-fg-muted mb-1">タイトル</label>
            <input
              type="text"
              value={pir.title}
              onChange={(e) => updatePir({ title: e.target.value })}
              placeholder="例: 日本標的の APT 動向"
              className="w-full h-8 px-2 bg-surface-2 border border-border-subtle rounded text-sm"
            />
          </div>
          <div>
            <label className="block text-[10px] uppercase tracking-wider text-fg-muted mb-1">
              説明文 (この文章を元に AI が条件を自動生成します)
            </label>
            <textarea
              value={pir.description}
              onChange={(e) => updatePir({ description: e.target.value })}
              placeholder="例: 中国 / 北朝鮮 / ロシア の state-aligned APT による日本企業 / 政府を標的としたキャンペーンを最優先で検知したい。CISA KEV や JPCERT 緊急が primary signal。"
              rows={5}
              className="w-full p-2 bg-surface-2 border border-border-subtle rounded text-sm"
            />
          </div>
          <div className="flex items-center gap-2 flex-wrap">
            <button
              onClick={() => compileMut.mutate()}
              disabled={compileMut.isPending || !pir.title.trim() || !pir.description.trim()}
              className="bg-accent hover:bg-accent-hover text-fg rounded px-3 py-1.5 text-sm font-semibold disabled:opacity-50 inline-flex items-center gap-1.5"
            >
              <Bot className="h-4 w-4" />
              {compileMut.isPending ? "AI 処理中 (10〜60秒)..." : "AI で条件を生成"}
            </button>
            {compileMut.isError && (
              <span className="text-critical text-xs">エラー: {(compileMut.error as Error).message}</span>
            )}
            <label className="flex items-center gap-1.5 text-xs text-fg-muted ml-auto">
              <input type="checkbox" checked={pir.enabled} onChange={(e) => updatePir({ enabled: e.target.checked })} />
              有効
            </label>
          </div>
          {warnings.length > 0 && (
            <div className="bg-warning-soft border border-warning rounded p-2 text-xs text-fg-muted">
              {warnings.map((w, i) => <div key={i} className="flex items-center gap-1"><AlertTriangle className="h-3.5 w-3.5 shrink-0" /> {w}</div>)}
            </div>
          )}
          {rationale && (
            <div className="text-xs text-fg-muted italic">AI 判断根拠: {rationale}</div>
          )}
        </div>
      </Section>

      {/* 照合条件 (実際の記事照合を駆動する条件ツリー、authoring 統一 2026-07-23) */}
      <Section title="照合条件 (この条件で記事が照合されます)">
        <div className="space-y-3">
          {pir.match ? (
            <div className="bg-surface-2 border border-border-subtle rounded p-2.5">
              <PirMatchTree node={pir.match} />
            </div>
          ) : (
            <div className="text-xs text-fg-muted">
              未設定 — 旧方式 (下の重要シグナルのキーワード照合) で動作します。
              「AI で条件を生成」を実行すると説明文から照合条件が作られます。
            </div>
          )}
          <div className="flex flex-wrap items-center gap-3">
            <label className="flex items-center gap-1.5 text-sm">
              <input
                type="checkbox"
                checked={pir.llm_judge.enabled}
                onChange={(e) => updatePir({ llm_judge: { ...pir.llm_judge, enabled: e.target.checked } })}
                disabled={!pir.match}
              />
              <span>AI の主題判定を必須にする</span>
            </label>
            <span className="text-[10px] text-fg-subtle">
              上の条件を「候補の絞り込み」とし、記事が本当にこの PIR を主題としているかを
              AI が確定します (判定は夜間の自動処理後に反映。プレビューは候補のみ)。
            </span>
          </div>
          {pir.llm_judge.enabled && (
            <div>
              <label className="block text-[10px] uppercase tracking-wider text-fg-muted mb-1">
                判定基準 (空欄ならタイトル + 説明文が基準になります)
              </label>
              <textarea
                value={pir.llm_judge.question}
                onChange={(e) => updatePir({ llm_judge: { ...pir.llm_judge, question: e.target.value } })}
                placeholder="例: 国家機関による APT への攻撃帰属の公表が記事の主題か。無関係な起訴・制裁は不適合。"
                rows={2}
                className="w-full p-2 bg-surface-2 border border-border-subtle rounded text-sm"
              />
            </div>
          )}
          <details>
            <summary className="text-xs text-fg-muted cursor-pointer select-none">
              上級者向け: 条件を直接編集 (JSON)
            </summary>
            <div className="mt-2 space-y-2">
              <textarea
                value={matchJsonDraft ?? (pir.match ? JSON.stringify(pir.match, null, 2) : "")}
                onChange={(e) => {
                  setMatchJsonDraft(e.target.value);
                  setMatchJsonError(null);
                }}
                rows={8}
                spellCheck={false}
                className="w-full p-2 bg-surface-2 border border-border-subtle rounded text-xs font-mono"
              />
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  onClick={() => {
                    if (matchJsonDraft === null) return;
                    const text = matchJsonDraft.trim();
                    if (!text) {
                      updatePir({ match: null });
                      setMatchJsonDraft(null);
                      return;
                    }
                    try {
                      updatePir({ match: JSON.parse(text) });
                      setMatchJsonDraft(null);
                    } catch (e) {
                      setMatchJsonError(`JSON が不正です: ${(e as Error).message}`);
                    }
                  }}
                  disabled={matchJsonDraft === null}
                  className="bg-surface-3 hover:bg-surface-2 text-fg rounded px-2.5 py-1 text-xs border border-border-subtle disabled:opacity-50"
                >
                  条件に反映
                </button>
                <span className="text-[10px] text-fg-subtle">
                  反映後にプレビューで件数を確認してから保存してください (構造は保存時にも検証されます)。
                </span>
              </div>
              {matchJsonError && <div className="text-critical text-xs">{matchJsonError}</div>}
            </div>
          </details>
        </div>
      </Section>

      {/* Step 4-5: 6 カテゴリ別 structured */}
      <Section title="重要シグナル (補助情報 — 脅威アクター連携・旧方式の予備)">
        {pir.match && (
          <div className="text-[10px] text-fg-subtle mb-2">
            照合は上の「照合条件」で行われます。この欄は補助用途
            (攻撃主体は脅威アクター画面との連携に使用。他は旧方式に切り戻した場合の予備) です。
          </div>
        )}
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          <ChipEditor
            label="検索語彙"
            items={pir.strong_signals.keywords}
            onChange={(items) => updateStrong("keywords", items)}
            confidence={confidence.keywords}
            placeholder="ransomware, breach 等"
          />
          <ChipEditor
            label="攻撃主体"
            items={pir.strong_signals.actors}
            onChange={(items) => updateStrong("actors", items)}
            confidence={confidence.actors}
            placeholder="Volt Typhoon, APT41 等"
            suggestions={actorDictNames}
            datalistId="pir-actor-dict-names"
            chipWarn={(it) => !isResolvedByDictionary(it, actorDictNamesLower)}
            warnHint="警告色の名前は辞書に解決されず、照合がタイトル一致のみに縮退します — 綴りを辞書の表記に合わせるか、辞書に登録してください"
          />
          <ChipEditor
            label="対象業界"
            items={pir.strong_signals.sectors}
            onChange={(items) => updateStrong("sectors", items)}
            confidence={confidence.sectors}
            placeholder="defense, finance, energy 等"
          />
          <ChipEditor
            label="被害/標的国 (ISO 2文字コード)"
            items={pir.strong_signals.countries}
            onChange={(items) => updateStrong("countries", items)}
            confidence={confidence.countries}
            placeholder="JP, US 等 (被害側の国)"
          />
          <ChipEditor
            label="アクター国籍 (ISO 2文字コード)"
            items={pir.strong_signals.actor_nations ?? []}
            onChange={(items) => updateStrong("actor_nations", items)}
            placeholder="CN, KP, RU 等 (加害側の国家帰属)"
          />
          <ChipEditor
            label="信頼情報源 (情報源名の一部)"
            items={pir.strong_signals.feed_titles}
            onChange={(items) => updateStrong("feed_titles", items)}
            confidence={confidence.feed_titles}
            placeholder="JPCERT, CISA, Microsoft 等"
            className="md:col-span-2"
          />
          <div className="md:col-span-2">
            <div className="text-sm font-medium text-fg mb-1.5">
              派生シグナル条件 (任意、配信判定に使う高精度な条件)
            </div>
            <div className="text-xs text-fg-subtle mb-2">
              キーワードでは表せない判定 (KEV カタログ照合・0day 等) を PIR の該当条件に加えます。
              KPI やプレビュー (集計値) には反映されません。
            </div>
            <div className="flex flex-wrap gap-1.5">
              {DERIVED_SIGNAL_OPTIONS.map((opt) => {
                const active = (pir.strong_signals.signals ?? []).includes(opt.value);
                return (
                  <button
                    key={opt.value}
                    type="button"
                    onClick={() => toggleSignal(opt.value)}
                    className={`px-2.5 py-1 rounded-full text-xs font-medium border transition-colors ${
                      active
                        ? "bg-accent-soft text-accent-hover border-accent"
                        : "bg-surface-2 text-fg-muted border-border-subtle hover:bg-surface-3"
                    }`}
                  >
                    {opt.label}
                  </button>
                );
              })}
            </div>
          </div>
        </div>
      </Section>

      {/* 評価 + Spotlight */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        <Section title="重要度評価への影響">
          <div className="space-y-3 text-sm">
            <Selector
              label="目標重要度"
              value={pir.target_importance}
              options={["auto", "high", "medium", "low"]}
              optionLabels={{
                auto: "自動",
                high: vocabLabel("importance", "high"),
                medium: vocabLabel("importance", "medium"),
                low: vocabLabel("importance", "low"),
              }}
              onChange={(v) => updatePir({ target_importance: v as RoutingImportance })}
            />
            <div className="text-[10px] text-fg-subtle italic mt-1">
              high / medium にすると、この PIR が重要度判定の基準として使われます。
              "auto" は基準に使いません。<strong>配信先チャンネルはここでは決めません</strong>
              (チャンネル決定は <a href="/app/flow" className="text-accent hover:underline not-italic">情報フロー</a> の
              配信ルールが重要度とシグナルから判定します)。
            </div>
          </div>
        </Section>
        <Section title="Spotlight">
          <div className="space-y-3 text-sm">
            <label className="flex items-center gap-2">
              <input
                type="checkbox"
                checked={pir.spotlight.enabled}
                onChange={(e) => updatePir({ spotlight: { ...pir.spotlight, enabled: e.target.checked } })}
              />
              <span>有効</span>
            </label>
            <div>
              <label className="block text-[10px] uppercase tracking-wider text-fg-muted mb-1">タイトル</label>
              <input
                type="text"
                value={pir.spotlight.title}
                onChange={(e) => updatePir({ spotlight: { ...pir.spotlight, title: e.target.value } })}
                placeholder="日本標的 APT"
                className="w-full h-8 px-2 bg-surface-2 border border-border-subtle rounded text-sm"
              />
            </div>
            <Selector
              label="集計期間"
              value={pir.spotlight.window}
              options={["daily", "weekly", "monthly"]}
              optionLabels={{
                daily: vocabLabel("period_type", "daily"),
                weekly: vocabLabel("period_type", "weekly"),
                monthly: vocabLabel("period_type", "monthly"),
              }}
              onChange={(v) => updatePir({ spotlight: { ...pir.spotlight, window: v as SpotlightWindow } })}
            />
          </div>
        </Section>
      </div>

      {/* Step 6: Preview */}
      <Section title="ステップ6: 試算 (過去 7 日の該当を確認)">
        <div className="flex items-center gap-2 flex-wrap">
          <button
            onClick={() => previewMut.mutate()}
            disabled={previewMut.isPending}
            className="bg-accent-subtle hover:bg-accent-soft text-accent-hover border border-accent-soft rounded px-3 py-1.5 text-sm font-semibold disabled:opacity-50 inline-flex items-center gap-1.5"
          >
            <Eye className="h-4 w-4" />
            {previewMut.isPending ? "計算中..." : "試算"}
          </button>
          {previewCount !== null && (
            <span className="text-fg-muted text-sm">
              過去 7 日に <strong className="text-fg">{previewCount}</strong> 件 該当
              {previewCount === 0 && " — 条件が厳しすぎる可能性"}
            </span>
          )}
        </div>
        {previewSamples.length > 0 && (
          <ul className="mt-2 m-0 p-0 list-none space-y-1">
            {previewSamples.map((s, i) => (
              <li key={i} className="text-xs text-fg-muted">
                <span className="bg-surface-3 text-fg-subtle px-1.5 py-0.5 rounded font-mono mr-2">{s.channel || "—"}</span>
                {s.title}
              </li>
            ))}
          </ul>
        )}
      </Section>

      {/* Save */}
      <div className="flex items-center gap-2 sticky bottom-0 bg-bg/95 backdrop-blur py-3 -mx-4 px-4 border-t border-border-subtle">
        <button
          onClick={() => saveMut.mutate()}
          disabled={saveMut.isPending || !pir.id || !pir.title}
          className="bg-accent hover:bg-accent-hover text-fg rounded px-4 py-2 text-sm font-semibold disabled:opacity-50 inline-flex items-center gap-1.5"
        >
          <Save className="h-4 w-4" />
          {saveMut.isPending ? "保存中..." : "保存"}
        </button>
        <a
          href={pirId ? `/app/pir/${encodeURIComponent(pirId)}` : "/app/pir"}
          className="text-fg-muted hover:text-fg text-sm no-underline px-3 py-2"
        >キャンセル</a>
        {saveError && <span className="text-critical text-xs">{saveError}</span>}
      </div>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="bg-surface-1 border border-border-subtle rounded-lg p-4">
      <h3 className="m-0 mb-3 text-sm font-semibold text-fg">{title}</h3>
      {children}
    </div>
  );
}

function ChipEditor({
  label,
  items,
  onChange,
  confidence,
  placeholder,
  className = "",
  suggestions,
  datalistId,
  chipWarn,
  warnHint,
}: {
  label: string;
  items: string[];
  onChange: (items: string[]) => void;
  confidence?: ConfidenceLevel;
  placeholder?: string;
  className?: string;
  // F1: 入力補完 (datalist) — suggestions と一意な datalistId をセットで渡す
  suggestions?: string[];
  datalistId?: string;
  // F1: チップ単位の警告判定 (true = 警告表示)。warnHint は警告があるときの説明文
  chipWarn?: (item: string) => boolean;
  warnHint?: string;
}) {
  const [input, setInput] = useState("");
  const confDot = confidence === "high" ? "bg-success" : confidence === "medium" ? "bg-warning" : confidence ? "bg-fg-subtle" : "";
  const hasWarn = chipWarn ? items.some((it) => chipWarn(it)) : false;
  return (
    <div className={className}>
      <div className="flex items-center gap-1.5 mb-1">
        <span className="text-[10px] uppercase tracking-wider text-fg-muted">{label}</span>
        {confidence && (
          <span title={`確度: ${confidence}`} className={`w-1.5 h-1.5 rounded-full ${confDot}`} />
        )}
      </div>
      <div className="flex flex-wrap gap-1 bg-surface-2 border border-border-subtle rounded p-2 min-h-[2.5rem]">
        {items.map((it, i) => {
          const warn = chipWarn ? chipWarn(it) : false;
          return (
            <span
              key={i}
              title={warn ? warnHint : undefined}
              className={`px-2 py-0.5 rounded text-xs font-mono inline-flex items-center gap-1 ${
                warn
                  ? "bg-warning-soft text-warning border border-warning/40"
                  : "bg-surface-3 text-fg"
              }`}
            >
              {it}
              <button
                onClick={() => onChange(items.filter((_, idx) => idx !== i))}
                className="text-fg-subtle hover:text-critical"
              >×</button>
            </span>
          );
        })}
        <input
          type="text"
          value={input}
          list={suggestions && datalistId ? datalistId : undefined}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if ((e.key === "Enter" || e.key === ",") && input.trim()) {
              e.preventDefault();
              if (!items.includes(input.trim())) onChange([...items, input.trim()]);
              setInput("");
            } else if (e.key === "Backspace" && !input && items.length > 0) {
              onChange(items.slice(0, -1));
            }
          }}
          placeholder={items.length === 0 ? placeholder : "+ 追加 (Enter)"}
          className="flex-1 min-w-[100px] bg-transparent text-sm outline-none placeholder:text-fg-subtle"
        />
        {suggestions && datalistId && (
          <datalist id={datalistId}>
            {suggestions.map((s) => (
              <option key={s} value={s} />
            ))}
          </datalist>
        )}
      </div>
      {hasWarn && warnHint && (
        <p className="m-0 mt-1 text-[11px] text-warning">{warnHint}</p>
      )}
    </div>
  );
}

function Selector({
  label,
  value,
  options,
  optionLabels,
  onChange,
}: {
  label: string;
  value: string;
  options: string[];
  optionLabels?: Record<string, string>;
  onChange: (v: string) => void;
}) {
  return (
    <div>
      <label className="block text-[10px] uppercase tracking-wider text-fg-muted mb-1">{label}</label>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="w-full h-8 px-2 bg-surface-2 border border-border-subtle rounded text-sm font-mono"
      >
        {options.map((o) => <option key={o} value={o}>{optionLabels?.[o] ?? o}</option>)}
      </select>
    </div>
  );
}
