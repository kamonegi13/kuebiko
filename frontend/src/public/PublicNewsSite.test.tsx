/**
 * 公開ニュースサイトの表示契約。
 *
 * 匿名の第三者が見る唯一の画面なので、「出典が常に見えること」と「分析者向けの
 * 導線が出ないこと」を固定する。
 */
import { describe, expect, it, vi, afterEach, beforeEach } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { PublicNewsSite } from "./PublicNewsSite";

const ITEM = {
  id: "ev-1",
  headline: "重大な脆弱性が実環境で悪用中",
  summary: "kuebiko が書いた要約。",
  generated: true,
  sources: 5,
  independent_sources: 3,
  published_at: "2026-08-25T00:00:00+00:00",
  citations: [
    { index: 1, title: "原記事タイトル", url: "https://example.test/a", source: "Example News", source_tier: "news", published_at: null },
    { index: 2, title: "別媒体の記事", url: "https://example.test/b", source: "Other Wire", source_tier: "news", published_at: null },
  ],
};

const requested: string[] = [];

function renderSite() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <PublicNewsSite />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  window.history.replaceState(null, "", "/app/news");
  requested.length = 0;
  vi.stubGlobal("fetch", async (url: string) => {
    requested.push(url);
    return {
      ok: true,
      json: async () =>
        url.includes("/api/v1/public/news/")
          ? { ...ITEM, bluf: "要点。", facts: [{ text: "事実行。", source_index: 1, paragraph: 1 }],
              discrepancies: [], unknowns: [], first_reported_at: ITEM.published_at,
              note: "kuebiko が生成した要約であり、原記事そのものではない" }
          : {
              items: [ITEM],
              note: "kuebiko が生成した要約であり、原記事そのものではない",
              categories: ["vuln", "incident_breach", "threat", "geopolitical"],
            },
    };
  });
});

afterEach(() => {
  // 自動 cleanup を有効にしていないので明示的に破棄する
  // (残すと次の test で同じ要素が複数見つかる)
  cleanup();
  vi.unstubAllGlobals();
});

describe("公開ニュースサイト", () => {
  it("一覧に見出しと出典媒体を出す", async () => {
    renderSite();
    expect(await screen.findByText(ITEM.headline)).toBeTruthy();
    // 出典は常に見える (契約 2)。注目枠にも同じ媒体が出るので件数は問わない
    await waitFor(() => expect(screen.getAllByText(/Example News/).length).toBeGreaterThan(0));
  });

  it("複数媒体が報じた事象はその旨を示す", async () => {
    renderSite();
    await screen.findByText(ITEM.headline);
    expect(screen.getAllByText("3 媒体が報道").length).toBeGreaterThan(0);
  });

  it("生成物であることを常に明示する", async () => {
    renderSite();
    await screen.findByText(ITEM.headline);
    expect(screen.getByText(/原記事そのものではありません/)).toBeTruthy();
  });

  it("運用者向けの導線は控えめなログインリンクだけ", async () => {
    renderSite();
    await screen.findByText(ITEM.headline);
    const links = Array.from(document.querySelectorAll("a")).map((a) => a.getAttribute("href"));
    expect(links).toContain("/auth/");
    // 分析画面への導線を出さない
    for (const analyst of ["/app/dashboard", "/app/intel/synthesis", "/app/config", "/app/pir"]) {
      expect(links).not.toContain(analyst);
    }
  });

  it("記事を開くと出典が番号付きで全件並ぶ", async () => {
    window.history.replaceState(null, "", "/app/news/ev-1");
    renderSite();
    expect(await screen.findByText(/出典 \(2\)/)).toBeTruthy();
    const external = Array.from(document.querySelectorAll('a[target="_blank"]'));
    expect(external.length).toBe(2);
    // 原記事へは外部リンク。rel を落とすと参照元が漏れる
    for (const a of external) expect(a.getAttribute("rel")).toContain("noopener");
  });
});

describe("カテゴリ", () => {
  it("カテゴリのタブを出す (並びと定義は backend が持つ)", async () => {
    renderSite();
    await screen.findByText(ITEM.headline);
    expect(screen.getByText("新着")).toBeTruthy();
    // 語彙が無い環境では key がそのまま出る (ラベル解決は vocabularies が SSoT)
    const tabs = Array.from(document.querySelectorAll("nav a")).map((a) => a.getAttribute("href"));
    expect(tabs).toContain("/app/news/c/vuln");
    expect(tabs).toContain("/app/news/c/geopolitical");
  });

  it("カテゴリページでは category を付けて取得する", async () => {
    window.history.replaceState(null, "", "/app/news/c/threat");
    renderSite();
    await screen.findByText(ITEM.headline);
    expect(requested.some((u) => u.includes("category=threat"))).toBe(true);
  });

  it("注目は新着の 1 ページ目だけに出す (カテゴリ絞り込み中は出さない)", async () => {
    window.history.replaceState(null, "", "/app/news/c/threat");
    renderSite();
    await screen.findByText(ITEM.headline);
    expect(requested.some((u) => u.includes("featured=true"))).toBe(false);
  });

  it("新着では注目を複数媒体 + 統合本文に限って取得する", async () => {
    renderSite();
    await screen.findByText(ITEM.headline);
    await waitFor(() => expect(requested.some((u) => u.includes("featured=true"))).toBe(true));
    expect(screen.getByText("注目")).toBeTruthy();
  });
});
