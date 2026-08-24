import { describe, expect, it, vi, afterEach } from "vitest";
import { fetchEventNews } from "./eventnews";

/** fetch を差し替えて、組み立てられたクエリ文字列だけを取り出す。 */
function capture(): { url: () => URLSearchParams } {
  const calls: string[] = [];
  vi.stubGlobal("fetch", async (url: string) => {
    calls.push(url);
    return { ok: true, json: async () => ({ items: [], note: "" }) };
  });
  return { url: () => new URLSearchParams(calls[0]?.split("?")[1] ?? "") };
}

afterEach(() => vi.unstubAllGlobals());

describe("fetchEventNews のクエリ組み立て", () => {
  it("記事側 facet を全て送る (feed / status は 2026-08-25 まで型から欠落していた)", async () => {
    const c = capture();
    await fetchEventNews({
      category: "vulnerability",
      feed: "JPCERT/CC",
      channel: "alert",
      actor: "apt29",
      intent: "espionage",
      pir: "pir_jp_targeted",
      affected_vendor: "cisco",
      status: "updated,reinforced",
    });
    const p = c.url();
    expect(p.get("feed")).toBe("JPCERT/CC");
    expect(p.get("status")).toBe("updated,reinforced");
    expect(p.get("category")).toBe("vulnerability");
    expect(p.get("affected_vendor")).toBe("cisco");
  });

  it("has_news=false は「未生成のみ」を意味するので落とさない", async () => {
    const c = capture();
    await fetchEventNews({ has_news: false });
    expect(c.url().get("has_news")).toBe("false");
  });

  it("未指定の事象軸は送らない (backend 既定 = 絞らない を壊さない)", async () => {
    const c = capture();
    await fetchEventNews({ min_independent_sources: 0 });
    const p = c.url();
    expect(p.has("has_news")).toBe(false);
    expect(p.has("min_independent_sources")).toBe(false);
  });

  it("複数媒体の下限は数値として送る", async () => {
    const c = capture();
    await fetchEventNews({ min_independent_sources: 2 });
    expect(c.url().get("min_independent_sources")).toBe("2");
  });
});
