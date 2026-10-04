import { describe, expect, test } from "vitest";

import { isOutsideMirror, MIRROR_HOME, mirrorNavGroups, NAV_FLAT } from "./nav";

// 写しは「書き出したデータのある画面」しか描けない。印と書き出しがずれると、
// 利用者からは読み込み中で固まったようにしか見えない (2026-08-29 の実障害)。
describe("写しのナビ", () => {
  test("印の付いた画面だけが残る", () => {
    const items = mirrorNavGroups().flatMap((g) => g.items);
    expect(items.length).toBeGreaterThan(0);
    expect(items.every((it) => it.mirror)).toBe(true);
  });

  test("着地先は写しに含まれている", () => {
    expect(isOutsideMirror(MIRROR_HOME)).toBe(false);
  });

  test("印の無い画面は写しの外と判定される", () => {
    const unmarked = NAV_FLAT.filter((it) => !it.mirror);
    expect(unmarked.length).toBeGreaterThan(0);
    for (const it of unmarked) expect(isOutsideMirror(it.href)).toBe(true);
  });

  // ダッシュボードと週次深掘りは写しにデータを持つ (ダッシュボードは固定レイアウト、
  // 深掘りは /api/v1/deep-dives) ので、ナビに含めて到達可能にする。
  test("ダッシュボードと週次深掘りは写しに含まれる", () => {
    const hrefs = mirrorNavGroups().flatMap((g) => g.items.map((it) => it.href));
    expect(hrefs).toContain("/app");
    expect(hrefs).toContain("/app/deep-dive");
  });

  // 購読ソース・メモ/ブックマーク・Grok 関連は運用者固有の設定/私的記録なので、
  // 写し (匿名公開) には出さない。
  test("購読ソース・メモ・ブックマークは写しから外れている", () => {
    const hrefs = mirrorNavGroups().flatMap((g) => g.items.map((it) => it.href));
    expect(hrefs).not.toContain("/app/subscriptions");
    expect(hrefs).not.toContain("/app/notes");
  });
});
