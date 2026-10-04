import { describe, expect, it } from "vitest";
import {
  jpTitleTag, severityTitleTags,
  EMPTY_SEVERITY_FACET, legacyLevelFilterToSeverityFacet, levelLabel,
  migrateImportanceToLevelFilter, migrateLegacyToSeverityFacet, readSeverityFacet,
  severityFacetFromConfigStrings, severityFacetQueryParams, writeSeverityFacet,
} from "./facets";

describe("migrateImportanceToLevelFilter", () => {
  it("high → top (重大のみ)", () => {
    expect(migrateImportanceToLevelFilter("high")).toBe("top");
  });

  it("medium / high,medium / medium,high → notable (注意以上)", () => {
    expect(migrateImportanceToLevelFilter("medium")).toBe("notable");
    expect(migrateImportanceToLevelFilter("high,medium")).toBe("notable");
    expect(migrateImportanceToLevelFilter("medium,high")).toBe("notable");
  });

  it("low・未知の値・空は すべて (絞り込み無し)", () => {
    expect(migrateImportanceToLevelFilter("low")).toBe("");
    expect(migrateImportanceToLevelFilter("bogus")).toBe("");
    expect(migrateImportanceToLevelFilter("")).toBe("");
    expect(migrateImportanceToLevelFilter(null)).toBe("");
    expect(migrateImportanceToLevelFilter(undefined)).toBe("");
  });
});

describe("legacyLevelFilterToSeverityFacet", () => {
  it("top → S3・関連性すべて・戦略上の重み含めない", () => {
    expect(legacyLevelFilterToSeverityFacet("top")).toEqual({
      minSeverity: "S3", relevantOnly: false, includeStrategic: false,
    });
  });

  it("notable → S2・戦略上の重みを含める (旧仕様が軸なし heavy も含んでいたため)", () => {
    expect(legacyLevelFilterToSeverityFacet("notable")).toEqual({
      minSeverity: "S2", relevantOnly: false, includeStrategic: true,
    });
  });

  it("relevant → S1・関連性ありのみ (旧仕様は severity 記録済みのみが対象)", () => {
    expect(legacyLevelFilterToSeverityFacet("relevant")).toEqual({
      minSeverity: "S1", relevantOnly: true, includeStrategic: false,
    });
  });

  it("未知の値・null・undefined は絞り込み無し", () => {
    expect(legacyLevelFilterToSeverityFacet("bogus")).toEqual(EMPTY_SEVERITY_FACET);
    expect(legacyLevelFilterToSeverityFacet(null)).toEqual(EMPTY_SEVERITY_FACET);
    expect(legacyLevelFilterToSeverityFacet(undefined)).toEqual(EMPTY_SEVERITY_FACET);
  });
});

describe("migrateLegacyToSeverityFacet", () => {
  it("level_filter があれば importance は見ない", () => {
    expect(migrateLegacyToSeverityFacet("top", "medium")).toEqual({
      minSeverity: "S3", relevantOnly: false, includeStrategic: false,
    });
  });

  it("level_filter が無ければ importance (旧 1 段目) を経由する", () => {
    // importance=high → 旧 level_filter=top → 新 S3
    expect(migrateLegacyToSeverityFacet(null, "high")).toEqual({
      minSeverity: "S3", relevantOnly: false, includeStrategic: false,
    });
    // importance=medium → 旧 level_filter=notable → 新 S2 + 戦略上の重みを含める
    expect(migrateLegacyToSeverityFacet(undefined, "medium")).toEqual({
      minSeverity: "S2", relevantOnly: false, includeStrategic: true,
    });
  });

  it("どちらも無ければ絞り込み無し", () => {
    expect(migrateLegacyToSeverityFacet(null, null)).toEqual(EMPTY_SEVERITY_FACET);
  });
});

