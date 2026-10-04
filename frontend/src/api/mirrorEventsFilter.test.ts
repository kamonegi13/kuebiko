import { describe, expect, it } from "vitest";
import { filterMirrorEvents, type MirrorEventNewsItem } from "./mirrorEventsFilter";

function item(overrides: Partial<MirrorEventNewsItem> = {}): MirrorEventNewsItem {
  return {
    id: "ev-1",
    headline: "見出し",
    preview: "要点の冒頭",
    status: "new",
    change_kind: null,
    importance: "medium",
    member_count: 1,
    independent_sources: 1,
    state_media_count: 0,
    unclassified_sources: 0,
    best_source_tier: "news",
    first_reported_at: "2026-10-01T00:00:00+00:00",
    last_reported_at: "2026-10-01T00:00:00+00:00",
    current_version: 0,
    has_news: false,
    ...overrides,
  };
}

describe("filterMirrorEvents", () => {
  it("既定 (high+medium) は low を除く — 元の不具合の再現条件", () => {
    const items = [
      item({ id: "h", importance: "high" }),
      item({ id: "m", importance: "medium" }),
      item({ id: "l", importance: "low" }),
    ];

    const { items: out } = filterMirrorEvents(items, { importance: "high,medium" });

    expect(out.map((i) => i.id).sort()).toEqual(["h", "m"]);
  });

  it("importance 未指定は絞らない (全重要度)", () => {
    const items = [item({ id: "h", importance: "high" }), item({ id: "l", importance: "low" })];
    const { items: out } = filterMirrorEvents(items, {});
    expect(out.length).toBe(2);
  });

  it("importance=high のみは high だけを返す", () => {
    const items = [item({ id: "h", importance: "high" }), item({ id: "m", importance: "medium" })];
    const { items: out } = filterMirrorEvents(items, { importance: "high" });
    expect(out.map((i) => i.id)).toEqual(["h"]);
  });

  it("category は合成グループ (vuln = vulnerability + advisory) を展開する", () => {
    const items = [
      item({ id: "a", categories: ["vulnerability"] }),
      item({ id: "b", categories: ["advisory"] }),
      item({ id: "c", categories: ["malware"] }),
    ];
    const { items: out } = filterMirrorEvents(items, { category: "vuln" });
    expect(out.map((i) => i.id).sort()).toEqual(["a", "b"]);
  });

  it("feed は構成記事の媒体名の和集合から絞る", () => {
    const items = [
      item({ id: "a", feeds: ["JPCERT/CC", "ITmedia"] }),
      item({ id: "b", feeds: ["ITmedia"] }),
    ];
    const { items: out } = filterMirrorEvents(items, { feed: "JPCERT/CC" });
    expect(out.map((i) => i.id)).toEqual(["a"]);
  });

  it("actor / cve / malware / pir は entities の型別一覧から絞る", () => {
    const items = [
      item({ id: "a", entities: { actor: ["apt29"], cve: ["CVE-2026-1"] } }),
      item({ id: "b", entities: { actor: ["lazarus"] } }),
    ];
    expect(filterMirrorEvents(items, { actor: "apt29" }).items.map((i) => i.id)).toEqual(["a"]);
    expect(filterMirrorEvents(items, { cve: "cve-2026-1" }).items.map((i) => i.id)).toEqual(["a"]);
  });

  it("affected_vendor は vendor/product の部分一致", () => {
    const items = [
      item({ id: "a", vendors: ["Fortinet", "FortiOS"] }),
      item({ id: "b", vendors: ["Cisco"] }),
    ];
    const { items: out } = filterMirrorEvents(items, { affected_vendor: "forti" });
    expect(out.map((i) => i.id)).toEqual(["a"]);
  });

  it("min_independent_sources は下限として効く", () => {
    const items = [
      item({ id: "a", independent_sources: 1 }),
      item({ id: "b", independent_sources: 3 }),
    ];
    const { items: out } = filterMirrorEvents(items, { min_independent_sources: 2 });
    expect(out.map((i) => i.id)).toEqual(["b"]);
  });

  it("has_news は current_version>0 を表す has_news フィールドと一致させる", () => {
    const items = [item({ id: "a", has_news: true }), item({ id: "b", has_news: false })];
    const { items: out } = filterMirrorEvents(items, { has_news: true });
    expect(out.map((i) => i.id)).toEqual(["a"]);
  });

  it("status はカンマ区切りの集合一致 (新事実あり = updated)", () => {
    const items = [
      item({ id: "a", status: "updated" }),
      item({ id: "b", status: "reinforced" }),
      item({ id: "c", status: "new" }),
    ];
    const { items: out } = filterMirrorEvents(items, { status: "updated" });
    expect(out.map((i) => i.id)).toEqual(["a"]);
  });

  it("since_hours は last_reported_at の相対窓で絞る", () => {
    const now = Date.now();
    const items = [
      item({ id: "recent", last_reported_at: new Date(now - 1 * 60 * 60 * 1000).toISOString() }),
      item({ id: "old", last_reported_at: new Date(now - 100 * 60 * 60 * 1000).toISOString() }),
    ];
    const { items: out } = filterMirrorEvents(items, { since_hours: 24 });
    expect(out.map((i) => i.id)).toEqual(["recent"]);
  });

  it("search は見出し・冒頭要点を対象に大小文字を無視して一致する", () => {
    const items = [
      item({ id: "a", headline: "Fortinet 製品に脆弱性", preview: "" }),
      item({ id: "b", headline: "別件", preview: "FORTINET の話題" }),
      item({ id: "c", headline: "無関係", preview: "" }),
    ];
    const { items: out } = filterMirrorEvents(items, { search: "fortinet" });
    expect(out.map((i) => i.id).sort()).toEqual(["a", "b"]);
  });

  it("entity_type/entity_value の pivot は entities の該当型を見る", () => {
    const items = [
      item({ id: "a", entities: { malware_family: ["Emotet"] } }),
      item({ id: "b", entities: { malware_family: ["Qakbot"] } }),
    ];
    const { items: out } = filterMirrorEvents(items, {
      entity_type: "malware_family",
      entity_value: "emotet",
    });
    expect(out.map((i) => i.id)).toEqual(["a"]);
  });

  it("並び順は last_reported_at の新しい順", () => {
    const items = [
      item({ id: "old", last_reported_at: "2026-09-01T00:00:00+00:00" }),
      item({ id: "new", last_reported_at: "2026-10-01T00:00:00+00:00" }),
    ];
    const { items: out } = filterMirrorEvents(items, {});
    expect(out.map((i) => i.id)).toEqual(["new", "old"]);
  });

  it("offset/limit でページングする", () => {
    const items = [1, 2, 3, 4, 5].map((n) =>
      item({ id: `e${n}`, last_reported_at: `2026-10-0${n}T00:00:00+00:00` }),
    );
    const { items: out } = filterMirrorEvents(items, { limit: 2, offset: 2 });
    // 新しい順: e5,e4,e3,e2,e1 → offset 2 から 2 件 = e3,e2
    expect(out.map((i) => i.id)).toEqual(["e3", "e2"]);
  });

  it("応答の形は items/note を持つ (scan_capped は写しに無い)", () => {
    const out = filterMirrorEvents([item()], {});
    expect(out).toHaveProperty("items");
    expect(out).toHaveProperty("note");
  });

  describe("jp (日本との関係)", () => {
    const items = [
      item({ id: "t", jp: "targeted" }),
      item({ id: "a", jp: "affected" }),
      item({ id: "m", jp: "mentioned" }),
      item({ id: "n", jp: "none" }),
      item({ id: "u", jp: undefined }),
    ];

    it("targeted_affected は標的・被害のみ", () => {
      const { items: out } = filterMirrorEvents(items, { jp: "targeted_affected" });
      expect(out.map((i) => i.id).sort()).toEqual(["a", "t"]);
    });

    it("mentioned は言及以上 (none/未記録を除く)", () => {
      const { items: out } = filterMirrorEvents(items, { jp: "mentioned" });
      expect(out.map((i) => i.id).sort()).toEqual(["a", "m", "t"]);
    });

    it("未指定は絞らない", () => {
      const { items: out } = filterMirrorEvents(items, {});
      expect(out.length).toBe(5);
    });
  });

  describe("sort (重要度順)", () => {
    const items = [
      item({ id: "worst", level: 6, last_reported_at: "2026-10-03T00:00:00+00:00" }),
      item({ id: "best", level: 1, last_reported_at: "2026-10-01T00:00:00+00:00" }),
      item({ id: "mid", level: 3, last_reported_at: "2026-10-02T00:00:00+00:00" }),
      item({ id: "unrecorded", level: null, last_reported_at: "2026-10-04T00:00:00+00:00" }),
    ];

    it("sort=level は重要度の高い順 (未記録は最後)", () => {
      const { items: out } = filterMirrorEvents(items, { sort: "level" });
      expect(out.map((i) => i.id)).toEqual(["best", "mid", "worst", "unrecorded"]);
    });

    it("未指定は新着順のまま (level を見ない)", () => {
      const { items: out } = filterMirrorEvents(items, {});
      expect(out.map((i) => i.id)).toEqual(["unrecorded", "worst", "mid", "best"]);
    });
  });

  // 重要度 6 段階による絞り込み (2026-10-04)。事象は構成記事のうち最良 (最小) の level で判定。
  describe("level_filter", () => {
    const items = [
      item({ id: "top", level: 1 }),
      item({ id: "notable", level: 4 }),
      item({ id: "relevant_only", level: 5 }),
      item({ id: "heavy_no_level", level: null, strategic_weight_heavy_no_level: true }),
      item({ id: "none", level: 6 }),
    ];

    it("top は level 1-2 (深刻さ S3) のみ", () => {
      const { items: out } = filterMirrorEvents(items, { level_filter: "top" });
      expect(out.map((i) => i.id)).toEqual(["top"]);
    });

    it("notable は level 1-4、または軸なしで heavy の構成記事を含む", () => {
      const { items: out } = filterMirrorEvents(items, { level_filter: "notable" });
      expect(out.map((i) => i.id).sort()).toEqual(["heavy_no_level", "notable", "top"]);
    });

    it("relevant は level 1,3,5 (関連性あり)", () => {
      const { items: out } = filterMirrorEvents(items, { level_filter: "relevant" });
      expect(out.map((i) => i.id).sort()).toEqual(["relevant_only", "top"]);
    });

    it("未指定は絞らない", () => {
      const { items: out } = filterMirrorEvents(items, {});
      expect(out.length).toBe(5);
    });
  });
});
