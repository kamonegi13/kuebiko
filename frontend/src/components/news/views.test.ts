import { describe, expect, it } from "vitest";
import {
  BUILTIN_VIEWS, EMPTY_FILTERS, filtersEqual, isUserViewId, matchingViewId,
  slugifyViewId, toEventNewsPageHref, toNewsPageHref, viewEffectiveFilters,
  type NewsFilters, type NewsView,
} from "./views";

describe("viewEffectiveFilters", () => {
  it("未指定項目は既定値で埋まる", () => {
    const v: NewsView = { id: "x", label: "X", filters: { category: "vuln" } };
    const eff = viewEffectiveFilters(v);
    expect(eff.category).toBe("vuln");
    expect(eff.minSeverity).toBe("");
    expect(eff.since).toBe("0");
  });
});

describe("filtersEqual / matchingViewId", () => {
  it("すべて既定値なら「すべて」view に一致する", () => {
    const id = matchingViewId(EMPTY_FILTERS, BUILTIN_VIEWS);
    expect(id).toBe("builtin:all");
  });

  it("注目 (S2 + 関連性あり) に一致する", () => {
    const f: NewsFilters = { ...EMPTY_FILTERS, minSeverity: "S2", relevantOnly: true };
    expect(matchingViewId(f, BUILTIN_VIEWS)).toBe("builtin:focus");
  });

  it("注意以上 (S2 のみ、他は絞らない) に一致する (ダッシュボード事象ニュース部品の既定)", () => {
    const f: NewsFilters = { ...EMPTY_FILTERS, minSeverity: "S2" };
    expect(matchingViewId(f, BUILTIN_VIEWS)).toBe("builtin:notable");
  });

  it("1 項目でも違えば一致しない (変更あり)", () => {
    const f: NewsFilters = { ...EMPTY_FILTERS, minSeverity: "S2", relevantOnly: true, category: "malware" };
    expect(matchingViewId(f, BUILTIN_VIEWS)).toBeNull();
  });

  it("filtersEqual は全フィールドを見る", () => {
    expect(filtersEqual(EMPTY_FILTERS, { ...EMPTY_FILTERS })).toBe(true);
    expect(filtersEqual(EMPTY_FILTERS, { ...EMPTY_FILTERS, feed: "x" })).toBe(false);
  });
});

describe("isUserViewId", () => {
  it("builtin: 接頭辞は利用者ビューではない", () => {
    expect(isUserViewId("builtin:all")).toBe(false);
    expect(isUserViewId("view-abc123")).toBe(true);
    expect(isUserViewId("")).toBe(false);
  });
});

describe("slugifyViewId", () => {
  it("一意な id を生成する (連続呼び出しでも重複しない想定)", () => {
    const a = slugifyViewId("自分のビュー");
    const b = slugifyViewId("自分のビュー");
    expect(a).not.toBe(b);
    expect(a.startsWith("view-")).toBe(true);
  });
});

describe("toNewsPageHref / toEventNewsPageHref", () => {
  it("空の絞り込みは素のページ URL", () => {
    expect(toNewsPageHref({})).toBe("/app/news");
    expect(toEventNewsPageHref({})).toBe("/app/eventnews");
  });

  it("News は since、事象ニュースは since_hours を使う (画面ごとの param 名の違い)", () => {
    expect(toNewsPageHref({ since: "72" })).toBe("/app/news?since=72");
    expect(toEventNewsPageHref({ since: "72" })).toBe("/app/eventnews?since_hours=72");
  });

  it("事象ニュース固有の軸は専用 param へ写す", () => {
    const href = toEventNewsPageHref({ minIndependentSources: true, hasNews: true, newFactsOnly: true });
    const url = new URL(href, "http://localhost");
    expect(url.searchParams.get("min_sources")).toBe("2");
    expect(url.searchParams.get("has_news")).toBe("1");
    expect(url.searchParams.get("status")).toBe("updated");
  });

  it("注目 view (S2 + 関連性あり) を正しく符号化する", () => {
    const f = viewEffectiveFilters(BUILTIN_VIEWS.find((v) => v.id === "builtin:focus")!);
    const url = new URL(toNewsPageHref(f), "http://localhost");
    expect(url.searchParams.get("min_severity")).toBe("S2");
    expect(url.searchParams.get("relevant_only")).toBe("1");
  });
});