describe("readSeverityFacet / writeSeverityFacet", () => {
  it("新 3 facet のいずれかがあれば、それを使う (旧パラメータは見ない)", () => {
    const p = new URLSearchParams("min_severity=S3&relevant_only=1&level_filter=notable");
    expect(readSeverityFacet(p)).toEqual({ minSeverity: "S3", relevantOnly: true, includeStrategic: false });
  });

  it("新 facet が無ければ旧 level_filter を写す", () => {
    const p = new URLSearchParams("level_filter=relevant");
    expect(readSeverityFacet(p)).toEqual({ minSeverity: "S1", relevantOnly: true, includeStrategic: false });
  });

  it("どちらも無ければ fallback (画面ごとの既定)", () => {
    const p = new URLSearchParams("");
    const fallback = { minSeverity: "S2" as const, relevantOnly: false, includeStrategic: true };
    expect(readSeverityFacet(p, fallback)).toEqual(fallback);
    expect(readSeverityFacet(p)).toEqual(EMPTY_SEVERITY_FACET);
  });

  it("read ⇄ write は往復する (すべて設定時)", () => {
    const state = { minSeverity: "S2" as const, relevantOnly: true, includeStrategic: true };
    const q = new URLSearchParams();
    writeSeverityFacet(q, state);
    expect(readSeverityFacet(q)).toEqual(state);
  });

  it("書き出しは絞り込み無しなら何も書かない (URL を汚さない)", () => {
    const q = new URLSearchParams();
    writeSeverityFacet(q, EMPTY_SEVERITY_FACET);
    expect(q.toString()).toBe("");
  });

  it("include_strategic は min_severity 未指定なら書かない (toggle 無効時)", () => {
    const q = new URLSearchParams();
    writeSeverityFacet(q, { minSeverity: "", relevantOnly: false, includeStrategic: true });
    expect(q.toString()).toBe("");
  });
});

describe("severityFacetQueryParams", () => {
  it("絞り込み無しは空オブジェクト相当 (全フィールド undefined)", () => {
    expect(severityFacetQueryParams(EMPTY_SEVERITY_FACET)).toEqual({
      min_severity: undefined, relevant_only: undefined, include_strategic: undefined,
    });
  });

  it("include_strategic は min_severity が無ければ送らない", () => {
    expect(
      severityFacetQueryParams({ minSeverity: "", relevantOnly: false, includeStrategic: true }),
    ).toEqual({ min_severity: undefined, relevant_only: undefined, include_strategic: undefined });
  });

  it("全部設定時はそのまま渡す", () => {
    expect(
      severityFacetQueryParams({ minSeverity: "S3", relevantOnly: true, includeStrategic: true }),
    ).toEqual({ min_severity: "S3", relevant_only: true, include_strategic: true });
  });
});

describe("severityFacetFromConfigStrings (dashboard widget config)", () => {
  it("新 3 facet の文字列があればそれを優先する", () => {
    expect(
      severityFacetFromConfigStrings("S3", "1", "", "notable", "", EMPTY_SEVERITY_FACET),
    ).toEqual({ minSeverity: "S3", relevantOnly: true, includeStrategic: false });
  });

  it("新 facet が無ければ旧 level_filter / importance を移行する", () => {
    expect(
      severityFacetFromConfigStrings("", "", "", "", "high", EMPTY_SEVERITY_FACET),
    ).toEqual({ minSeverity: "S3", relevantOnly: false, includeStrategic: false });
  });

  it("どれも無ければ fallback", () => {
    const fallback = { minSeverity: "S2" as const, relevantOnly: false, includeStrategic: true };
    expect(severityFacetFromConfigStrings("", "", "", "", "", fallback)).toEqual(fallback);
  });
});

describe("levelLabel", () => {
  it("1-6 はラベルを返す", () => {
    expect(levelLabel(1)).toBe("重大◎");
    expect(levelLabel(6)).toBe("参考");
  });

  it("null/undefined は null (未記録)", () => {
    expect(levelLabel(null)).toBeNull();
    expect(levelLabel(undefined)).toBeNull();
  });
});

describe("ダッシュボードのタイトルに添える印", () => {
  it("日本との関係の選択がタイトルの印になる", () => {
    expect(jpTitleTag("targeted_affected")).toBe("日本が標的・被害");
    expect(jpTitleTag("mentioned")).toBe("日本に触れるもの");
    expect(jpTitleTag("")).toBe("");
  });

  it("深刻さ・関連性・政策地政学が印になり、深刻さがすべてなら政策地政学は付かない", () => {
    expect(severityTitleTags({ minSeverity: "S3", relevantOnly: true, includeStrategic: true })).toEqual([
      "重大", "関連性あり", "政策・地政学を含む",
    ]);
    expect(severityTitleTags({ minSeverity: "", relevantOnly: false, includeStrategic: true })).toEqual([]);
  });
});
