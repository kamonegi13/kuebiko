// block 方式プロンプト (status_synthesis 以降) 編集の draft 状態・保存前検証 (preview) の
// 600ms デバウンスを束ねる hook。
//
// summarizer の `frontend/src/pages/config/rubric/useRubricDraft.ts` と同じ設計原則を踏襲する
// (dirty は独立 state を持たず draft と保存済み値の差分から都度導出する / 更新・取消は
// field_id (= block id) をキーに同一 index への書き戻しのみ行い配列順を変えない / 重複
// field_id は編集不可で index 指定の削除だけ許す)。**あえて独立実装にした** — 共通化しなかった
// 理由:
//   - preview/save エンドポイントが prompt_id ごとに変わる (summarizer は固定パス)
//   - block 方式に few-shot examples は無い (backend `validate_blocks` が非空を拒否する) ので
//     examples 系の update/revert を持たない
//   - block 方式は intro を使わない (block_composer.compose_blocks は skeleton marker 置換
//     のみで intro を一切参照しない) ので intro 編集も持たない — 書いても効かない
//     write-only な入力欄を UI に置かない判断 (CLAUDE.md の guide.editable=false と同じ配慮)
// 対応表 (同名同義): updateSectionBody→updateBlockBody / revertSection→revertBlock /
// removeSectionAt→removeBlockAt / duplicateFieldIds→duplicateBlockIds。
// 差分検出は同じ `RubricSection[]` 形なので `changedFieldIds` をそのまま再利用する (DRY)。

import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { promptBlocksApi, type BlockRubric, type BlocksPreviewResult } from "../../../api/promptBlocks";
import { changedFieldIds } from "../rubric/format";

const PREVIEW_DEBOUNCE_MS = 600;

interface UseBlockDraftResult {
  draft: BlockRubric | null;
  dirty: boolean;
  /** 2 枚以上のカードが同じ block id を持っている場合のその id 集合 (通常は空)。 */
  duplicateBlockIds: Set<string>;
  updateBlockBody: (blockId: string, body: string) => void;
  revertBlock: (blockId: string) => void;
  removeBlockAt: (index: number) => void;
  preview: BlocksPreviewResult | undefined;
  previewPending: boolean;
}

export function useBlockDraft(promptId: string, saved: BlockRubric | undefined): UseBlockDraftResult {
  const [draft, setDraft] = useState<BlockRubric | null>(null);

  // GET 到着で 1 回だけ初期化する。以後 saved が更新されても (保存成功後の invalidate 等)
  // draft を勝手に上書きしない — 上書きするとユーザの未保存編集が消える。
  useEffect(() => {
    if (saved && draft === null) setDraft(saved);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [saved, draft]);

  // prompt_id が切り替わったら (別プロンプトを選び直した) draft を作り直す。
  const promptIdRef = useRef(promptId);
  useEffect(() => {
    if (promptIdRef.current !== promptId) {
      promptIdRef.current = promptId;
      setDraft(null);
    }
  }, [promptId]);

  // ⚠ useMutation は「最新勝ち」を保証しない。デバウンスより応答が遅れると古い検証結果が
  // 後着して上書きしてしまう。世代番号で後着した古い応答を捨てる (summarizer と同じ対策)。
  const previewGenRef = useRef(0);
  const [preview, setPreview] = useState<BlocksPreviewResult | undefined>(undefined);

  const previewMut = useMutation({
    mutationFn: async (rubric: BlockRubric) => {
      const gen = ++previewGenRef.current;
      const result = await promptBlocksApi.previewBlocks(promptId, rubric);
      return { gen, result };
    },
    onSuccess: ({ gen, result }) => {
      if (gen === previewGenRef.current) setPreview(result);
    },
  });

  useEffect(() => {
    if (!draft) return;
    const t = setTimeout(() => previewMut.mutate(draft), PREVIEW_DEBOUNCE_MS);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draft]);

  const dirty = useMemo(() => {
    if (!draft || !saved) return false;
    return changedFieldIds(draft.sections, saved.sections).size > 0;
  }, [draft, saved]);

  // 同じ field_id (block id) のカードが 2 枚あると、更新キーが両方に当たって相互汚染する。
  // backend も保存を拒否する (block id が重複しています) ため、画面に修復手段が無いと詰む。
  const duplicateBlockIds = useMemo(() => {
    const seen = new Set<string>();
    const dup = new Set<string>();
    for (const s of draft?.sections ?? []) {
      if (seen.has(s.field_id)) dup.add(s.field_id);
      seen.add(s.field_id);
    }
    return dup;
  }, [draft]);

  // 一意な block id は削除させない (skeleton の slot と 1:1 の不変量があり、消すと
  // 「skeleton の slot に対応する block がありません」でどのみち保存できなくなる)。
  // 削除は field_id ではなく index 指定 (field_id 指定では両方消える)。
  const removeBlockAt = (index: number) => {
    if (!draft) return;
    const target = draft.sections[index];
    if (!target || !duplicateBlockIds.has(target.field_id)) return;
    setDraft({ ...draft, sections: draft.sections.filter((_s, i) => i !== index) });
  };

  // I-1 (summarizer と同じ不変量): sections.map で同一 index に書き戻すだけ。
  // 並べ替え・フィルタは絶対にしない — 表示順 (slots 順) は呼び出し側の描画時のみ。
  const updateBlockBody = (blockId: string, body: string) => {
    if (!draft) return;
    setDraft({
      ...draft,
      sections: draft.sections.map((s) => (s.field_id === blockId ? { ...s, body } : s)),
    });
  };

  const revertBlock = (blockId: string) => {
    if (!draft || !saved) return;
    const original = saved.sections.find((s) => s.field_id === blockId);
    if (!original) return;
    setDraft({
      ...draft,
      sections: draft.sections.map((s) => (s.field_id === blockId ? original : s)),
    });
  };

  return {
    draft,
    dirty,
    duplicateBlockIds,
    updateBlockBody,
    revertBlock,
    removeBlockAt,
    preview,
    previewPending: previewMut.isPending,
  };
}
