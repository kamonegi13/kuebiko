/**
 * 公開ニュースサイトの表示契約。
 *
 * 匿名の第三者が見る唯一の画面なので、「出典が常に見えること」と「分析者向けの
 * 導線が出ないこと」を固定する。
 */
import { describe, expect, it, vi, afterEach, beforeEach } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { PublicNewsSite } from "./PublicNewsSite";
import mapSource from "./PublicMap.tsx?raw";
import siteSource from "./PublicNewsSite.tsx?raw";
import tailwindSource from "../../tailwind.config.ts?raw";
import indexHtml from "../../public.html?raw";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";


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
    {
      index: 1,
      title: "原記事タイトル",
      url: "https://example.test/a",
      source: "Example News",
      source_tier: "news",
      published_at: null,
    },
    {
      index: 2,
      title: "別媒体の記事",
      url: "https://example.test/b",
      source: "Other Wire",
      source_tier: "news",
      published_at: null,
    },
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
              nodes: [
                {
                  iso: "JP",
                  label: "日本",
                  lat: 35.68,
                  lon: 139.69,
                  count: 12,
                },
              ],
              window_days: 30,
              placed: 12,
              unplaced: 20,
              total: 32,
              note: "収集した報道の分布であり、世界全体の実態を示すものではありません。",
            }
          : url.includes("/api/v1/public/news/")
            ? {
                ...ITEM,
                bluf: "要点。",
                facts: [{ text: "事実行。", source_index: 1, paragraph: 1 }],
                discrepancies: [],
                unknowns: [],
                first_reported_at: ITEM.published_at,
                note: "kuebiko が生成した要約であり、原記事そのものではない",
              }
            : {
                // カテゴリ節は「先頭 1 本 + 見出しのみ 3 本」なので複数返す
                items: url.includes("category=")
                  ? [
                      ITEM,
                      ...[1, 2, 3].map((n) => ({
                        ...ITEM,
                        id: `ev-${n}`,
                        headline: `見出し${n}`,
                      })),
                    ]
                  : [ITEM],
                note: "kuebiko が生成した要約であり、原記事そのものではない",
                categories: [
                  "vuln",
                  "incident_breach",
                  "threat",
                  "geopolitical",
                ],
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
  it("先頭記事はカテゴリ・見出し・要約・日付を出す", async () => {
    renderSite();
    expect(await screen.findByText(ITEM.headline)).toBeTruthy();
    expect(screen.getAllByText(ITEM.summary).length).toBeGreaterThan(0);
    // カテゴリバッジ (語彙が無い環境では key がそのまま出る)
    expect(screen.getAllByText("vuln").length).toBeGreaterThan(0);
  });

  it("要約は画面幅で出し分ける (モバイルは見出しのみ・PC は要約あり)", () => {
    // 制約が縦か横かで最適が逆になる。モバイルは縦が足りないので見出しだけ、
    // PC は 2 列で横に広く、見出しだけだとカードが間延びする (2026-08-28 利用者指摘)。
    const card = siteSource.slice(
      siteSource.indexOf("function NewsCard("),
      siteSource.indexOf("function NewsCard(") + 2600,
    );
    expect(card).toContain("{item.headline}");
    // 要約は出すが、段数が変わる境目 (md) より下では隠す
    const summaryLine = card.slice(card.indexOf("{item.summary}") - 400, card.indexOf("{item.summary}"));
    expect(summaryLine).toContain("hidden md:block");
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
    const links = Array.from(document.querySelectorAll("a")).map((a) =>
      a.getAttribute("href"),
    );
    // 着地点は /auth/login。/auth/ はアプリにルートが無く、認証通過後に 404 になる
    expect(links).toContain("/auth/login");
    // 分析画面への導線を出さない
    for (const analyst of [
      "/app/dashboard",
      "/app/intel/synthesis",
      "/app/config",
      "/app/pir",
    ]) {
      expect(links).not.toContain(analyst);
    }
  });

  it("記事を開くと出典が番号付きで全件並ぶ", async () => {
    window.history.replaceState(null, "", "/app/news/ev-1");
    renderSite();
    expect(await screen.findByText(/出典 \(2\)/)).toBeTruthy();
    const external = Array.from(
      document.querySelectorAll('a[target="_blank"]'),
    );
    expect(external.length).toBe(2);
    // 原記事へは外部リンク。rel を落とすと参照元が漏れる
    for (const a of external)
      expect(a.getAttribute("rel")).toContain("noopener");
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
    const tabs = Array.from(document.querySelectorAll("nav a")).map((a) =>
      a.getAttribute("href"),
    );
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
    await waitFor(() =>
      expect(requested.some((u) => u.includes("featured=true"))).toBe(true),
    );
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
    const close = document.querySelector(
      '[aria-label="閉じる"], button[title="閉じる"]',
    );
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

    await waitFor(() =>
      expect(document.body.style.overflow).not.toBe("hidden"),
    );
  });
});

