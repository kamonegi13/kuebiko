// 設定画面の群立て (S1、docs/settings_consolidation_plan.md)。
//
// 配置基準の改訂 (CLAUDE.md §11、2026-09-16):
// **専用ページは閲覧のみ、定義の変更はすべて設定カテゴリ**。
// 後続の段が移してくる定義 (関心 / 収集 / 台帳 / 表示) は【定義】群へ着地する。
import { describe, expect, it } from "vitest";

import { TAB_GROUPS } from "./ConfigPage";

describe("設定タブの群立て", () => {
  it("定義・接続・記録・上級の 4 群に分かれる", () => {
    expect(TAB_GROUPS.map((g) => g.group)).toEqual(["定義", "接続・システム", "記録", "上級"]);
  });

  it("【定義】群が存在する — 後続の移設がここへ着地する", () => {
    const def = TAB_GROUPS.find((g) => g.group === "定義");

    expect(def).toBeDefined();
    expect(def!.tabs.length).toBeGreaterThan(0);
  });

  it("移設済みの定義が【定義】群にある (S3: 指定事業者名簿)", () => {
    const def = TAB_GROUPS.find((g) => g.group === "定義")!;

    expect(def.tabs.map((t) => t.id)).toContain("operators");
  });

  it("空の群を作らない", () => {
    // 器だけ先に置くと死んだ UI が残る。移設は中身と一緒に入れる。
    for (const g of TAB_GROUPS) {
      expect(g.tabs.length, `${g.group} が空`).toBeGreaterThan(0);
    }
  });

  it("タブ id が重複しない", () => {
    const ids = TAB_GROUPS.flatMap((g) => g.tabs.map((t) => t.id));

    expect(new Set(ids).size).toBe(ids.length);
  });

  it("記録と上級は末尾に置く (設定ではないものを設定の前に出さない)", () => {
    const names = TAB_GROUPS.map((g) => g.group);

    expect(names.indexOf("記録")).toBeGreaterThan(names.indexOf("定義"));
    expect(names.indexOf("上級")).toBe(names.length - 1);
  });
});
