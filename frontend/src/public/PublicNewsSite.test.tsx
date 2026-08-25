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
import mapSource from "./PublicMap.tsx?raw";

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
        // ⚠ /map は詳細の path にも前方一致するので **先に**判定する
        url.includes("/api/v1/public/news/map")
          ? {
              nodes: [{ iso: "JP", label: "日本", lat: 35.68, lon: 139.69, count: 12 }],
              window_days: 30,
              placed: 12,
              unplaced: 20,
              total: 32,
              note: "収集した報道の分布であり、世界全体の実態を示すものではありません。",
            }
          : url.includes("/api/v1/public/news/")
          ? { ...ITEM, bluf: "要点。", facts: [{ text: "事実行。", source_index: 1, paragraph: 1 }],
              discrepancies: [], unknowns: [], first_reported_at: ITEM.published_at,
              note: "kuebiko が生成した要約であり、原記事そのものではない" }
          : {
              // カテゴリ節は「先頭 1 本 + 見出しのみ 3 本」なので複数返す
              items: url.includes("category=")
                ? [ITEM, ...[1, 2, 3].map((n) => ({ ...ITEM, id: `ev-${n}`, headline: `見出し${n}` }))]
                : [ITEM],
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

  it("記事を開いている間は背後をスクロールさせない (ロックの持ち主は Drawer)", async () => {
    window.history.replaceState(null, "", "/app/news/ev-1");
    renderSite();
    await screen.findByText(/出典 \(2\)/);
    expect(document.body.style.overflow).toBe("hidden");
  });

  it("閉じたらスクロールが戻る", async () => {
    document.body.style.overflow = "";
    window.history.replaceState(null, "", "/app/news/ev-1");
    renderSite();
    await screen.findByText(/出典 \(2\)/);
    expect(document.body.style.overflow).toBe("hidden");

    // 閉じる = 履歴が戻って route が home になる
    window.history.replaceState(null, "", "/app/news");
    window.dispatchEvent(new PopStateEvent("popstate"));

    await waitFor(() => expect(document.body.style.overflow).not.toBe("hidden"));
  });
});

describe("地図", () => {
  it("ナビに地図を出す", async () => {
    renderSite();
    await screen.findByText(ITEM.headline);
    const hrefs = Array.from(document.querySelectorAll("nav a")).map((a) => a.getAttribute("href"));
    expect(hrefs).toContain("/app/news/map");
  });

  it("置けなかった件数を必ず併記する (収集網の観測≠世界)", async () => {
    window.history.replaceState(null, "", "/app/news/map");
    renderSite();
    // 母集団 32 件 / 置けた 12 件 / 置けなかった 20 件 をすべて示す
    expect(await screen.findByText(/32 件のうち/)).toBeTruthy();
    expect(screen.getByText(/12 件/)).toBeTruthy();
    expect(screen.getByText(/20 件は国を特定できず/)).toBeTruthy();
    expect(screen.getByText(/世界全体の実態を示すものではありません/)).toBeTruthy();
  });

  it("国を選ぶと記事一覧が絞り込まれる", async () => {
    window.history.replaceState(null, "", "/app/news/map");
    renderSite();
    const jp = await screen.findByText("日本");
    (jp.closest("button") as HTMLElement).click();
    await waitFor(() => expect(window.location.search).toContain("country=JP"));
  });
});

