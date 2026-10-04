import { describe, expect, it } from "vitest";
import { MIRROR_PC_DEFAULT_LAYOUT } from "./mirrorDefaultLayout";
import { WIDGET_REGISTRY } from "./registry";

describe("写しの PC の既定の並び", () => {
  it("すべての部品が registry にあり、写しで出さない部品を含まない", () => {
    for (const w of MIRROR_PC_DEFAULT_LAYOUT.widgets) {
      const def = WIDGET_REGISTRY[w.id];
      expect(def, w.id).toBeDefined();
      expect(def.liveState || def.mirrorExcluded, w.id).toBeFalsy();
    }
  });

  it("uid が重複しない", () => {
    const uids = MIRROR_PC_DEFAULT_LAYOUT.widgets.map((w) => w.uid);
    expect(new Set(uids).size).toBe(uids.length);
  });
});
