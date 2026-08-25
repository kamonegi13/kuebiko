/**
 * 本文の節組み。**誤ると記事が壊れる**ので、実測で起きた形を全部固定する。
 */
import { describe, expect, it } from "vitest";
import { buildSections, SECTION_ORDER, type SectionFact } from "./sections";

const f = (text: string, paragraph: number, section?: string, source_index = 1): SectionFact => ({
  text,
  paragraph,
  section,
  source_index,
});

describe("buildSections", () => {
  it("節を正規の並び順で 1 回ずつ出す", () => {
    // 実測で LLM は what → response → what と往復した。見出しが 2 回出てはいけない
    const out = buildSections([
      f("起きた", 1, "what"),
      f("対応した", 2, "response"),
      f("起きた続き", 3, "what"),
    ]);
    expect(out.map((s) => s.key)).toEqual(["what", "response"]);
    expect(out[0].paragraphs.length).toBe(2); // 段落 1 と 3 が what にまとまる
  });

  it("節の並びは書かれた順ではなく正規の順", () => {
    const out = buildSections([f("対応", 1, "response"), f("起きた", 2, "what")]);
    expect(out.map((s) => s.key)).toEqual(["what", "response"]);
  });

  it("1 段落の節は多数決で決める (段落の途中で節が変わっても割れない)", () => {
    const out = buildSections([
      f("a", 1, "scope"),
      f("b", 1, "scope"),
      f("c", 1, "how"), // 少数派
    ]);
    expect(out.map((s) => s.key)).toEqual(["scope"]);
    expect(out[0].paragraphs[0]).toHaveLength(3); // 段落は割らない
  });

  it("段落の中の文の順序を保つ", () => {
    const out = buildSections([f("一", 1, "what"), f("二", 1, "what"), f("三", 1, "what")]);
    expect(out[0].paragraphs[0].map((x) => x.text)).toEqual(["一", "二", "三"]);
  });

  it("段落は番号順に並べる", () => {
    const out = buildSections([f("後", 3, "what"), f("前", 1, "what")]);
    expect(out[0].paragraphs.map((p) => p[0].text)).toEqual(["前", "後"]);
  });

  it("section を持たない記事 (v4 より前) は空を返す", () => {
    // 呼び手は段落だけで描く。既存 530 件を作り直さなくても壊れない
    expect(buildSections([f("旧", 1), f("記事", 2)])).toEqual([]);
  });

  it("語彙外の section は節を作らない (勝手な見出しを出さない)", () => {
    const out = buildSections([f("正", 1, "what"), f("謎", 2, "nonsense")]);
    expect(out.map((s) => s.key)).toEqual(["what"]);
    // 語彙外の段落は既定 (what) に寄せて落とさない — 本文を失わないこと
    expect(out[0].paragraphs.length).toBe(2);
  });

  it("事実行を 1 つも落とさない", () => {
    const facts = [
      f("a", 1, "what"),
      f("b", 2, "scope"),
      f("c", 2, "scope"),
      f("d", 3, "action"),
    ];
    const kept = buildSections(facts).flatMap((s) => s.paragraphs.flat());
    expect(kept).toHaveLength(facts.length);
  });

  it("SECTION_ORDER は backend の SECTION_KEYS と同じ 6 つ", () => {
    expect([...SECTION_ORDER]).toEqual(["what", "scope", "how", "response", "context", "action"]);
  });
});
