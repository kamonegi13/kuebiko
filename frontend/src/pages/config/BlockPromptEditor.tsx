// プロンプト層分けの一般化 (2026-08-20): block 方式プロンプト (status_synthesis 以降) の
// カード編集 UI。SummarizerRubricEditor.tsx の簡略版 — 出さないもの:
//   - フィールド統計 (field-stats は summarizer 専用 API、block 方式に対応する集計が無い)
//   - contract バッジ (契約 = 出力スキーマの概念自体が block 方式には無い)
//   - guide (「効く先」— block 方式は 1 block = 1 段落で自明、専用ガイド未整備)
//   - 目次・検索・グループ化 (block 数が summarizer のフィールド数より少なく、slots 順の
//     単一列で足りる規模。将来 block 数が増えたら summarizer と同様に足す — YAGNI)
//   - LLM 1 件テスト (backend に対応する /test エンドポイントが無い)
//   - intro 編集 (block_composer.compose_blocks が intro を一切参照しないため)

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Info, Save } from "lucide-react";
import { promptBlocksApi } from "../../api/promptBlocks";
import { changedFieldIds } from "./rubric/format";
import { useBlockDraft } from "./blocks/useBlockDraft";
import { BlockCard } from "./blocks/BlockCard";

function formatTimestamp(iso: string): string {
  if (!iso) return "";
  return iso.slice(0, 16).replace("T", " ");
}

interface BlockPromptEditorProps {
  promptId: string;
}

