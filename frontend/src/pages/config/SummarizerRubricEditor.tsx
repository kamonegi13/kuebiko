// summarizer プロンプトの層分け (WP3) → 判定基準エディタ UI 再設計 (WP-F5): 記事要約・翻訳
// プロンプトの構造化エディタ。判定基準 (rubric) は DB (config_store, key=summarizer_rubric)
// が SSoT — legacy の prompts/briefing/summarizer.j2 は rollback 用の据置コピー
// (I8: このエディタから .j2 へ書き戻すコードは書かない)。
//
// 再設計の要旨 (設計書): 24 フィールドを縦に並べるだけの旧版から、
// 「ある記事の判定がおかしい → 原因の基準を見つける → 直す → 効果を確かめる」ループを
// 支える画面へ。目次 (グループ+検索) / グループ化 / 実データ統計 / issue 局在化を追加。
//
// I-1 (最重要): 保存ペイロードの sections 配列順は GET で得た順序を保持する。
// 表示順 (グループ化・並べ替え) は描画時のみで、draft.sections 自体は一切並べ替えない
// (useRubricDraft の各 update/revert はすべて同一 index への書き戻し)。

import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Save } from "lucide-react";
import {
  promptRubricApi,
  type ContractField,
  type RubricFieldGuide,
  type RubricGroupId,
  type RubricSection,
  type StatsDays,
} from "../../api/promptRubric";
import { useVocabOptions } from "../../hooks/useVocab";
import { useRubricDraft } from "./rubric/useRubricDraft";
import { buildIssueIndex, countSeverity, issuesForExample, issuesForSection, sumIssueCounts } from "./rubric/issues";
import { changedExampleIndices, changedFieldIds, matchesQuery, promptOrderMap } from "./rubric/format";
import { RubricSectionCard } from "./rubric/RubricSectionCard";
import { RubricExampleCard } from "./rubric/RubricExampleCard";
import { RubricGroupSection } from "./rubric/RubricGroupSection";
import { RubricToc, type TocGroup } from "./rubric/RubricToc";
import { RubricIssueBanner } from "./rubric/RubricIssueBanner";

