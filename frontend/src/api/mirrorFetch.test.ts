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
