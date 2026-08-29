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
});
