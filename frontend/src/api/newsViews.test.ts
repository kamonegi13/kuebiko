import { describe, expect, it } from "vitest";
import { toWireFilters, fromWireFilters } from "./newsViews";
import { EMPTY_FILTERS, type NewsFilters } from "../components/news/views";

describe("toWireFilters / fromWireFilters", () => {
  it("既定値のみなら空オブジェクト (絞り込み無しを明示的に送らない)", () => {
    expect(toWireFilters(EMPTY_FILTERS)).toEqual({});
  });

  it("往復する (round-trip) — 設定した項目だけ復元される", () => {
    const f: Partial<NewsFilters> = {
      category: "vuln",
      minSeverity: "S2",
      relevantOnly: true,
      jp: "targeted_affected",
      minIndependentSources: true,
      hasNews: true,
      newFactsOnly: true,
    };
    const wire = toWireFilters(f);
    const back = fromWireFilters(wire);
    expect(back.category).toBe("vuln");
    expect(back.minSeverity).toBe("S2");
    expect(back.relevantOnly).toBe(true);
    // jp が入っているので targeted_affected が勝つ (relevant_only は送らない設計)
    expect(back.jp).toBe("targeted_affected");
    expect(back.minIndependentSources).toBe(true);
    expect(back.hasNews).toBe(true);
    expect(back.newFactsOnly).toBe(true);
  });

  it("新事実あり (status=updated) は専用フィールドへ写す", () => {
    expect(toWireFilters({ newFactsOnly: true })).toEqual({ status: "updated" });
    expect(fromWireFilters({ status: "updated" })).toEqual({ newFactsOnly: true });
  });
});
