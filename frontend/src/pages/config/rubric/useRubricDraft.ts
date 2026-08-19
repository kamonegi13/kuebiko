// summarizer 判定基準エディタ UI 再設計 (WP-F2): draft 状態・保存前検証 (preview) の
// 600ms デバウンスを束ねる hook。
//
// dirty は独立した state を持たず、draft と保存済み値の差分から都度導出する
// (「二重の真実を作らない」— 別 state で持つと、カード単位の取消 (revert) で全項目を
// 元に戻したのに dirty=true のまま残る、といったバグを生む)。
// I-1: セクション/出力例の更新・取消はすべて同一 index への書き戻しのみ行い、配列順を変えない。

import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import {
  promptRubricApi,
  type PreviewResult,
  type RubricExample,
  type SummarizerRubric,
} from "../../../api/promptRubric";
import { changedExampleIndices, changedFieldIds } from "./format";

const PREVIEW_DEBOUNCE_MS = 600;

interface UseRubricDraftResult {
  draft: SummarizerRubric | null;
  dirty: boolean;
  /** 2 枚以上のカードが同じ field_id を持っている場合のその id 集合 (通常は空)。 */
  duplicateFieldIds: Set<string>;
  updateIntro: (intro: string) => void;
  updateSectionBody: (fieldId: string, body: string) => void;
  revertSection: (fieldId: string) => void;
  removeSectionAt: (index: number) => void;
  updateExample: (idx: number, patch: Partial<RubricExample>) => void;
  revertExample: (idx: number) => void;
  preview: PreviewResult | undefined;
  previewPending: boolean;
}

export function useRubricDraft(saved: SummarizerRubric | undefined): UseRubricDraftResult {
  const [draft, setDraft] = useState<SummarizerRubric | null>(null);

  // GET 到着で 1 回だけ初期化する (既存ロジック維持)。以後 saved が更新されても
  // (例: 保存成功後の invalidate → 再取得) draft を勝手に上書きしない — 上書きすると
  // ユーザの未保存編集が消える。
  useEffect(() => {
    if (saved && draft === null) setDraft(saved);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [saved, draft]);

  // ⚠ useMutation は useQuery と違い「最新勝ち」を保証しない。デバウンス (600ms) より
  // 応答が遅れると古い検証結果が後着して上書きし、直したはずの指摘がバナーに残る /
  // 保存ボタンの活性が古い判定になる。世代番号で後着した古い応答を捨てる。
  const previewGenRef = useRef(0);
  const [preview, setPreview] = useState<PreviewResult | undefined>(undefined);

  const previewMut = useMutation({
    mutationFn: async (rubric: SummarizerRubric) => {
      const gen = ++previewGenRef.current;
      const result = await promptRubricApi.previewRubric(rubric);
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
    if (draft.intro !== saved.intro) return true;
    if (changedFieldIds(draft.sections, saved.sections).size > 0) return true;
    if (changedExampleIndices(draft.examples, saved.examples).size > 0) return true;
    return false;
  }, [draft, saved]);

  // 同じ field_id のカードが 2 枚あると、更新キー (field_id) が両方に当たって相互汚染する。
  // この状態は backend が保存を拒否する (duplicate_field_id) ため、画面に修復手段が無いと
  // 詰む (旧 UI の未対応項目)。編集は止めて、index 指定の削除だけを許す。
  const duplicateFieldIds = useMemo(() => {
    const seen = new Set<string>();
    const dup = new Set<string>();
    for (const s of draft?.sections ?? []) {
      if (seen.has(s.field_id)) dup.add(s.field_id);
      seen.add(s.field_id);
    }
    return dup;
  }, [draft]);

  // ⚠ 一意な field_id は削除させない。sections の集合は SummaryOutput の model_fields と
  // 一致する不変量があり、消すと missing_fields でどのみち保存できなくなる。
  // 削除は field_id ではなく **index** 指定 (field_id 指定では両方消える)。
  const removeSectionAt = (index: number) => {
    if (!draft) return;
    const target = draft.sections[index];
    if (!target || !duplicateFieldIds.has(target.field_id)) return;
    setDraft({ ...draft, sections: draft.sections.filter((_s, i) => i !== index) });
  };

  const updateIntro = (intro: string) => {
    if (!draft) return;
    setDraft({ ...draft, intro });
  };

  // I-1: sections.map で同一 index に書き戻すだけ。並べ替え・フィルタは絶対にしない。
  const updateSectionBody = (fieldId: string, body: string) => {
    if (!draft) return;
    setDraft({
      ...draft,
      sections: draft.sections.map((s) => (s.field_id === fieldId ? { ...s, body } : s)),
    });
  };

  const revertSection = (fieldId: string) => {
    if (!draft || !saved) return;
    const original = saved.sections.find((s) => s.field_id === fieldId);
    if (!original) return;
    setDraft({
      ...draft,
      sections: draft.sections.map((s) => (s.field_id === fieldId ? original : s)),
    });
  };

  const updateExample = (idx: number, patch: Partial<RubricExample>) => {
    if (!draft) return;
    setDraft({
      ...draft,
      examples: draft.examples.map((ex, i) => (i === idx ? { ...ex, ...patch } : ex)),
    });
  };

  const revertExample = (idx: number) => {
    if (!draft || !saved) return;
    const original = saved.examples[idx];
    if (!original) return;
    setDraft({
      ...draft,
      examples: draft.examples.map((ex, i) => (i === idx ? original : ex)),
    });
  };

  return {
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
    previewPending: previewMut.isPending,
  };
}
