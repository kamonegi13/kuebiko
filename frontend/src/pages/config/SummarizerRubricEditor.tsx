// summarizer プロンプトの層分け (WP3): 記事要約・翻訳プロンプトの構造化エディタ。
// 判定基準 (rubric) は DB (config_store, key=summarizer_rubric) が SSoT — legacy の
// prompts/briefing/summarizer.j2 は rollback 用の据置コピー (I8: このエディタから
// .j2 へ書き戻すコードは書かない)。レイアウトは設計書 §6-3、API 契約は §5 に従う。

import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Save } from "lucide-react";
import {
  promptRubricApi,
  type ContractField,
  type RubricExample,
  type SummarizerRubric,
} from "../../api/promptRubric";
import { RubricSectionCard } from "./RubricSectionCard";

const SUMMARIZER_PATH = "prompts/briefing/summarizer.j2";
const PREVIEW_DEBOUNCE_MS = 600;

function validateJson(text: string): string | null {
  try {
    JSON.parse(text);
    return null;
  } catch (e: unknown) {
    return e instanceof Error ? e.message : "不明なエラー";
  }
}

function formatTimestamp(iso: string): string {
  if (!iso) return "";
  return iso.slice(0, 16).replace("T", " ");
}

interface SummarizerRubricEditorProps {
  // 「raw (.j2) を表示」トグル押下時に親 (PromptsEditor) が raw editor へ切替るための callback。
  onSwitchToRaw: () => void;
}

