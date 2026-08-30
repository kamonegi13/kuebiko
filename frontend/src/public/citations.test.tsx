/**
 * 本文の出典番号 [N] が、下の「出典」の該当行へ飛べることを固定する。
 *
 * 2026-08-30 利用者報告「統合記事の [1] とかを押しても対象のニュースが出てきません」。
 * 番号は出ているのにただの sup で、押しても何も起きなかった。運用画面側は記事への
 * リンクになっていたので、**公開面だけが取り残されていた** (同型の経路の片方漏れ)。
 *
 * ⚠ 詳細は **portal で body 直下に出る** (一覧を背景に残す作り)。render() が返す
 * container を見ても空で、「詳細が描けていない」と誤読する。document から探すこと。
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { PublicNewsSite } from "./PublicNewsSite";

const DETAIL = {
  id: "ev-test",
  headline: "テスト事象の見出し",
  bluf: "要約",
  category: "cyber",
  published_at: "2026-08-30T00:00:00+00:00",
  first_reported_at: "2026-08-29T00:00:00+00:00",
  independent_sources: 2,
  generated: true,
  note: "",
  facts: [
    { text: "1 番目の媒体が報じた事実。", paragraph: 1, source_index: 1 },
    { text: "2 番目の媒体が報じた事実。", paragraph: 1, source_index: 2 },
  ],
  discrepancies: [{ text: "食い違う点。", source_index: 2 }],
  unknowns: [],
  citations: [
    { index: 1, title: "媒体 A の記事", url: "https://a.kuebiko.example/1", source: "媒体 A" },
    { index: 2, title: "媒体 B の記事", url: "https://b.kuebiko.example/2", source: "媒体 B" },
  ],
};

function renderDetail() {
  window.history.replaceState(null, "", `/app/news/${DETAIL.id}`);
  vi.stubGlobal("fetch", async (url: string) => ({
    ok: true,
    json: async () =>
      url.includes(`/api/v1/public/news/${DETAIL.id}`)
        ? DETAIL
        : { items: [], note: "", categories: [] },
  }));
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <PublicNewsSite />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("出典番号の導線", () => {
  it("本文の [N] は押せて、出典の該当行を指す", async () => {
    renderDetail();
    await screen.findByText(DETAIL.headline);

    const refs = document.querySelectorAll<HTMLAnchorElement>('sup a[href^="#cite-"]');
    expect(refs.length).toBeGreaterThan(0);

    // 本文が指す番号すべてに、飛び先の実体があること (指しているのに無い = 今回の不具合)
    for (const a of refs) {
      const id = a.getAttribute("href")!.slice(1);
      expect(document.querySelector(`#${id}`), `${id} の飛び先が無い`).toBeTruthy();
    }
  });

  it("食い違う点の [N] も同じ導線を持つ (片方だけリンクにしない)", async () => {
    renderDetail();
    await screen.findByText(DETAIL.headline);
    const hrefs = [...document.querySelectorAll<HTMLAnchorElement>('sup a[href^="#cite-"]')].map(
      (a) => a.getAttribute("href"),
    );
    // 事実 2 件 + 食い違い 1 件 = 3 箇所すべてがリンク
    expect(hrefs).toHaveLength(3);
    expect(hrefs).toContain("#cite-2");
  });
});
