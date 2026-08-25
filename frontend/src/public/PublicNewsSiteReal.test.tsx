/**
 * **本番の実データ 60 件**を実際に描画して落ちないことを確認する。
 *
 * 2026-08-25 利用者報告「記事を表示すると、出る場合もあるがブラックになって何も
 * 出なくなる場合がある」。API のレスポンス形状の検査 (型) は全件通っていたので、
 * 落ちるとしたら描画側。**形が正しいことと描けることは別**なので実物で確かめる。
 */
import { describe, expect, it, vi, afterEach } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { PublicNewsSite } from "./PublicNewsSite";
import details from "./__fixtures__/real-details.json";

type Detail = (typeof details)[number];

function renderDetail(d: Detail) {
  window.history.replaceState(null, "", `/app/news/${d.id}`);
  // 一覧要求には空を返す (同じ見出しが 2 箇所に出て照合が曖昧になるのを避ける)
  vi.stubGlobal("fetch", async (url: string) => ({
    ok: true,
    json: async () =>
      url.includes(`/api/v1/public/news/${d.id}`) ? d : { items: [], note: "", categories: [] },
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

describe("本番実データの描画", () => {
  it.each(details.map((d, i) => [i, d.id, d] as const))(
    "[%i] %s が描ける",
    async (_i, _id, d) => {
      renderDetail(d);
      // 単独報は見出しと出典タイトルが同じなので 2 箇所に出る (正しい挙動)
      expect((await screen.findAllByText(d.headline)).length).toBeGreaterThan(0);
      // 出典が 1 件も出ないのは契約違反 (公開面は出典必須)
      expect(screen.getByText(new RegExp(`出典 \\(${d.citations.length}\\)`))).toBeTruthy();
    },
  );
});
