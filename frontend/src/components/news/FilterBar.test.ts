import { describe, expect, it } from "vitest";
import { applyRelation, relationFromState } from "./FilterBar";

describe("relationFromState", () => {
  it("jp が入っていれば jp を優先する (旧 URL で両方指定されていた場合も同じ)", () => {
    expect(relationFromState("targeted_affected", true)).toBe("targeted_affected");
    expect(relationFromState("mentioned", true)).toBe("mentioned");
  });

  it("jp が無く relevantOnly のみなら relevant", () => {
    expect(relationFromState("", true)).toBe("relevant");
  });

  it("どちらも無ければ すべて (空文字)", () => {
    expect(relationFromState("", false)).toBe("");
  });
});

describe("applyRelation", () => {
  it("targeted_affected / mentioned → jp のみ設定、relevantOnly は false", () => {
    expect(applyRelation("targeted_affected")).toEqual({ jp: "targeted_affected", relevantOnly: false });
    expect(applyRelation("mentioned")).toEqual({ jp: "mentioned", relevantOnly: false });
  });

  it("relevant → relevantOnly のみ true", () => {
    expect(applyRelation("relevant")).toEqual({ jp: "", relevantOnly: true });
  });

  it("すべて (空) → 両方 false/空", () => {
    expect(applyRelation("")).toEqual({ jp: "", relevantOnly: false });
  });

  it("relationFromState と往復する (round-trip)", () => {
    for (const v of ["", "relevant", "targeted_affected", "mentioned"] as const) {
      const { jp, relevantOnly } = applyRelation(v);
      expect(relationFromState(jp, relevantOnly)).toBe(v);
    }
  });
});