describe("地図", () => {
  it("ナビに地図を出す", async () => {
    renderSite();
    await screen.findByText(ITEM.headline);
    const hrefs = Array.from(document.querySelectorAll("nav a")).map((a) =>
      a.getAttribute("href"),
    );
    expect(hrefs).toContain("/app/news/map");
  });

  it("置けなかった件数を必ず併記する (収集網の観測≠世界)", async () => {
    window.history.replaceState(null, "", "/app/news/map");
    renderSite();
    // 母集団 32 件 / 置けた 12 件 / 置けなかった 20 件 をすべて示す
    expect(await screen.findByText(/32 件のうち/)).toBeTruthy();
    expect(screen.getByText(/12 件/)).toBeTruthy();
    expect(screen.getByText(/20 件は国を特定できず/)).toBeTruthy();
    expect(
      screen.getByText(/世界全体の実態を示すものではありません/),
    ).toBeTruthy();
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
    await waitFor(() =>
      expect(screen.getAllByText("一覧へ →").length).toBeGreaterThan(1),
    );
    const links = Array.from(document.querySelectorAll("a")).map((a) =>
      a.getAttribute("href"),
    );
    expect(links).toContain("/app/news/c/vuln");
    expect(links).toContain("/app/news/latest");
  });

  it("節の見出しを記事見出しと区別できる形にする", async () => {
    renderSite();
    await waitFor(() =>
      expect(screen.getAllByText("一覧へ →").length).toBeGreaterThan(1),
    );
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
    expect(categoryColor("incident_breach")).not.toBe(
      categoryColor("geopolitical"),
    );
    // 未知の分類は中立色 (勝手に色を割り当てない)
    const { CATEGORY_NEUTRAL } = await import("./categoryColors");
    expect(categoryColor("unknown-key")).toBe(CATEGORY_NEUTRAL);
  });

  it("面を持たせるのは分類の区画だけ (注目・新着まで箱にしない)", async () => {
    renderSite();
    await waitFor(() =>
      expect(screen.getAllByText("一覧へ →").length).toBeGreaterThan(1),
    );
    const boxed = Array.from(document.querySelectorAll("section")).filter(
      (el) => el.className.includes("bg-surface-2/40"),
    );
    const headings = boxed.map((el) => el.querySelector("h2")?.textContent);
    expect(headings).not.toContain("新着");
    expect(headings).not.toContain("注目");
    expect(boxed.length).toBeGreaterThan(0);
  });

  it("カテゴリ節の中ではカードのカテゴリバッジを出さない (節見出しと重複)", async () => {
    renderSite();
    await waitFor(() =>
      expect(screen.getAllByText("一覧へ →").length).toBeGreaterThan(1),
    );
    const headings = Array.from(document.querySelectorAll("h2")).map(
      (h) => h.textContent,
    );
    // 見出しに vuln があるのに、その節のカード内にバッジは出ない
    expect(headings).toContain("vuln");
    const badges = screen.getAllByText("vuln");
    // h2 の 1 つと、新着/注目カードのバッジのみ (カテゴリ節のカードには出ない)
    expect(badges.length).toBeLessThan(headings.length + 4);
  });

  it("カテゴリ節の見出し一覧を本文と区別できる形にする", async () => {
    // 同じ ITEM が head にも rest にも入る stub なので、見出しリスト側の要素で確認する
    renderSite();
    await waitFor(() =>
      expect(screen.getAllByText("一覧へ →").length).toBeGreaterThan(1),
    );

    // 行頭記号は置かない (罫線が区切るので記号は繰り返しがくどいだけ、
    // 2026-08-28 利用者指摘)。区別は **色と太さ** で付ける。
    const rows = Array.from(document.querySelectorAll("li button"));
    expect(rows.length).toBeGreaterThan(0);
    expect(rows.some((b) => b.textContent?.includes("●"))).toBe(false);
    const headline = rows[0].querySelector("span");
    expect(headline?.className).toContain("text-fg");
    expect(headline?.className).not.toContain("text-fg-muted");
    expect(headline?.className).toContain("font-medium");
  });

  it("カード自身は li を作らない (裸の li がブラウザ既定の ● を出していた)", async () => {
    renderSite();
    await waitFor(() =>
      expect(screen.getAllByText("一覧へ →").length).toBeGreaterThan(1),
    );
    // カテゴリ節の先頭記事は <ul> の外に置くため、li だと勝手にマーカーが付く
    const strayLi = Array.from(document.querySelectorAll("li")).filter(
      (el) =>
        el.parentElement && !["UL", "OL"].includes(el.parentElement.tagName),
    );
    expect(strayLi).toEqual([]);
  });

  it("hover の色替えを hover 可能な端末に限る (モバイルでタップ後に残る)", () => {
    // 実機 (iPhone) で 2 件目だけ青く残っていた。:hover はタップ後も維持されるため。
    expect(siteSource).not.toMatch(
      /(?<!hover:hover\)\]:)group-hover:text-accent/,
    );
    expect(siteSource).toContain(
      "[@media(hover:hover)]:group-hover:text-accent",
    );
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
          ? {
              nodes: [],
              window_days: 30,
              placed: 0,
              unplaced: 0,
              total: 0,
              note: "",
            }
          : url.includes("/api/v1/public/news/")
            ? detail
            : { items: [], note: "", categories: [] },
    }));
    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    return render(
      <QueryClientProvider client={qc}>
        <PublicNewsSite />
      </QueryClientProvider>,
    );
  }

  it("要点があれば要約とは別に箇条書きで出す", async () => {
    renderArticle({ ...MULTI, key_points: ["要点その一。", "要点その二。"] });
    await screen.findByText("要点");
    const items = Array.from(document.querySelectorAll("li")).map(
      (el) => el.textContent,
    );
    expect(items).toContain("要点その一。");
    expect(items).toContain("要点その二。");
    // 要約 (散文) は別枠のまま — 要点と要約は別物 (2026-08-25 利用者指摘)
    expect(screen.getByText("要約")).toBeTruthy();
  });

  it("要点を持たない過去の版でも落ちない", async () => {
    // 2026-08-26 より前の 542 版は key_points をキーごと持たない
    renderArticle(MULTI);
    await screen.findByText(/出典 \(2\)/);
    expect(screen.queryByText("要点")).toBeNull();
  });

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

  it("出典が複数ある記事は本文中に番号を残す", async () => {
    renderArticle(MULTI);
    await screen.findByText(/出典 \(2\)/);
    const sups = Array.from(document.querySelectorAll("sup")).map(
      (s) => s.textContent,
    );
    expect(sups).toContain("[1]");
    expect(sups).toContain("[2]");
  });

  it("出典が 1 件の記事は番号を出さない (全部 [1] で情報を持たない)", async () => {
    const solo = {
      ...MULTI,
      citations: [MULTI.citations[0]],
      facts: [{ text: "単独報の一文。", source_index: 1, paragraph: 1 }],
      discrepancies: [],
    };
    renderArticle(solo);
    // 見出しは「出典」のみ (件数を括弧で出さない)
    expect(await screen.findByText("出典")).toBeTruthy();
    expect(document.querySelectorAll("sup").length).toBe(0);
    expect(screen.queryByText("[1]")).toBeNull();
  });

  it("冒頭の要約を独立したボックスで先に見せる", async () => {
    renderArticle(MULTI);
    expect(await screen.findByText("要約")).toBeTruthy();
    expect(screen.getByText("要点の文。")).toBeTruthy();
  });

  it("要約 (散文) と要点 (箇条書き) を同じ語で呼ばない", async () => {
    // 散文の BLUF を「要点」と呼ばないこと。**要点と要約は別物** (2026-08-25 利用者指摘)。
    // サイトの他の箇所 (フッタ・注記) も「生成した要約」で統一している。
    renderArticle({ ...MULTI, key_points: ["要点その一。"] });
    await screen.findByText("要点");
    const summary = screen.getByText("要約");
    const points = screen.getByText("要点");
    expect(summary.nextElementSibling?.textContent).toBe(MULTI.bluf);
    expect(points.parentElement?.querySelectorAll("li").length).toBe(1);
  });

  it("読了目安を出す", async () => {
    renderArticle(MULTI);
    await screen.findByText(/出典 \(2\)/);
    expect(screen.getByText(/分で読めます/)).toBeTruthy();
  });
});