const SUMMARIZER_PATH = "prompts/briefing/summarizer.j2";

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

  const [statsDays, setStatsDays] = useState<StatsDays>(30);
  const [query, setQuery] = useState("");
  const [openIds, setOpenIds] = useState<Set<string>>(new Set());
  const [jumpTarget, setJumpTarget] = useState<string | null>(null);
  const autoOpenedRef = useRef(false);
  const [testArticleId, setTestArticleId] = useState("");
  const [elapsedSec, setElapsedSec] = useState(0);
  const [saveMessage, setSaveMessage] = useState<{ kind: "success" | "error"; text: string } | null>(null);
  const [saveWarnings, setSaveWarnings] = useState<string[]>([]);

  const {
    draft,
    dirty,
    duplicateFieldIds,
    updateIntro,
    updateSectionBody,
    revertSection,
    removeSectionAt,
    updateExample,
    revertExample,
    preview,
    previewPending,
  } = useRubricDraft(data?.rubric);

  const statsQuery = useQuery({
    queryKey: ["summarizer-rubric-field-stats", statsDays],
    queryFn: () => promptRubricApi.getFieldStats(statsDays),
    staleTime: 5 * 60_000,
    gcTime: 10 * 60_000,
    refetchOnWindowFocus: false,
  });
  const fieldStats = statsQuery.data;

  const contractMap = useMemo(() => {
    const m = new Map<string, ContractField>();
    for (const f of data?.contract.fields ?? []) m.set(f.name, f);
    return m;
  }, [data]);

  const guideMap = useMemo(() => {
    const m = new Map<string, RubricFieldGuide>();
    for (const f of data?.guide.fields ?? []) m.set(f.field_id, f);
    return m;
  }, [data]);

  const groupDefsRaw = useVocabOptions("rubric_group");
  const groupDefs = useMemo(() => [...groupDefsRaw].sort((a, b) => a.order - b.order), [groupDefsRaw]);

  const groupOf = (fieldId: string): string => guideMap.get(fieldId)?.group ?? "other";

  const promptOrder = useMemo(() => (draft ? promptOrderMap(draft.sections) : new Map<string, number>()), [draft]);
  const changedIds = useMemo(
    () => (draft && data ? changedFieldIds(draft.sections, data.rubric.sections) : new Set<string>()),
    [draft, data],
  );
  const changedExampleIdx = useMemo(
    () => (draft && data ? changedExampleIndices(draft.examples, data.rubric.examples) : new Set<number>()),
    [draft, data],
  );
  const issueIndex = useMemo(() => buildIssueIndex(preview?.issues ?? []), [preview]);

  // 検索中は「一致したカードを開いて見せる」が、開閉状態そのもの (openIds) は書き換えない。
  // ⚠ この 2 つを混同すると、検索中に「すべて閉じる」を押しても無反応に見えるのに裏で
  // openIds だけ変わり、検索語を消した瞬間に全カードが畳まれる (レビュー M-1)。
  const isSearching = query.trim() !== "";
  const isFieldOpen = (fieldId: string): boolean => isSearching || openIds.has(`field:${fieldId}`);
  const isExampleOpen = (idx: number): boolean => openIds.has(`example:${idx + 1}`);

  // ⚠ 並べ替えは **描画専用**。draft.sections は GET で得た配列順のまま保存する
  // (この順序がプロンプトの節順になるため、崩すと LLM の判定が静かに変質する)。
  const byGuideOrder = (fieldId: string): number => guideMap.get(fieldId)?.order ?? Number.MAX_SAFE_INTEGER;
  const sortForDisplay = (sections: RubricSection[]): RubricSection[] =>
    [...sections].sort((a, b) => byGuideOrder(a.field_id) - byGuideOrder(b.field_id));

  // 語彙が取れないと groupDefs が空になり、カードが 1 枚も出ない画面になってしまう
  // (VocabGate は取得失敗でも先へ進む設計)。分類できなくても編集はできるべきなので、
  // 単一グループに全件を入れて描画を必ず成立させる (M-5)。
  const effectiveGroups = useMemo(
    () =>
      groupDefs.length > 0
        ? groupDefs
        : [{ value: "other", label: "判定基準", description: "分類の取得に失敗しました", order: 0 }],
    [groupDefs],
  );
  const groupFallback = groupDefs.length === 0;

  // fallback 時は分類できないので 1 グループに全件を入れる (取りこぼしを作らない)。
  const sectionsOfGroup = (groupId: string): RubricSection[] =>
    sortForDisplay(
      draft ? draft.sections.filter((s) => groupFallback || groupOf(s.field_id) === groupId) : [],
    );

  const grouped = useMemo(() => {
    if (!draft) return [];
    const q = query.trim();
    return effectiveGroups.map((g) => {
      const allInGroup = sectionsOfGroup(g.value);
      const sections = q === "" ? allInGroup : allInGroup.filter((s) => matchesQuery(q, s, g.label));
      return {
        id: g.value as RubricGroupId,
        label: g.label,
        description: g.description ?? "",
        sections,
      };
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draft, effectiveGroups, guideMap, query]);

  const tocGroups = useMemo<TocGroup[]>(() => {
    if (!draft) return [];
    return effectiveGroups.map((g) => {
      const sections = sectionsOfGroup(g.value);
      const fields = sections.map((s) => {
        const c = countSeverity(issuesForSection(issueIndex, s.field_id));
        return {
          fieldId: s.field_id,
          title: s.title,
          changed: changedIds.has(s.field_id),
          errorCount: c.errorCount,
          warnCount: c.warnCount,
        };
      });
      const totals = fields.reduce(
        (acc, f) => ({ errorCount: acc.errorCount + f.errorCount, warnCount: acc.warnCount + f.warnCount }),
        { errorCount: 0, warnCount: 0 },
      );
      return { id: g.value as RubricGroupId, label: g.label, fields, ...totals };
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draft, effectiveGroups, guideMap, issueIndex, changedIds]);

  const exampleIssueTotals = useMemo(() => {
    if (!draft) return { errorCount: 0, warnCount: 0 };
    return sumIssueCounts(draft.examples.map((_ex, i) => issuesForExample(issueIndex, i)));
  }, [draft, issueIndex]);

  // カード自動展開は初回 preview のみ (issue のあるカードを見つけやすくする)。
  // 編集中に暴発させないよう ref で 1 回だけに制限する。
  useEffect(() => {
    if (!preview || autoOpenedRef.current) return;
    autoOpenedRef.current = true;
    if (preview.issues.length === 0) return;
    setOpenIds((prev) => {
      const next = new Set(prev);
      for (const issue of preview.issues) {
        if (issue.field_id) next.add(`field:${issue.field_id}`);
        else if (issue.example_index > 0) next.add(`example:${issue.example_index}`);
      }
      return next;
    });
  }, [preview]);

  useEffect(() => {
    if (!jumpTarget) return;
    const el = document.getElementById(jumpTarget);
    if (!el) {
      // 目次は常に全件を出すので、検索で絞られたカードへも飛べてしまう。要素が無いときは
      // 検索を解除して次の描画で再試行する (押しても何も起きないボタンを残さない)。
      if (isSearching) {
        setQuery("");
        return; // jumpTarget は保持 — 解除後の再描画で改めて探す
      }
      setJumpTarget(null);
      return;
    }
    el.scrollIntoView({ behavior: "smooth", block: "start" });
    setJumpTarget(null);
  }, [jumpTarget, isSearching]);

  // ⚠ 検索中は一致カードを強制展開しているので、開閉操作は「効いていないのに裏で
  // 状態だけ変わる」ことになる。検索を先に解除してから畳む (M-1)。
  const toggleAnchor = (anchorId: string) => {
    if (isSearching && anchorId.startsWith("field:")) {
      setQuery("");
      return;
    }
    setOpenIds((prev) => {
      const next = new Set(prev);
      if (next.has(anchorId)) next.delete(anchorId);
      else next.add(anchorId);
      return next;
    });
  };

  const onJump = (anchorId: string) => {
    setOpenIds((prev) => (prev.has(anchorId) ? prev : new Set(prev).add(anchorId)));
    setJumpTarget(anchorId);
  };

  const onExpandAll = () => {
    if (!draft) return;
    const all = new Set<string>();
    for (const s of draft.sections) all.add(`field:${s.field_id}`);
    draft.examples.forEach((_ex, i) => all.add(`example:${i + 1}`));
    setOpenIds(all);
  };
  const onCollapseAll = () => {
    setQuery(""); // 検索の強制展開が残っていると「閉じたのに閉じない」ように見える
    setOpenIds(new Set());
  };

  const saveMut = useMutation({
    mutationFn: (rubric: NonNullable<typeof draft>) => promptRubricApi.saveRubric(rubric),
    onSuccess: (res) => {
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

  const handleSave = () => {
    if (!draft || !data) return;
    const introChanged = draft.intro !== data.rubric.intro ? 1 : 0;
    const changedCount = changedIds.size + changedExampleIdx.size + introChanged;
    if (!window.confirm(`${changedCount} 件の判定基準を変更します。次回の実行から反映されます。よろしいですか?`)) return;
    saveMut.mutate(draft);
  };

  const valid = preview?.valid ?? false;

  if (isLoading) return <div className="text-sm text-fg-muted">読み込み中...</div>;
  if (isError || !data || !draft) {
    return (
      <div className="rounded-lg border border-critical bg-critical-soft p-4 text-sm text-critical">
        判定基準の取得に失敗しました{error instanceof Error ? `: ${error.message}` : ""}
      </div>
    );
  }

  const renderCard = (section: RubricSection) => {
    const fieldId = section.field_id;
    // 表示順ではなく draft.sections 上の位置 (I-1: 配列順は不変なので identity で引ける)。
    // 重複カードの削除は index 指定でしか成立しない (field_id 指定では両方消える)。
    const sectionIndex = draft.sections.indexOf(section);
    const duplicate = duplicateFieldIds.has(fieldId);
    return (
      <RubricSectionCard
        key={`${fieldId}:${sectionIndex}`}
        section={section}
        contract={contractMap.get(fieldId)}
        guide={guideMap.get(fieldId)}
        stat={fieldStats?.fields[fieldId]}
        statsDays={statsDays}
        statsLoading={statsQuery.isLoading}
        promptOrder={promptOrder.get(fieldId) ?? null}
        issues={issuesForSection(issueIndex, fieldId)}
        changed={changedIds.has(fieldId)}
        open={isFieldOpen(fieldId)}
        duplicate={duplicate}
        onToggle={() => toggleAnchor(`field:${fieldId}`)}
        onChangeBody={(body) => updateSectionBody(fieldId, body)}
        onRevert={() => revertSection(fieldId)}
        onRemove={() => removeSectionAt(sectionIndex)}
      />
    );
  };

  return (
    <div className="space-y-4">
      {data.runtime.active_source === "legacy_file" && (
        <div className="inline-flex items-start gap-1.5 rounded-lg border border-critical bg-critical-soft p-3 text-sm text-critical">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
          <span>
            現在この基準は使われていません (理由: {data.runtime.fallback_reason || "不明"})。
            legacy ファイルがそのまま LLM に渡されています。
          </span>
        </div>
      )}

      {/* ヘッダ行 */}
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h3 className="m-0 text-md font-semibold text-fg">記事要約・翻訳 (summarizer)</h3>
          <p className="m-0 text-[11px] text-fg-subtle">
            有効な基準:{" "}
            {data.runtime.active_source === "composed" ? `DB v${data.runtime.version ?? "?"}` : "legacy ファイル"}
            {data.runtime.saved_at && ` (${formatTimestamp(data.runtime.saved_at)})`}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {previewPending && <span className="text-[11px] italic text-fg-subtle">検証中...</span>}
          {saveMessage && (
            <span
              className={`rounded px-2 py-0.5 text-xs ${
                saveMessage.kind === "success" ? "bg-success-soft text-success" : "bg-critical-soft text-critical"
              }`}
            >
              {saveMessage.text}
            </span>
          )}
          <button
            onClick={onSwitchToRaw}
            className="rounded border border-border-subtle px-3 py-1.5 text-xs font-medium text-fg-muted hover:bg-surface-2 hover:text-fg"
          >
            raw (.j2) を表示
          </button>
          <button
            onClick={handleSave}
            disabled={!dirty || !valid || saveMut.isPending}
            className="inline-flex items-center gap-1 rounded-md bg-accent px-4 py-1.5 text-sm font-semibold text-bg transition-colors hover:bg-accent-hover disabled:opacity-40"
          >
            {saveMut.isPending ? (
              "保存中..."
            ) : (
              <>
                <Save className="h-4 w-4" /> 保存
              </>
            )}
          </button>
        </div>
      </div>

      {/* プロンプト長バジェット */}
      {preview && (
        <div className="space-y-1">
          <div className="flex items-center justify-between text-[11px] tnum text-fg-subtle">
            <span>プロンプト長</span>
            <span>
              {preview.composed_chars.toLocaleString()} / legacy {preview.legacy_chars.toLocaleString()} 字
              {preview.composed_chars > preview.legacy_chars
                ? `（超過 ${(preview.composed_chars - preview.legacy_chars).toLocaleString()}）`
                : `（残り ${(preview.legacy_chars - preview.composed_chars).toLocaleString()}）`}
            </span>
          </div>
          <div className="h-1.5 overflow-hidden rounded-full bg-surface-3">
            <div
              className={`h-full ${preview.composed_chars > preview.legacy_chars ? "bg-critical" : "bg-accent"}`}
              style={{
                width: `${Math.min(100, (preview.composed_chars / Math.max(preview.legacy_chars, 1)) * 100)}%`,
              }}
            />
          </div>
        </div>
      )}

      {/* 分類が未登録のフィールド (出力スキーマに足したが FIELD_GUIDE に無い)。
          「その他」に落ちて表示自体は残るが、効く先の説明が出ないので気付けるようにする。 */}
      {data.guide.unmapped_fields.length > 0 && (
        <div className="rounded border border-warning bg-warning-soft px-2 py-1 text-xs text-warning">
          分類が未登録のフィールドがあります ({data.guide.unmapped_fields.join(", ")})。
          コード側の判定基準ガイド (src/prompts/rubric_guide.py) に追加すると、
          グループと「効く先」の説明が出るようになります。
        </div>
      )}

      {/* 検証結果 (issue 局在化バナー) */}
      <RubricIssueBanner issues={preview?.issues ?? []} onJump={onJump} />
      {saveWarnings.length > 0 && (
        <div className="space-y-1">
          {saveWarnings.map((w, i) => (
            <div key={i} className="rounded border border-warning bg-warning-soft px-2 py-1 text-xs text-warning">
              保存時の注意: {w}
            </div>
          ))}
        </div>
      )}

      {/* 目次 + 本文 の 2 カラム */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-[220px_1fr]">
        {/* 目次と注記をまとめて 1 つの sticky 単位にする。目次だけを sticky にすると
            スクロール時に注記へ重なる。項目数が多く画面高を超えるので自前でスクロールさせる。 */}
        <aside className="space-y-3 lg:sticky lg:top-4 lg:max-h-[calc(100vh-6rem)] lg:overflow-y-auto lg:pr-1">
          <RubricToc
            groups={tocGroups}
            exampleCount={draft.examples.length}
            exampleErrorCount={exampleIssueTotals.errorCount}
            exampleWarnCount={exampleIssueTotals.warnCount}
            query={query}
            onQueryChange={setQuery}
            statsDays={statsDays}
            onStatsDaysChange={setStatsDays}
            onJump={onJump}
            onExpandAll={onExpandAll}
            onCollapseAll={onCollapseAll}
          />
          {fieldStats && (
            <p className="m-0 text-[10.5px] leading-snug text-fg-subtle">
              分母: {fieldStats.denominator.label} {fieldStats.denominator.count.toLocaleString()} 件（
              {fieldStats.window.since.slice(0, 10)} 〜 {fieldStats.window.until.slice(0, 10)}）。
              {fieldStats.denominator.note}
            </p>
          )}
        </aside>

        <div className="min-w-0 space-y-4">
          {/* 導入文 */}
          <div className="space-y-1.5 rounded-lg border border-border-subtle bg-surface-1 p-4">
            <label className="block text-xs text-fg-subtle">導入文 (intro)</label>
            <textarea
              value={draft.intro}
              onChange={(e) => updateIntro(e.target.value)}
              rows={3}
              spellCheck={false}
              className="w-full rounded border border-border-subtle bg-surface-2 px-2 py-1.5 font-mono text-sm leading-relaxed text-fg"
            />
          </div>

          {/* グループ化された判定基準カード */}
          {grouped.map((g) => {
            const groupIssues = sumIssueCounts(g.sections.map((s) => issuesForSection(issueIndex, s.field_id)));
            return (
              <RubricGroupSection
                key={g.id}
                groupId={g.id}
                label={g.label}
                description={g.description}
                sections={g.sections}
                isOpen={isFieldOpen}
                onToggleGroup={(open) => {
                  setOpenIds((prev) => {
                    const next = new Set(prev);
                    for (const s of g.sections) {
                      const anchor = `field:${s.field_id}`;
                      if (open) next.add(anchor);
                      else next.delete(anchor);
                    }
                    return next;
                  });
                }}
                renderCard={renderCard}
                errorCount={groupIssues.errorCount}
                warnCount={groupIssues.warnCount}
              />
            );
          })}

          {/* 出力例 */}
          {draft.examples.length > 0 && (
            <div id="examples" className="scroll-mt-24 space-y-3">
              <h4 className="m-0 text-sm font-semibold text-fg">出力例</h4>
              {draft.examples.map((ex, i) => (
                <RubricExampleCard
                  key={i}
                  index={i}
                  example={ex}
                  issues={issuesForExample(issueIndex, i)}
                  changed={changedExampleIdx.has(i)}
                  open={isExampleOpen(i)}
                  onToggle={() => toggleAnchor(`example:${i + 1}`)}
                  onChange={(patch) => updateExample(i, patch)}
                  onRevert={() => revertExample(i)}
                />
              ))}
            </div>
          )}
        </div>
      </div>

      {/* 合成プレビュー / 展開後プロンプト */}
      <details className="rounded-lg border border-border-subtle bg-surface-1">
        <summary className="cursor-pointer select-none px-4 py-2 text-sm text-fg-muted">合成プレビュー</summary>
        <div className="space-y-3 px-4 pb-3">
          <div>
            <div className="mb-1 text-[11px] text-fg-subtle">合成後テンプレート (Jinja タグ未展開)</div>
            <pre className="max-h-96 overflow-y-auto whitespace-pre-wrap break-words rounded bg-black/60 p-3 font-mono text-[11px] text-fg">
              {preview?.composed ?? ""}
            </pre>
          </div>
          <div>
            <div className="mb-1 text-[11px] text-fg-subtle">サンプル記事での展開後プロンプト</div>
            <pre className="max-h-96 overflow-y-auto whitespace-pre-wrap break-words rounded bg-black/60 p-3 font-mono text-[11px] text-fg">
              {preview?.rendered_sample ?? ""}
            </pre>
          </div>
        </div>
      </details>

      {/* LLM テスト */}
      <div className="space-y-2 rounded-lg border border-border-subtle bg-surface-1 p-4">
        <h4 className="m-0 text-sm font-semibold text-fg">LLM で 1 件テスト</h4>
        <div className="flex flex-wrap items-center gap-2">
          <button
            onClick={() => testMut.mutate()}
            disabled={testMut.isPending}
            className="rounded bg-accent px-3 py-1.5 text-xs font-medium text-bg hover:bg-accent-hover disabled:opacity-40"
          >
            {testMut.isPending ? `テスト中... (${elapsedSec}秒)` : "LLM で 1 件テスト"}
          </button>
          <input
            value={testArticleId}
            onChange={(e) => setTestArticleId(e.target.value)}
            placeholder="記事ID (任意、未指定は固定サンプル記事)"
            className="min-w-[200px] flex-1 rounded border border-border-subtle bg-surface-2 px-2 py-1 font-mono text-xs text-fg"
          />
        </div>
        {testMut.data && testMut.data.ok && (
          <div className="space-y-1 text-xs text-fg-muted">
            <div className="tnum">
              モデル: {testMut.data.model} / {(testMut.data.elapsed_ms / 1000).toFixed(1)} 秒 / プロンプト{" "}
              {testMut.data.prompt_chars.toLocaleString()} 字
            </div>
            {testMut.data.empty_fields.length > 0 && (
              <div className="text-warning">空だったフィールド: {testMut.data.empty_fields.join(", ")}</div>
            )}
            <pre className="max-h-64 overflow-y-auto whitespace-pre-wrap break-words rounded bg-black/60 p-3 font-mono text-[11px] text-fg">
              {JSON.stringify(testMut.data.output, null, 2)}
            </pre>
          </div>
        )}
        {testMut.data && !testMut.data.ok && (
          <div className="rounded border border-critical bg-critical-soft px-2 py-1 text-xs text-critical">
            失敗: {testMut.data.error}
          </div>
        )}
        {testMut.isError && <div className="text-xs text-critical">{(testMut.error as Error).message}</div>}
      </div>
    </div>
  );
}