describe("PC のレイアウト", () => {
  it("本文幅を広げる (46rem では PC で左右が盛大に余っていた)", async () => {
    renderSite();
    await screen.findByText(ITEM.headline);
    const main = document.querySelector("main");
    expect(main?.className).toContain("max-w-[72rem]");
  });

  it("一覧は PC で 2 列にする", async () => {
    renderSite();
    await screen.findByText(ITEM.headline);
    const grid = Array.from(document.querySelectorAll("ul")).find((u) =>
      u.className.includes("md:grid-cols-2"),
    );
    expect(grid).toBeTruthy();
  });

  it("トップは各カテゴリの入口になっている (無限一覧にしない)", async () => {
    renderSite();
    await screen.findByText(ITEM.headline);
    // カテゴリごとの節見出しと「一覧へ」の導線
    // カテゴリ節は後から解決するので待つ
    await waitFor(() => expect(screen.getAllByText("一覧へ →").length).toBeGreaterThan(1));
    const links = Array.from(document.querySelectorAll("a")).map((a) => a.getAttribute("href"));
    expect(links).toContain("/app/news/c/vuln");
    expect(links).toContain("/app/news/latest");
  });

  it("節の見出しを記事見出しと区別できる形にする", async () => {
    renderSite();
    await waitFor(() => expect(screen.getAllByText("一覧へ →").length).toBeGreaterThan(1));
    // 節の見出しは色バーを伴い、記事見出しより太い
    const headings = Array.from(document.querySelectorAll("h2"));
    const section = headings.find((h) => h.textContent === "新着");
    expect(section?.className).toContain("font-bold");
    const bar = section?.previousElementSibling as HTMLElement | null;
    expect(bar?.getAttribute("aria-hidden")).toBe("true");
    expect(bar?.style.background).toBeTruthy();
  });

  it("カテゴリごとに色を変える (節のバーと記事バッジで同じ色を使う)", async () => {
    const { categoryColor } = await import("./categoryColors");
    expect(categoryColor("vuln")).not.toBe(categoryColor("threat"));
    expect(categoryColor("incident_breach")).not.toBe(categoryColor("geopolitical"));
    // 未知の分類は中立色 (勝手に色を割り当てない)
    const { CATEGORY_NEUTRAL } = await import("./categoryColors");
    expect(categoryColor("unknown-key")).toBe(CATEGORY_NEUTRAL);
  });

  it("面を持たせるのは分類の区画だけ (注目・新着まで箱にしない)", async () => {
    renderSite();
    await waitFor(() => expect(screen.getAllByText("一覧へ →").length).toBeGreaterThan(1));
    const boxed = Array.from(document.querySelectorAll("section")).filter((el) =>
      el.className.includes("bg-surface-2/40"),
    );
    const headings = boxed.map((el) => el.querySelector("h2")?.textContent);
    expect(headings).not.toContain("新着");
    expect(headings).not.toContain("注目");
    expect(boxed.length).toBeGreaterThan(0);
  });

  it("カテゴリ節の中ではカードのカテゴリバッジを出さない (節見出しと重複)", async () => {
    renderSite();
    await waitFor(() => expect(screen.getAllByText("一覧へ →").length).toBeGreaterThan(1));
    const headings = Array.from(document.querySelectorAll("h2")).map((h) => h.textContent);
    // 見出しに vuln があるのに、その節のカード内にバッジは出ない
    expect(headings).toContain("vuln");
    const badges = screen.getAllByText("vuln");
    // h2 の 1 つと、新着/注目カードのバッジのみ (カテゴリ節のカードには出ない)
    expect(badges.length).toBeLessThan(headings.length + 4);
  });

  it("カテゴリ節の見出し一覧を本文と区別できる形にする", async () => {
    // 同じ ITEM が head にも rest にも入る stub なので、見出しリスト側の要素で確認する
    renderSite();
    await waitFor(() => expect(screen.getAllByText("一覧へ →").length).toBeGreaterThan(1));

    const bullets = Array.from(document.querySelectorAll("li button")).filter((b) =>
      b.textContent?.includes("●"),
    );
    expect(bullets.length).toBeGreaterThan(0);
    const headline = bullets[0].querySelector("span:nth-of-type(2)");
    // ⚠ 先頭カードの要約と同じ text-fg-muted にしない (本文の続きに見える)
    expect(headline?.className).toContain("text-fg");
    expect(headline?.className).not.toContain("text-fg-muted");
    expect(headline?.className).toContain("font-medium");
  });

  it("被害国レールは出さない (PC で違和感があった)", async () => {
    renderSite();
    await screen.findByText(ITEM.headline);
    expect(document.querySelector("aside")).toBeNull();
  });
});