describe("ヘッダ", () => {
  it("追従するのはナビだけ (題字はスクロールで流す)", async () => {
    renderSite();
    await screen.findByText(ITEM.headline);
    const header = document.querySelector("header");
    // 題字の行は sticky にしない
    expect(header?.className).not.toContain("sticky");
    // ナビを包む要素が sticky top-0
    const nav = document.querySelector("nav");
    const bar = nav?.closest("div.sticky");
    expect(bar).toBeTruthy();
    expect(bar?.className).toContain("top-0");
  });

  it("追従時にタブが上端へ貼り付かない (上余白を取る)", async () => {
    renderSite();
    await screen.findByText(ITEM.headline);
    const inner = document.querySelector("nav")?.parentElement;
    expect(inner?.className).toContain("pt-3");
  });

  it("ナビは折り返さない (flex-wrap と overflow-x-auto の併用で縦バーが出る)", async () => {
    renderSite();
    await screen.findByText(ITEM.headline);
    const nav = document.querySelector("nav");
    expect(nav?.className).toContain("flex-nowrap");
    expect(nav?.className).not.toContain("flex-wrap");
    // 片方の軸が visible でなくなるともう一方も auto になるため、縦は明示で hidden
    expect(nav?.className).toContain("overflow-y-hidden");
  });

  it("ナビのスクロールバーを隠す (横スクロールは指で行う)", async () => {
    renderSite();
    await screen.findByText(ITEM.headline);
    const nav = document.querySelector("nav");
    expect(nav?.className).toContain("[scrollbar-width:none]");
  });

  it("ナビは地図より前面に出る (Leaflet の pane は z-index 400+)", async () => {
    renderSite();
    await screen.findByText(ITEM.headline);
    const bar = document.querySelector("nav")?.closest("div.sticky");
    expect(bar?.className).toContain("z-20");
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

describe("続報バッジ", () => {
  // 一覧の日付は最終報なので、続報が付いた事象は再浮上する。初報と見分ける手段が
  // バッジしかないため、「本文が書き直された」と「媒体が増えただけ」を混ぜない
  async function renderWith(patch: Record<string, unknown>) {
    vi.stubGlobal("fetch", async (url: string) => ({
      ok: true,
      json: async () =>
        url.includes("/api/v1/public/news/map")
          ? { nodes: [], window_days: 30, placed: 0, unplaced: 0, total: 0, note: "" }
          : url.includes("/api/v1/public/news/")
            ? {
                ...ITEM,
                ...patch,
                bluf: "要点。",
                facts: [{ text: "事実行。", source_index: 1, paragraph: 1 }],
                discrepancies: [],
                unknowns: [],
                note: "",
              }
            : { items: [{ ...ITEM, ...patch }], note: "", categories: [] },
    }));
    renderSite();
    await screen.findByText(ITEM.headline);
  }

  it("本文を書き直した事象にだけ「更新」を出す", async () => {
    await renderWith({
      update_kind: "rewritten",
      first_reported_at: "2026-08-25T00:00:00+00:00",
    });
    expect(screen.getAllByText("更新").length).toBeGreaterThan(0);
    expect(screen.queryByText("続報")).toBeNull();
  });

  it("媒体が増えただけなら「続報」— 中身が変わっていないのに更新と言わない", async () => {
    await renderWith({
      update_kind: "follow_up",
      follow_up_sources: 2,
      first_reported_at: "2026-08-25T00:00:00+00:00",
    });
    expect(screen.getByText("続報")).toBeTruthy();
    expect(screen.queryByText("更新")).toBeNull();
  });

  it("初報のままならバッジを出さない", async () => {
    await renderWith({ update_kind: null });
    expect(screen.queryByText("更新")).toBeNull();
    expect(screen.queryByText("続報")).toBeNull();
    expect(screen.queryByText(/初報/)).toBeNull();
  });

  it("更新の経緯は「加わった要素」で示す (本文の行差分ではない)", async () => {
    await renderWith({
      update_kind: "rewritten",
      first_reported_at: "2026-08-25T00:00:00+00:00",
      revisions: [
        {
          at: "2026-08-27T01:00:00+00:00",
          added: [{ type: "victim_org", label: "被害組織", values: ["Federal Reserve Board"] }],
          note: "",
          source: "Some Wire",
          url: "https://example.test/c",
        },
      ],
    });
    fireEvent.click(screen.getByText(ITEM.headline));
    expect(await screen.findByText("この記事の更新")).toBeTruthy();
    expect(screen.getByText("Federal Reserve Board")).toBeTruthy();
  });
});

describe("ドロワーの背景", () => {
  it("一覧から記事を開いても背景は同じ一覧のまま (先頭に戻さない)", () => {
    // 2026-08-27 利用者報告: ドロワーを出すと背景が一番上に戻り、閉じると位置が
    // 復帰する。実体はスクロールロックではなく **背景の取り違え** —
    // isPortal() が URL から推測していたため、新着/カテゴリ一覧から開くと背景が
    // トップページに差し替わり、別コンポーネントとして先頭から描き直されていた。
    expect(siteSource).toContain("backdropRef");
    expect(siteSource).toContain("isPortal(route, backdrop)");
    // 一覧の絞り込みも引き継ぐ (カテゴリ一覧から開いたらそのカテゴリのまま)
    expect(siteSource).toMatch(/backdrop\?\.kind === "category"/);
  });
});

describe("開いたままの自動更新", () => {
  it("トップページも一覧と同じ間隔で更新する", () => {
    // 主導線であるトップが古いまま残ると、更新されていないサイトに見える
    // (2026-08-26 利用者指摘)。間隔の定義は 1 か所に置く。
    const intervals = siteSource.match(/refetchInterval:\s*([^,\n]+)/g) ?? [];
    expect(intervals.length).toBeGreaterThanOrEqual(5);
    expect(new Set(intervals.map((s) => s.trim())).size).toBe(1);
  });
});

describe("可読性の下限", () => {
  // 2026-08-28 の可読性診断で直した値が、後の編集で静かに戻らないようにする。
  // 基準: 本文 16px 以上 / 表示文字は 12px 以上 / 二次色は WCAG AA (4.5:1)。
  const contrastRgb = (fg: readonly number[], bg: readonly number[]) => {
    const lum = (c: readonly number[]) => {
      const f = (x: number) => {
        const s = x / 255;
        return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
      };
      return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2]);
    };
    const [a, b] = [lum(fg), lum(bg)].sort((x, y) => y - x);
    return (a + 0.05) / (b + 0.05);
  };

  it("公開面に 12px 未満の文字を置かない", () => {
    const sizes = [...siteSource.matchAll(/text-\[([0-9.]+)px\]/g)].map((m) => Number(m[1]));
    expect(sizes.length).toBeGreaterThan(10);
    expect(sizes.filter((n) => n < 12)).toEqual([]);
  });

  it("記事本文は推奨帯 (16px 以上) にある", () => {
    // 本文段落と要点。読み物なので密度より可読性を優先する
    expect(siteSource).toMatch(/text-\[16\.5px\][^"]*indent-\[1em\]/);
    expect(siteSource).toMatch(/text-\[17px\][^"]*text-fg/);
  });

  it.each([
    ["ダーク", /:root\s*\{([^}]*)\}/],
    ["ライト", /\[data-theme="light"\]\s*\{([^}]*)\}/],
  ])("%s配色の文字色が AA を満たす", (_name, re) => {
    // 明暗どちらの palette でも、本文・副文・メタが背景と面の両方で 4.5:1 以上あること。
    // 片方だけ直して他方を割る、が起きやすいのでテーマごとに測る。
    // ⚠ CSS を `?raw` で import すると Vite の CSS プラグインが横取りして
    //    空文字になる (2026-08-28 実測)。ファイルとして読む。
    const cssSource = readFileSync(resolve(process.cwd(), "src/index.css"), "utf8");
    const block = (cssSource.match(re) ?? [])[1] ?? "";
    const v = (name: string) => {
      const m = block.match(new RegExp(`--c-${name}:\\s*([0-9]+) ([0-9]+) ([0-9]+)`));
      expect(m, `--c-${name} が見つからない`).toBeTruthy();
      return [Number(m![1]), Number(m![2]), Number(m![3])] as const;
    };
    const grounds = [v("bg"), v("surface-1"), v("surface-2")];
    for (const fgName of ["fg", "fg-muted", "fg-subtle"]) {
      for (const ground of grounds) {
        expect(contrastRgb(v(fgName), ground)).toBeGreaterThanOrEqual(4.5);
      }
    }
  });

  it("見出しの行頭はバッジの有無で動かない", () => {
    // バッジを見出しの前に置くと、付いている行だけ開始位置が右へずれる。
    // 密な一覧では行頭が揃っていることが拾い読みの前提なので、
    // バッジは日付と同じ右の列に積む (2026-08-28 利用者指摘)。
    const row = siteSource.slice(
      siteSource.indexOf("{rest.map("),
      siteSource.indexOf("{rest.map(") + 2600,
    );
    const headlineAt = row.indexOf("{it.headline}");
    const badgeAt = row.indexOf("更新<");
    expect(headlineAt).toBeGreaterThan(0);
    expect(badgeAt).toBeGreaterThan(headlineAt);
  });

  it("読ませる枠の見出しは省略しない", () => {
    // カード (item) と先頭記事は、見出しが唯一の情報になる場所。省略記号で
    // 消すと何の記事か分からない。3 行で止めていたときは 63% が切れていた。
    const sites = [...siteSource.matchAll(/\{item\.headline\}/g)];
    expect(sites.length).toBeGreaterThanOrEqual(2);
    for (const m of sites) {
      const before = siteSource.slice(Math.max(0, m.index! - 300), m.index!);
      expect(before, `見出しが省略されている: …${before.slice(-90)}`).not.toMatch(/line-clamp-\d/);
    }
  });

  it("入口の密な一覧だけは 2 行で揃える", () => {
    // ここは「一覧へ」で全件が見える入口。見出しを切ってでも行位置を揃える方を
    // 採る (2026-08-28 利用者判断)。左右 2 列で行がずれると格子が崩れて見える。
    const at = siteSource.indexOf("{it.headline}");
    expect(at).toBeGreaterThan(0);
    const before = siteSource.slice(at - 400, at);
    expect(before).toMatch(/line-clamp-2/);
  });

  it("公開サイトの既定はライト、運用画面の既定はダーク", () => {
    // ニュースは日中の屋外でも読まれるので明るい面を標準にする。
    // 運用画面は分析作業を暗い面で設計しているのでダークのまま。
    const ops = readFileSync(resolve(process.cwd(), "index.html"), "utf8");
    expect(indexHtml).toMatch(/prefersDark \? "dark" : "light"/);
    expect(ops).toMatch(/saved \|\| "dark"/);
    // どちらも描画前に属性を立てる (React 待ちだと初回に別配色が一瞬出る)
    expect(indexHtml).toContain("setAttribute(\"data-theme\"");
    expect(ops).toContain("setAttribute(\"data-theme\"");
  });

  it("和文の Web フォントを読み込んでいる", () => {
    // 無いと和文は OS 任せになり、閲覧環境ごとに別の書体で出る
    expect(indexHtml).toContain("Noto+Sans+JP");
    expect(tailwindSource).toContain('"Noto Sans JP"');
  });
});

describe("運用者ログインの導線", () => {
  it("着地点は /auth/login (/auth/ は認証後に 404 になる)", () => {
    // Cloudflare Access は /auth/* を保護するが、アプリに /auth/ のルートは無い。
    // 認証を通過した直後に 404 が出る (2026-08-26 実測)。
    expect(siteSource).toContain("/auth/login");
    expect(siteSource).not.toContain('href="/auth/"');
  });

  it("運用画面のオリジンをビルド時に差し込める", () => {
    // 静的配信 (Pages) には運用画面が無いので、tunnel 側のホストを指す必要がある
    expect(siteSource).toContain("VITE_OPERATOR_ORIGIN");
  });
});