// raw との切替は親 (PromptsEditor) のトグルに一本化 (2026-08-20 再設計)。
export function BlockPromptEditor({ promptId }: BlockPromptEditorProps) {
  const qc = useQueryClient();
  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["prompt-blocks", promptId],
    queryFn: () => promptBlocksApi.getBlocks(promptId),
  });

  const [openIds, setOpenIds] = useState<Set<string>>(new Set());
  const [saveMessage, setSaveMessage] = useState<{ kind: "success" | "error"; text: string } | null>(null);

  const { draft, dirty, duplicateBlockIds, updateBlockBody, revertBlock, removeBlockAt, preview, previewPending } =
    useBlockDraft(promptId, data?.rubric);

  // slots (skeleton のマーカー出現順) = カードの表示順。draft.sections (保存ペイロード)
  // 自体は GET 順のまま変えない — 並べ替えは描画専用 (I-1 と同じ不変量)。
  const slotOrder = useMemo(() => {
    const map = new Map<string, number>();
    (data?.slots ?? []).forEach((id, i) => map.set(id, i + 1));
    return map;
  }, [data]);

  const orderedBlocks = useMemo(() => {
    if (!draft) return [];
    return [...draft.sections].sort((a, b) => {
      const ao = slotOrder.get(a.field_id) ?? Number.MAX_SAFE_INTEGER;
      const bo = slotOrder.get(b.field_id) ?? Number.MAX_SAFE_INTEGER;
      return ao - bo;
    });
  }, [draft, slotOrder]);

  const changedIds = useMemo(
    () => (draft && data ? changedFieldIds(draft.sections, data.rubric.sections) : new Set<string>()),
    [draft, data],
  );

  const saveMut = useMutation({
    mutationFn: (rubric: NonNullable<typeof draft>) => promptBlocksApi.saveBlocks(promptId, rubric),
    onSuccess: (res) => {
      setSaveMessage({ kind: "success", text: `保存しました (v${res.version})` });
      qc.invalidateQueries({ queryKey: ["prompt-blocks", promptId] });
      qc.invalidateQueries({ queryKey: ["prompts-managed"] });
      setTimeout(() => setSaveMessage(null), 4000);
    },
    onError: (e: Error) => setSaveMessage({ kind: "error", text: e.message }),
  });

  const handleSave = () => {
    if (!draft) return;
    if (!window.confirm(`${changedIds.size} 件の block を変更します。次回の実行から反映されます。よろしいですか?`)) return;
    saveMut.mutate(draft);
  };

  const toggle = (blockId: string) => {
    setOpenIds((prev) => {
      const next = new Set(prev);
      if (next.has(blockId)) next.delete(blockId);
      else next.add(blockId);
      return next;
    });
  };

  const valid = preview?.valid ?? false;

  if (isLoading) return <div className="text-sm text-fg-muted">読み込み中...</div>;
  if (isError || !data || !draft) {
    return (
      <div className="rounded-lg border border-critical bg-critical-soft p-4 text-sm text-critical">
        プロンプトの取得に失敗しました{error instanceof Error ? `: ${error.message}` : ""}
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {data.runtime.active_source === "legacy_file" && (
        <div className="inline-flex items-start gap-1.5 rounded-lg border border-critical bg-critical-soft p-3 text-sm text-critical">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
          <span>
            現在この編集層は使われていません (理由: {data.runtime.fallback_reason || "不明"})。
            legacy ファイルがそのまま LLM に渡されています。
          </span>
        </div>
      )}

      {data.kind === "python_block" && (
        <div className="inline-flex items-start gap-1.5 rounded-lg border border-border-subtle bg-surface-2 p-3 text-sm text-fg-muted">
          <Info className="mt-0.5 h-4 w-4 shrink-0" />
          <span>
            このプロンプトは skeleton ファイル (.j2) を持たず、構造は Python コードが所有します
            (候補リストなどの動的な部分はコードが組み立てます)。「実ファイル」表示は無く、
            下の合成プレビューは代表的なサンプル候補での合成結果です。
          </span>
        </div>
      )}

      {/* ヘッダ行 */}
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h3 className="m-0 text-md font-semibold text-fg">
            {data.title} <span className="font-mono text-xs font-normal text-fg-subtle">{data.prompt_id}</span>
          </h3>
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

      {/* プロンプト長 */}
      {preview && (
        <p className="m-0 text-[11px] tnum text-fg-subtle">
          合成後プロンプト長: {preview.composed_chars.toLocaleString()} 字
        </p>
      )}

      {/* 検証結果 (block API はカード単位の局在化情報を持たない文字列 list なのでバナー表示) */}
      {preview && preview.errors.length > 0 && (
        <div className="space-y-1">
          {preview.errors.map((msg, i) => (
            <div
              key={i}
              className="flex items-center gap-2 rounded border border-critical bg-critical-soft px-2 py-1 text-xs text-critical"
            >
              <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
              <span>{msg}</span>
            </div>
          ))}
        </div>
      )}

      {/* block カード列挙 (slots 順) */}
      <div className="space-y-2">
        {orderedBlocks.map((b) => (
          <BlockCard
            key={b.field_id}
            block={b}
            order={slotOrder.get(b.field_id) ?? null}
            changed={changedIds.has(b.field_id)}
            duplicate={duplicateBlockIds.has(b.field_id)}
            open={openIds.has(b.field_id)}
            onToggle={() => toggle(b.field_id)}
            onChangeBody={(body) => updateBlockBody(b.field_id, body)}
            onRevert={() => revertBlock(b.field_id)}
            onRemove={() => removeBlockAt(draft.sections.indexOf(b))}
          />
        ))}
      </div>

      {/* 合成プレビュー / 展開後プロンプト (読み取り専用・折りたたみ) */}
      <details className="rounded-lg border border-border-subtle bg-surface-1">
        <summary className="cursor-pointer select-none px-4 py-2 text-sm text-fg-muted">合成プレビュー</summary>
        <div className="space-y-3 px-4 pb-3">
          <div>
            <div className="mb-1 text-[11px] text-fg-subtle">
              {data.kind === "python_block" ? "合成結果 (サンプル候補)" : "合成後テンプレート (Jinja タグ未展開)"}
            </div>
            <pre className="max-h-96 overflow-y-auto whitespace-pre-wrap break-words rounded bg-black/60 p-3 font-mono text-[11px] text-fg">
              {preview?.composed ?? ""}
            </pre>
          </div>
          {data.kind !== "python_block" && (
            <div>
              <div className="mb-1 text-[11px] text-fg-subtle">サンプル context での展開後プロンプト</div>
              <pre className="max-h-96 overflow-y-auto whitespace-pre-wrap break-words rounded bg-black/60 p-3 font-mono text-[11px] text-fg">
                {preview?.rendered_sample ?? ""}
              </pre>
            </div>
          )}
        </div>
      </details>
    </div>
  );
}
