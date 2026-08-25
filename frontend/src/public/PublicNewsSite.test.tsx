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
  category: "vuln",
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
  it("一覧は「何の話か」だけを出す (カテゴリ → 見出し → 要約 → 日付)", async () => {
    renderSite();
    expect(await screen.findByText(ITEM.headline)).toBeTruthy();
    expect(screen.getAllByText(ITEM.summary).length).toBeGreaterThan(0);
    // カテゴリバッジ (語彙が無い環境では key がそのまま出る)
    expect(screen.getAllByText("vuln").length).toBeGreaterThan(0);
  });

  it("一覧に出典名と媒体数を出さない (2026-08-25 利用者指摘)", async () => {
    renderSite();
    await screen.findByText(ITEM.headline);
    // 分析者向けの情報は開いてから見せる
    expect(screen.queryByText(/Example News/)).toBeNull();
    expect(screen.queryByText(/Other Wire/)).toBeNull();
    expect(screen.queryByText(/媒体が報道/)).toBeNull();
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

  it("媒体数は記事を開いたときにだけ見せる", async () => {
    window.history.replaceState(null, "", "/app/news/ev-1");
    renderSite();
    await screen.findByText(/出典 \(2\)/);
    expect(screen.getByText(/独立 3 媒体が報道/)).toBeTruthy();
    expect(screen.getByText(/Example News/)).toBeTruthy();
  });
});

describe("カテゴリ", () => {
  it("カテゴリのタブを出す (並びと定義は backend が持つ)", async () => {
    renderSite();
    await screen.findByText(ITEM.headline);
    // ナビのタブと一覧の見出しの 2 箇所に出る
    expect(screen.getAllByText("新着").length).toBeGreaterThan(0);
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

describe("ドロワー表示", () => {
  it("記事を開いても一覧は裏に残る (閉じたときスクロール位置を失わない)", async () => {
    window.history.replaceState(null, "", "/app/news/ev-1");
    renderSite();
    // 一覧の見出し (新着) と記事の中身が同時に存在する
    await screen.findByText(/出典 \(2\)/);
    expect(screen.getAllByText("新着").length).toBeGreaterThan(0);
  });

  it("閉じる操作は履歴を戻す (URL と表示を一致させたまま)", async () => {
    const back = vi.spyOn(window.history, "back").mockImplementation(() => {});
    window.history.replaceState(null, "", "/app/news/ev-1");
    renderSite();
    await screen.findByText(/出典 \(2\)/);
    const close = document.querySelector('[aria-label="閉じる"], button[title="閉じる"]');
    if (close) (close as HTMLElement).click();
    expect(back).toHaveBeenCalled();
    back.mockRestore();
  });

  it("記事を開いている間は背後をスクロールさせない", async () => {
    window.history.replaceState(null, "", "/app/news/ev-1");
    renderSite();
    await screen.findByText(/出典 \(2\)/);
    expect(document.body.style.overflow).toBe("hidden");
  });
});
