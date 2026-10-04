import { describe, expect, test, vi, beforeEach, afterEach } from "vitest";

import { installMirrorFetch } from "./mirrorFetch";

// 写しの経路表は書き出し側の置き場と **対** で決まる。ずれると
// 「取得できませんでした」の形で表に出るので、対応を固定する。
const json = (body: unknown) =>
  new Response(JSON.stringify(body), { headers: { "content-type": "application/json" } });
const html = () => new Response("<!doctype html>", { headers: { "content-type": "text/html" } });

describe("写しの fetch 差し替え", () => {
  let served: Record<string, unknown>;
  let original: typeof window.fetch;

  beforeEach(() => {
    original = window.fetch;
    served = {};
    window.fetch = vi.fn(async (input: RequestInfo | URL) => {
      const u = String(input);
      return u in served ? json(served[u]) : html();
    }) as typeof window.fetch;
    installMirrorFetch();
  });
  afterEach(() => {
    window.fetch = original;
  });

  async function hash(s: string): Promise<string> {
    const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
    return Array.from(new Uint8Array(buf))
      .map((b) => b.toString(16).padStart(2, "0"))
      .join("")
      .slice(0, 32);
  }

  test("絞り込みごとに別の写しを引く", async () => {
    const withQuery = "/api/v1/intel-graph/snapshot?time=30";
    served[`/data/api/${await hash(withQuery)}.json`] = { ok: "30日" };
    const r = await fetch(withQuery);
    expect(await r.json()).toEqual({ ok: "30日" });
  });

  test("写しに無い絞り込みは path だけで引き直す", async () => {
    served[`/data/api/${await hash("/api/v1/intel-graph/brief-context")}.json`] = { ok: "既定" };
    const r = await fetch("/api/v1/intel-graph/brief-context?until=2026-08-29T00%3A00%3A00Z");
    expect(await r.json()).toEqual({ ok: "既定" });
  });

  test("写していない取得は 501 で即座に断る (待たせない)", async () => {
    const r = await fetch("/api/v1/does-not-exist");
    expect(r.status).toBe(501);
  });

  test("書き込みは試みず断る", async () => {
    const r = await fetch("/api/v1/pir", { method: "POST" });
    expect(r.status).toBe(501);
  });

  test("API 以外は素通しする", async () => {
    served["/data/meta.json"] = { generated_at: "x" };
    expect(await (await fetch("/data/meta.json")).json()).toEqual({ generated_at: "x" });
  });
});

// dashboard widget (記事フィード) が実際に投げる絵姿に合わせた fixture。
// id が大きいほど新しい = created_at DESC で書き出し済みという前提を模す。
function fixtureArticle(overrides: Record<string, unknown>) {
  return {
    id: 1,
    article_id: "rss:https://example.com/1",
    title: "t",
    url: "https://example.com/1",
    feed_title: "Example Feed",
    importance: "low",
    category: "other",
    posted_channel: "watch",
    victim_sector: null,
    victim_country: null,
    socio_political_intent: null,
    intent_confidence: null,
    technical_axis_summary: null,
    malware_families: [],
    summary: "本文の要約",
    published_at: "2026-10-01T00:00:00+00:00",
    created_at: "2026-10-01T00:00:05+00:00",
    ...overrides,
  };
}

