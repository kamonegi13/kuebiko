// summarizer 判定基準エディタ UI 再設計 (WP-F2): 純ロジックのヘルパ群。
// I-5 (ラベル SSoT): vocab による値ラベルの解決はここでは一切行わない (FieldStatBlock.tsx
// に集約する)。このファイルは文字列整形・差分検出・並び順計算のみを持つ。

import type { RubricExample, RubricSection } from "../../../api/promptRubric";

const PREVIEW_MAX_CHARS = 60;

/** カード折りたたみヘッダに出す本文冒頭プレビュー (改行を空白に畳み、長さで省略)。 */
export function previewLine(body: string, maxChars: number = PREVIEW_MAX_CHARS): string {
  const flat = body.replace(/\s+/g, " ").trim();
  if (!flat) return "";
  return flat.length > maxChars ? `${flat.slice(0, maxChars)}…` : flat;
}

/**
 * draft.sections の body 非空セクションだけを対象に、compose() が実際に書き出す
 * プロンプト出力順 (1-based) を field_id → 順位 で返す。空セクションは対象外
 * (= 末尾フィールドの枯死を可視化するため promptOrder=null 相当)。
 * I-1: 配列を先頭から走査するだけで並べ替えは一切しない (表示順は呼び出し側の描画時のみ)。
 */
export function promptOrderMap(sections: readonly RubricSection[]): Map<string, number> {
  const map = new Map<string, number>();
  let order = 0;
  for (const s of sections) {
    if (s.body.trim().length === 0) continue;
    order += 1;
    map.set(s.field_id, order);
  }
  return map;
}

/** 検索一致判定: field_id / タイトル / 本文 / グループラベル のいずれかに部分一致 (小文字化)。 */
export function matchesQuery(
  query: string,
  section: Pick<RubricSection, "field_id" | "title" | "body">,
  groupLabel: string,
): boolean {
  const q = query.trim().toLowerCase();
  if (!q) return true;
  return (
    section.field_id.toLowerCase().includes(q) ||
    section.title.toLowerCase().includes(q) ||
    section.body.toLowerCase().includes(q) ||
    groupLabel.toLowerCase().includes(q)
  );
}

/**
 * draft と保存済み (react-query キャッシュ) の sections を突合し、body/title/note が
 * 変わった field_id の集合を返す。エディタは変更していないフィールドの section オブジェクト
 * 参照をそのまま使い回す (spread は変更対象の 1 件だけ) ため、大半のフィールドは参照一致の
 * 高速経路で判定できる。
 */
export function changedFieldIds(
  draftSections: readonly RubricSection[],
  savedSections: readonly RubricSection[],
): Set<string> {
  const savedMap = new Map(savedSections.map((s) => [s.field_id, s] as const));
  const out = new Set<string>();
  for (const d of draftSections) {
    const saved = savedMap.get(d.field_id);
    if (!saved) {
      out.add(d.field_id);
      continue;
    }
    if (saved === d) continue;
    if (saved.body !== d.body || saved.title !== d.title || saved.note !== d.note) {
      out.add(d.field_id);
    }
  }
  return out;
}

/** draft と保存済みの出力例を突合し、変更された index (0-based) の集合を返す。 */
export function changedExampleIndices(
  draftExamples: readonly RubricExample[],
  savedExamples: readonly RubricExample[],
): Set<number> {
  const out = new Set<number>();
  draftExamples.forEach((d, i) => {
    const saved = savedExamples[i];
    if (!saved) {
      out.add(i);
      return;
    }
    if (saved === d) return;
    if (saved.label !== d.label || saved.json_text !== d.json_text) out.add(i);
  });
  return out;
}

/** field-stats の drilldown_query をニュース一覧への URL に変換する。 */
export function newsUrl(drilldownQuery: string): string {
  return `/app/news?${drilldownQuery}`;
}
