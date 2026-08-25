// 本文の節。**key は backend (src/eventnews/models.py SECTION_KEYS) が決め、
// 表示名はここが持つ** — 自由記述の見出しを許すと表記が揺れるため。
//
// v4 より前に生成された記事は section を持たない。その場合は節見出しを出さず、
// 従来どおり段落だけで描く (既存 530 件を作り直さなくても壊れない)。

export const SECTION_ORDER = ["what", "scope", "how", "response", "context", "action"] as const;

const SECTION_LABELS: Record<string, string> = {
  what: "何が起きたか",
  scope: "影響範囲",
  how: "攻撃の手口",
  response: "対応・緩和",
  context: "背景・経緯",
  action: "利用者が取るべき対応",
};

export function sectionLabel(key: string): string {
  return SECTION_LABELS[key] ?? "";
}

export interface SectionFact {
  text: string;
  source_index: number;
  paragraph: number;
  section?: string;
}

export interface RenderedSection {
  key: string;
  label: string;
  /** 段落ごとの事実行 (段落は 1 つの <p> になる)。 */
  paragraphs: SectionFact[][];
}

/**
 * 事実行を「節 → 段落」に組む。
 *
 * - 節は **正規の並び順で 1 回ずつ**。LLM が節を往復しても見出しは重複しない
 *   (実測で `what → response → what` のように往復する出力があった)
 * - 1 段落の節は **その段落内の多数決**で決める (段落の途中で節が変わっても割れない)
 * - section を 1 つも持たない (v4 より前) なら空を返す → 呼び手は段落だけで描く
 */
export function buildSections(facts: SectionFact[]): RenderedSection[] {
  if (!facts.some((f) => f.section && SECTION_LABELS[f.section])) return [];

  const byParagraph = new Map<number, SectionFact[]>();
  for (const f of facts) {
    const list = byParagraph.get(f.paragraph);
    if (list) list.push(f);
    else byParagraph.set(f.paragraph, [f]);
  }

  const sectionOfParagraph = new Map<number, string>();
  for (const [paragraph, group] of byParagraph) {
    const counts = new Map<string, number>();
    for (const f of group) {
      if (f.section && SECTION_LABELS[f.section]) {
        counts.set(f.section, (counts.get(f.section) ?? 0) + 1);
      }
    }
    let best = "what";
    let top = 0;
    for (const [key, n] of counts) {
      if (n > top) {
        best = key;
        top = n;
      }
    }
    sectionOfParagraph.set(paragraph, best);
  }

  const out: RenderedSection[] = [];
  for (const key of SECTION_ORDER) {
    const paragraphs = [...byParagraph.keys()]
      .filter((p) => sectionOfParagraph.get(p) === key)
      .sort((a, b) => a - b)
      .map((p) => byParagraph.get(p) ?? []);
    if (paragraphs.length > 0) {
      out.push({ key, label: SECTION_LABELS[key], paragraphs });
    }
  }
  return out;
}