describe("一覧の全件ファイル", () => {
  let served: Record<string, unknown>;
  let original: typeof window.fetch;
  beforeEach(() => {
    original = window.fetch;
    served = {};
    window.fetch = vi.fn(async (input: RequestInfo | URL) => {
      const u = String(input);
      return u in served
        ? new Response(JSON.stringify(served[u]), { headers: { "content-type": "application/json" } })
        : new Response("<!doctype html>", { headers: { "content-type": "text/html" } });
    }) as typeof window.fetch;
    installMirrorFetch();
  });
  afterEach(() => {
    window.fetch = original;
  });

  test("絞り込み無しなら全件ファイルを返す", async () => {
    served["/data/articles.json"] = { articles: [1, 2, 3], count: 3 };
    expect(await (await fetch("/api/v1/articles")).json()).toEqual({ articles: [1, 2, 3], count: 3 });
  });

  test("書き出し済みの exact query ファイルがあればそれを使う (ブラウザ側絞り込みより優先)", async () => {
    const exact = "/api/v1/articles?category=vuln&status=posted&limit=5";
    served[`/data/api/${await hashOf(exact)}.json`] = { articles: [{ id: 99 }], count: 1 };
    served["/data/articles.json"] = { articles: [fixtureArticle({ id: 1, category: "vulnerability" })], count: 1 };
    const r = await fetch(exact);
    expect(await r.json()).toEqual({ articles: [{ id: 99 }], count: 1 });
  });

  // dashboard widget (ArticleFeedWidget) は per/mode/category/importance/channel 等を
  // 自由に組み合わせるため、組み合わせごとのファイルを全部書き出さず、専用ファイルが
  // 無ければ全件写し (articles.json) をブラウザ側で絞り込んで救う。
  describe("専用ファイルが無い絞り込みは全件写しから引き直す", () => {
    beforeEach(() => {
      served["/data/articles.json"] = {
        articles: [
          fixtureArticle({ id: 3, category: "vulnerability", importance: "high", posted_channel: "alert" }),
          fixtureArticle({ id: 2, category: "malware", importance: "medium", posted_channel: "watch" }),
          fixtureArticle({ id: 1, category: "geopolitical", importance: "low", posted_channel: "watch" }),
        ],
        count: 3,
      };
    });

    test("category (合成カテゴリ vuln を含む) で絞り込む", async () => {
      const r = await fetch("/api/v1/articles?category=vuln&status=posted&limit=30");
      const body = (await r.json()) as { articles: Array<{ id: number }>; count: number };
      expect(body.articles.map((a) => a.id)).toEqual([3]);
      expect(body.count).toBe(1);
    });

    test("importance=medium は medium 以上 (medium+high) を含む", async () => {
      const r = await fetch("/api/v1/articles?importance=medium&status=posted&limit=30");
      const body = (await r.json()) as { articles: Array<{ id: number }>; count: number };
      expect(body.articles.map((a) => a.id)).toEqual([3, 2]);
    });

    test("limit/offset でページングする (順序は書き出し元のまま)", async () => {
      const r = await fetch("/api/v1/articles?status=posted&limit=1&offset=1");
      const body = (await r.json()) as { articles: Array<{ id: number }>; count: number };
      expect(body.articles.map((a) => a.id)).toEqual([2]);
      expect(body.count).toBe(1);
    });

    test("include_summary が無ければ summary を null にする (本物の API と同じ形)", async () => {
      const r = await fetch("/api/v1/articles?status=posted&limit=30");
      const body = (await r.json()) as { articles: Array<{ summary: unknown }> };
      expect(body.articles.every((a) => a.summary === null)).toBe(true);
    });

    test("include_summary=1 なら summary を含める", async () => {
      const r = await fetch("/api/v1/articles?status=posted&limit=1&include_summary=1");
      const body = (await r.json()) as { articles: Array<{ summary: unknown }> };
      expect(body.articles[0].summary).toBe("本文の要約");
    });
  });

  // 日本との関係 (2026-10-04)。
  describe("jp (日本との関係) で絞り込む", () => {
    beforeEach(() => {
      served["/data/articles.json"] = {
        articles: [
          fixtureArticle({ id: 3, jp: "targeted" }),
          fixtureArticle({ id: 2, jp: "mentioned" }),
          fixtureArticle({ id: 1, jp: "none" }),
        ],
        count: 3,
      };
    });

    test("targeted_affected は標的・被害のみ", async () => {
      const r = await fetch("/api/v1/articles?jp=targeted_affected&status=posted&limit=30");
      const body = (await r.json()) as { articles: Array<{ id: number }> };
      expect(body.articles.map((a) => a.id)).toEqual([3]);
    });

    test("mentioned は言及以上 (jp <> none)", async () => {
      const r = await fetch("/api/v1/articles?jp=mentioned&status=posted&limit=30");
      const body = (await r.json()) as { articles: Array<{ id: number }> };
      expect(body.articles.map((a) => a.id)).toEqual([3, 2]);
    });

    test("未指定は絞り込まない", async () => {
      const r = await fetch("/api/v1/articles?status=posted&limit=30");
      const body = (await r.json()) as { articles: Array<{ id: number }> };
      expect(body.articles.map((a) => a.id)).toEqual([3, 2, 1]);
    });
  });

  // ⚠ 写していない絞り込み (search/malware/cve/pir/actor 等) に全件を返すと、画面は
  //    黙って違うものを出す (実測: 30 件のはずが 6,443 件出ていた)。501 にして表に出す。
  test("ブラウザ側で再現できない絞り込みは全件を返さず 501 にする", async () => {
    served["/data/articles.json"] = { articles: [fixtureArticle({})], count: 1 };
    const r = await fetch("/api/v1/articles?search=ransomware&status=posted&limit=30");
    expect(r.status).toBe(501);
  });

  // status=posted 以外は写し (= status=posted のみ書き出し済み) では救えない。
  test("status=posted 以外は 501 にする", async () => {
    served["/data/articles.json"] = { articles: [fixtureArticle({})], count: 1 };
    const r = await fetch("/api/v1/articles?status=draft&limit=30");
    expect(r.status).toBe(501);
  });
});

async function hashOf(s: string): Promise<string> {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
  return Array.from(new Uint8Array(buf))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("")
    .slice(0, 32);
}