export function SummarizerRubricEditor({ onSwitchToRaw }: SummarizerRubricEditorProps) {
  const qc = useQueryClient();
  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["summarizer-rubric"],
    queryFn: () => promptRubricApi.getRubric(),
  });

  const [draft, setDraft] = useState<SummarizerRubric | null>(null);
  const [dirty, setDirty] = useState(false);
  const [testArticleId, setTestArticleId] = useState("");
  const [elapsedSec, setElapsedSec] = useState(0);
  const [saveMessage, setSaveMessage] = useState<{ kind: "success" | "error"; text: string } | null>(null);
  const [saveWarnings, setSaveWarnings] = useState<string[]>([]);

  useEffect(() => {
    if (data && draft === null) {
      setDraft(data.rubric);
      setDirty(false);
    }
  }, [data, draft]);

  const contractMap = useMemo(() => {
    const m = new Map<string, ContractField>();
    for (const f of data?.contract.fields ?? []) m.set(f.name, f);
    return m;
  }, [data]);

  // プレビュー/検証: 編集後 600ms デバウンスで自動実行 (初回ロード時も 1 回走る)。
  const previewMut = useMutation({
    mutationFn: (rubric: SummarizerRubric) => promptRubricApi.previewRubric(rubric),
  });

  useEffect(() => {
    if (!draft) return;
    const t = setTimeout(() => previewMut.mutate(draft), PREVIEW_DEBOUNCE_MS);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draft]);

  const preview = previewMut.data;
  const valid = preview?.valid ?? false;

  const saveMut = useMutation({
    mutationFn: (rubric: SummarizerRubric) => promptRubricApi.saveRubric(rubric),
    onSuccess: (res) => {
      setDirty(false);
      setSaveWarnings(res.warnings);
      setSaveMessage({ kind: "success", text: `保存しました (v${res.version})` });
      qc.invalidateQueries({ queryKey: ["summarizer-rubric"] });
      qc.invalidateQueries({ queryKey: ["prompts-file", SUMMARIZER_PATH] });
      setTimeout(() => setSaveMessage(null), 4000);
    },
    onError: (e: Error) => setSaveMessage({ kind: "error", text: e.message }),
  });

  const testMut = useMutation({
    mutationFn: () => promptRubricApi.testRubric({ rubric: draft, articleId: testArticleId.trim() || null }),
  });

  useEffect(() => {
    if (!testMut.isPending) {
      setElapsedSec(0);
      return;
    }
    const start = Date.now();
    const id = setInterval(() => setElapsedSec(Math.floor((Date.now() - start) / 1000)), 1000);
    return () => clearInterval(id);
  }, [testMut.isPending]);

  const updateIntro = (intro: string) => {
    if (!draft) return;
    setDraft({ ...draft, intro });
    setDirty(true);
  };

  const updateSectionBody = (fieldId: string, body: string) => {
    if (!draft) return;
    setDraft({
      ...draft,
      sections: draft.sections.map((s) => (s.field_id === fieldId ? { ...s, body } : s)),
    });
    setDirty(true);
  };

  const updateExample = (idx: number, patch: Partial<RubricExample>) => {
    if (!draft) return;
    setDraft({
      ...draft,
      examples: draft.examples.map((ex, i) => (i === idx ? { ...ex, ...patch } : ex)),
    });
    setDirty(true);
  };

  const handleSave = () => {
    if (!draft) return;
    if (!window.confirm("記事要約プロンプトの判定基準を保存します。次回の実行から反映されます。よろしいですか?")) return;
    saveMut.mutate(draft);
  };

  if (isLoading) return <div className="text-fg-muted text-sm">読み込み中...</div>;
  if (isError || !data || !draft) {
    return (
      <div className="bg-critical-soft border border-critical rounded-lg p-4 text-sm text-critical">
        判定基準の取得に失敗しました{error instanceof Error ? `: ${error.message}` : ""}
      </div>
    );
  }

  const rubricSections = draft.sections.filter((s) => s.kind === "rubric");
  const suppressedSections = draft.sections.filter((s) => s.kind === "suppressed");

  return (
    <div className="space-y-4">
      {data.runtime.active_source === "legacy_file" && (
        <div className="bg-critical-soft border border-critical rounded-lg p-3 text-sm text-critical inline-flex items-start gap-1.5">
          <AlertTriangle className="h-4 w-4 shrink-0 mt-0.5" />
          <span>
            現在この基準は使われていません (理由: {data.runtime.fallback_reason || "不明"})。
            legacy ファイルがそのまま LLM に渡されています。
          </span>
        </div>
      )}

      {/* ヘッダ行 */}
      <div className="flex items-center justify-between flex-wrap gap-2">
        <div>
          <h3 className="m-0 text-md font-semibold text-fg">記事要約・翻訳 (summarizer)</h3>
          <p className="m-0 text-[11px] text-fg-subtle">
            有効な基準:{" "}
            {data.runtime.active_source === "composed"
              ? `DB v${data.runtime.version ?? "?"}`
              : "legacy ファイル"}
            {data.runtime.saved_at && ` (${formatTimestamp(data.runtime.saved_at)})`}
          </p>
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          {preview && (
            <span className="text-[11px] text-fg-subtle tnum">
              合成 {preview.composed_chars.toLocaleString()} 字 / legacy {preview.legacy_chars.toLocaleString()} 字
            </span>
          )}
          {saveMessage && (
            <span
              className={`text-xs px-2 py-0.5 rounded ${
                saveMessage.kind === "success" ? "bg-success-soft text-success" : "bg-critical-soft text-critical"
              }`}
            >
              {saveMessage.text}
            </span>
          )}
          <button
            onClick={onSwitchToRaw}
            className="px-3 py-1.5 rounded text-xs font-medium border border-border-subtle text-fg-muted hover:text-fg hover:bg-surface-2"
          >
            raw (.j2) を表示
          </button>
          <button
            onClick={handleSave}
            disabled={!dirty || !valid || saveMut.isPending}
            className="inline-flex items-center gap-1 bg-accent text-bg px-4 py-1.5 rounded-md font-semibold text-sm hover:bg-accent-hover disabled:opacity-40 transition-colors"
          >
            {saveMut.isPending ? "保存中..." : <><Save className="h-4 w-4" /> 保存</>}
          </button>
        </div>
      </div>

      {/* 検証結果 */}
      {preview && (preview.errors.length > 0 || preview.warnings.length > 0 || preview.missing_fields.length > 0 || preview.unknown_fields.length > 0) && (
        <div className="space-y-1">
          {preview.errors.map((e, i) => (
            <div key={`err-${i}`} className="text-xs text-critical bg-critical-soft border border-critical rounded px-2 py-1">
              {e}
            </div>
          ))}
          {preview.unknown_fields.length > 0 && (
            <div className="text-xs text-critical bg-critical-soft border border-critical rounded px-2 py-1">
              未知のフィールド: {preview.unknown_fields.join(", ")}
            </div>
          )}
          {preview.warnings.map((w, i) => (
            <div key={`warn-${i}`} className="text-xs text-warning bg-warning-soft border border-warning rounded px-2 py-1">
              {w}
            </div>
          ))}
          {preview.missing_fields.length > 0 && (
            <div className="text-xs text-warning bg-warning-soft border border-warning rounded px-2 py-1">
              未宣言のフィールド: {preview.missing_fields.join(", ")}
            </div>
          )}
        </div>
      )}
      {saveWarnings.length > 0 && (
        <div className="space-y-1">
          {saveWarnings.map((w, i) => (
            <div key={i} className="text-xs text-warning bg-warning-soft border border-warning rounded px-2 py-1">
              保存時の注意: {w}
            </div>
          ))}
        </div>
      )}

      {/* 導入文 */}
      <div className="bg-surface-1 border border-border-subtle rounded-lg p-4 space-y-1.5">
        <label className="text-xs text-fg-subtle block">導入文 (intro)</label>
        <textarea
          value={draft.intro}
          onChange={(e) => updateIntro(e.target.value)}
          rows={3}
          spellCheck={false}
          className="w-full rounded border border-border-subtle bg-surface-2 px-2 py-1.5 text-sm text-fg font-mono leading-relaxed"
        />
      </div>

      {/* 判定基準カード */}
      <div className="space-y-3">
        {rubricSections.map((s) => (
          <RubricSectionCard
            key={s.field_id}
            section={s}
            contract={contractMap.get(s.field_id)}
            onChangeBody={(body) => updateSectionBody(s.field_id, body)}
          />
        ))}
      </div>

      {/* 出力させないフィールド */}
      {suppressedSections.length > 0 && (
        <div className="space-y-2">
          <h4 className="m-0 text-sm font-semibold text-fg">出力させないフィールド</h4>
          {suppressedSections.map((s) => (
            <RubricSectionCard
              key={s.field_id}
              section={s}
              contract={contractMap.get(s.field_id)}
              onChangeBody={(body) => updateSectionBody(s.field_id, body)}
            />
          ))}
        </div>
      )}

      {/* 出力例 */}
      {draft.examples.length > 0 && (
        <div className="space-y-3">
          <h4 className="m-0 text-sm font-semibold text-fg">出力例</h4>
          {draft.examples.map((ex, i) => {
            const jsonError = validateJson(ex.json_text);
            return (
              <div key={i} className="bg-surface-1 border border-border-subtle rounded-lg p-4 space-y-1.5">
                <input
                  value={ex.label}
                  onChange={(e) => updateExample(i, { label: e.target.value })}
                  placeholder="ラベル (例: 参考)"
                  className="w-full rounded border border-border-subtle bg-surface-2 px-2 py-1 text-xs text-fg"
                />
                <textarea
                  value={ex.json_text}
                  onChange={(e) => updateExample(i, { json_text: e.target.value })}
                  rows={8}
                  spellCheck={false}
                  className={`w-full rounded border px-2 py-1.5 font-mono text-xs text-fg leading-relaxed ${
                    jsonError ? "border-critical" : "border-border-subtle"
                  } bg-surface-2`}
                />
                {jsonError && <div className="text-[11px] text-critical">JSON が不正です: {jsonError}</div>}
              </div>
            );
          })}
        </div>
      )}

      {/* 合成プレビュー / 展開後プロンプト */}
      <details className="bg-surface-1 border border-border-subtle rounded-lg">
        <summary className="cursor-pointer px-4 py-2 text-sm text-fg-muted select-none">合成プレビュー</summary>
        <div className="px-4 pb-3 space-y-3">
          <div>
            <div className="text-[11px] text-fg-subtle mb-1">合成後テンプレート (Jinja タグ未展開)</div>
            <pre className="whitespace-pre-wrap break-words bg-black/60 text-fg font-mono text-[11px] p-3 rounded max-h-96 overflow-y-auto">
              {preview?.composed ?? ""}
            </pre>
          </div>
          <div>
            <div className="text-[11px] text-fg-subtle mb-1">サンプル記事での展開後プロンプト</div>
            <pre className="whitespace-pre-wrap break-words bg-black/60 text-fg font-mono text-[11px] p-3 rounded max-h-96 overflow-y-auto">
              {preview?.rendered_sample ?? ""}
            </pre>
          </div>
        </div>
      </details>

      {/* LLM テスト */}
      <div className="bg-surface-1 border border-border-subtle rounded-lg p-4 space-y-2">
        <h4 className="m-0 text-sm font-semibold text-fg">LLM で 1 件テスト</h4>
        <div className="flex items-center gap-2 flex-wrap">
          <button
            onClick={() => testMut.mutate()}
            disabled={testMut.isPending}
            className="px-3 py-1.5 rounded text-xs font-medium bg-accent text-bg hover:bg-accent-hover disabled:opacity-40"
          >
            {testMut.isPending ? `テスト中... (${elapsedSec}秒)` : "LLM で 1 件テスト"}
          </button>
          <input
            value={testArticleId}
            onChange={(e) => setTestArticleId(e.target.value)}
            placeholder="記事ID (任意、未指定は固定サンプル記事)"
            className="flex-1 min-w-[200px] rounded border border-border-subtle bg-surface-2 px-2 py-1 text-xs font-mono text-fg"
          />
        </div>
        {testMut.data && testMut.data.ok && (
          <div className="text-xs text-fg-muted space-y-1">
            <div className="tnum">
              モデル: {testMut.data.model} / {(testMut.data.elapsed_ms / 1000).toFixed(1)} 秒 / プロンプト{" "}
              {testMut.data.prompt_chars.toLocaleString()} 字
            </div>
            {testMut.data.empty_fields.length > 0 && (
              <div className="text-warning">空だったフィールド: {testMut.data.empty_fields.join(", ")}</div>
            )}
            <pre className="whitespace-pre-wrap break-words bg-black/60 text-fg font-mono text-[11px] p-3 rounded max-h-64 overflow-y-auto">
              {JSON.stringify(testMut.data.output, null, 2)}
            </pre>
          </div>
        )}
        {testMut.data && !testMut.data.ok && (
          <div className="text-xs text-critical bg-critical-soft border border-critical rounded px-2 py-1">
            失敗: {testMut.data.error}
          </div>
        )}
        {testMut.isError && (
          <div className="text-xs text-critical">{(testMut.error as Error).message}</div>
        )}
      </div>
    </div>
  );
}
