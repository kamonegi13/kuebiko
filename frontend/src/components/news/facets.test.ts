import { describe, expect, it } from "vitest";
import { migrateImportanceToLevelFilter, levelLabel } from "./facets";

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
