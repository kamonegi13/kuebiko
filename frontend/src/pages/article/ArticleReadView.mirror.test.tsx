// 写し (Cloudflare Pages) での記事詳細の表示契約。
//
// 写しは本文 (収集した記事) を再配布しない方針なので、ArticleReadView は本文欄・
// 翻訳ボタンを出さず、出典へのリンクで案内する。メモ・ブックマーク (write API) も
// 保存不可の入力欄を見せない (ArticleDetailPage 側で隠す)。

import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ArticleDetailPage } from "../ArticleDetailPage";
import type { ArticleDetailResponse } from "../../api/article";

function baseArticle(overrides: Partial<ArticleDetailResponse["article"]> = {}): ArticleDetailResponse {
  return {
    article: {
      article_id: "a1",
      title: "重大な脆弱性が実環境で悪用中",
      url: "https://example.test/original",
      feed_title: "Example Feed",
      importance: "high",
      category: "vuln",
      status: "posted",
      posted_channel: null,
      routing_rule_id: null,
      routing_rule_label: null,
      routing_reason: null,
      summary: "kuebiko が書いた要約。",
      body: null,
      body_ja: null,
      victim_sector: null,
      victim_sector_raw: null,
      victim_country: null,
      victim_country_raw: null,
      socio_political_intent: null,
      intent_confidence: null,
      socio_political_rationale: null,
      technical_axis_summary: null,
      remediation: null,
      analyst_note: null,
      editorial_stance: null,
      pmesii: { p: false, m: false, e: false, s: false, i_infra: false, i_cyber: false, p_env: false, t: false },
      discord_message_id: null,
      discord_channel_id: null,
      published_at: "2026-08-25T00:00:00+00:00",
      created_at: "2026-08-25T00:00:00+00:00",
      event_date: null,
      event_date_basis: null,
      compromise_date: null,
      reporting_lag_days: null,
      dwell_days: null,
      body_source: null,
      extraction_failure_reason: null,
      subject_actor_ids: [],
      subject_actors: [],
      subject_actor_source: null,
      subject_actor_confidence: null,
      subject_actor_rationale: null,
      article_type: null,
      ...overrides,
    },
    discord_url: null,
    entities: [],
    note: null,
  };
}

function stubFetch(article: ArticleDetailResponse): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/v1/articles/")) {
        return new Response(JSON.stringify(article), { headers: { "content-type": "application/json" } });
      }
      return new Response("{}", { status: 404, headers: { "content-type": "application/json" } });
    }),
  );
}

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ArticleDetailPage articleId="a1" />
    </QueryClientProvider>,
  );
}

describe("記事詳細 — 写しでの本文非表示", () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it("ライブでは本文欄と翻訳ボタン、メモ・ブックマークが出る (比較対照)", async () => {
    vi.stubEnv("VITE_MIRROR", "");
    stubFetch(baseArticle({ body: "Full English body text here." }));
    renderPage();

    await waitFor(() => expect(screen.getByText(/日本語訳を生成/)).toBeTruthy());
    expect(screen.getByText(/メモ・ブックマーク/)).toBeTruthy();
  });

  it("写しでは本文欄を出さず、出典へのリンクを出す", async () => {
    vi.stubEnv("VITE_MIRROR", "1");
    // 写しの書き出しは本文を含まないので body/body_ja は null で届く。
    stubFetch(baseArticle({ body: null, body_ja: null }));
    renderPage();

    await waitFor(() => expect(screen.getByText(/本文はこの画面では表示できません/)).toBeTruthy());
    expect(screen.getByText(/本文は出典でお読みください/)).toBeTruthy();
    const sourceLink = screen.getByRole("link", { name: /出典を開く/ });
    expect(sourceLink.getAttribute("href")).toBe("https://example.test/original");

    // 翻訳ボタン・メモ/ブックマーク (write/LLM) は出ない。
    expect(screen.queryByText(/日本語訳を生成/)).toBeNull();
    expect(screen.queryByText(/メモ・ブックマーク/)).toBeNull();
  });

  it("写しで本文が万一届いても本文欄・翻訳 UI は描かない (防御線)", async () => {
    vi.stubEnv("VITE_MIRROR", "1");
    stubFetch(baseArticle({ body: "Should not render in mirror.", body_ja: null }));
    renderPage();

    await waitFor(() => expect(screen.getByText(/重大な脆弱性が実環境で悪用中/)).toBeTruthy());
    expect(screen.queryByText(/Should not render in mirror\./)).toBeNull();
    expect(screen.queryByText(/日本語訳を生成/)).toBeNull();
  });
});