describe("記事本文の組み方", () => {
  const MULTI = {
    ...ITEM,
    bluf: "要点の文。",
    facts: [
      { text: "一段落目の一文目。", source_index: 1, paragraph: 1 },
      { text: "一段落目の二文目。", source_index: 0, paragraph: 1 },
      { text: "二段落目の一文目。", source_index: 2, paragraph: 2 },
    ],
    discrepancies: [{ text: "食い違いの点。", source_index: 1, paragraph: 0 }],
    unknowns: ["未確認の点。"],
    first_reported_at: ITEM.published_at,
    note: "kuebiko が生成した要約であり、原記事そのものではない",
  };

  function renderArticle(detail: object) {
    window.history.replaceState(null, "", "/app/news/ev-1");
    vi.stubGlobal("fetch", async (url: string) => ({
      ok: true,
      json: async () =>
        url.includes("/api/v1/public/news/map")
          ? { nodes: [], window_days: 30, placed: 0, unplaced: 0, total: 0, note: "" }
          : url.includes("/api/v1/public/news/")
            ? detail
            : { items: [], note: "", categories: [] },
    }));
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    return render(
      <QueryClientProvider client={qc}>
        <PublicNewsSite />
      </QueryClientProvider>,
    );
  }

  it("事実行を段落にまとめる (平坦な箇条書きにしない)", async () => {
    renderArticle(MULTI);
    await screen.findByText(/出典 \(2\)/);
    const paras = Array.from(document.querySelectorAll("p")).filter((el) =>
      el.className.includes("indent-[1em]"),
    );
    // paragraph は 1 と 2 の 2 つ → <p> も 2 つ
    expect(paras.length).toBe(2);
    expect(paras[0].textContent).toContain("一段落目の一文目。");
    expect(paras[0].textContent).toContain("一段落目の二文目。");
    expect(paras[1].textContent).toContain("二段落目の一文目。");
  });

  it("同じ段落の文をばらばらの行にしない", async () => {
    renderArticle(MULTI);
    await screen.findByText(/出典 \(2\)/);
    const first = Array.from(document.querySelectorAll("p")).find((el) =>
      el.textContent?.includes("一段落目の一文目。"),
    );
    // 同段落の 2 文が 1 つの <p> に入っていること
    expect(first?.textContent).toContain("一段落目の二文目。");
  });

  it("出典番号を本文中に残す", async () => {
    renderArticle(MULTI);
    await screen.findByText(/出典 \(2\)/);
    const sups = Array.from(document.querySelectorAll("sup")).map((s) => s.textContent);
    expect(sups).toContain("[1]");
    expect(sups).toContain("[2]");
  });

  it("要点を独立したボックスで先に見せる", async () => {
    renderArticle(MULTI);
    expect(await screen.findByText("要点")).toBeTruthy();
    expect(screen.getByText("要点の文。")).toBeTruthy();
  });

  it("読了目安を出す", async () => {
    renderArticle(MULTI);
    await screen.findByText(/出典 \(2\)/);
    expect(screen.getByText(/分で読めます/)).toBeTruthy();
  });
});

describe("地図の描画設定", () => {
  /**
   * jsdom では Leaflet の実描画を検証できないので、**壊れると必ず見える 2 点**が
   * ソースに残っていることを固定する。2026-08-25 に両方落として、
   * 「海が真っ白」「バブルが sticky ヘッダの上に描かれる」を実機で出した。
   */
  it("暗色の背景を当てる (既定のままだと海が真っ白になる)", () => {
    expect(mapSource).toContain('background: "#0a0e16"');
  });

  it("スタッキング文脈を作る (Leaflet の pane は z-index 400+ でヘッダを突き抜ける)", () => {
    expect(mapSource).toContain("relative z-0");
  });

  it("ホイールズームを切る (記事を読みながらの誤操作を防ぐ)", () => {
    expect(mapSource).toContain("scrollWheelZoom: false");
  });
});
