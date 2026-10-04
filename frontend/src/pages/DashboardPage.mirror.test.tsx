// 写し (Cloudflare Pages) ダッシュボードのカスタマイズ契約。
//
// 写しは server を持たない静的コピーなので、カスタマイズの保存先は常に localStorage の
// `:mirror` 接尾辞キーのみ (layout API には一切 fetch しない)。読込時は registry の
// liveState/mirrorExcluded 印が付いた widget (または registry から消えた widget) を
// 落とす防御フィルタを通す。localStorage が例外を投げる環境 (private window 等) では
// 既定レイアウト (buildMirrorLayout) に黙って fallback する。

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { WidgetDef, WidgetProps } from "./dashboard/shared";
import { DashboardPage } from "./DashboardPage";

// 本物の registry は widgets/*.tsx 経由で実データを fetch する重量級の定義群なので、
// mirror customize の配管 (保存先・読込フィルタ) だけを確かめるのに不要な複雑さを
// 持ち込まないよう、3 種の軽量 stub (通常/liveState/mirrorExcluded) に差し替える。
function stubDef(title: string, overrides: Partial<WidgetDef> = {}): WidgetDef {
  return {
    title,
    Component: (_props: WidgetProps) => <div>{title}</div>,
    defaultSpan: 1,
    ...overrides,
  };
}

vi.mock("./dashboard/registry", () => ({
  WIDGET_REGISTRY: {
    normal: stubDef("通常widget"),
    live: stubDef("死活widget", { liveState: true }),
    excluded: stubDef("除外widget", { mirrorExcluded: true }),
  },
}));

const MOBILE_MIRROR_KEY = "cti.dashboard.layout.mobile:mirror";

function renderDashboard() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <DashboardPage />
    </QueryClientProvider>,
  );
}

// mobile 分岐 (1 列 content 駆動) にすると react-grid-layout / ResizeObserver を
// 介さずに widget の実描画を確かめられる (useIsMobile は mount 時の window.innerWidth を見る)。
function setMobileViewport(): void {
  Object.defineProperty(window, "innerWidth", { writable: true, configurable: true, value: 500 });
}
function setDesktopViewport(): void {
  Object.defineProperty(window, "innerWidth", { writable: true, configurable: true, value: 1024 });
}

describe("写しダッシュボードのカスタマイズ", () => {
  beforeEach(() => {
    localStorage.clear();
    setDesktopViewport();
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
    localStorage.clear();
    setDesktopViewport();
  });

  it("写しで保存すると :mirror 接尾辞キーに localStorage 保存し、layout API には fetch しない", async () => {
    // Arrange
    vi.stubEnv("VITE_MIRROR", "1");
    setMobileViewport();
    const fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy);
    renderDashboard();

    // Act: カスタマイズ → 保存 (mobile 下部バー)
    fireEvent.click(screen.getByRole("button", { name: /カスタマイズ/ }));
    fireEvent.click(screen.getByRole("button", { name: "保存" }));

    // Assert
    await waitFor(() => {
      expect(localStorage.getItem("cti.dashboard.layout.mobile:mirror")).not.toBeNull();
    });
    expect(localStorage.getItem("cti.dashboard.layout.mobile")).toBeNull();
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("保存済みの写しレイアウトを読み込むとき liveState/mirrorExcluded widget を落とす", async () => {
    // Arrange: 3 widget (通常/liveState/mirrorExcluded) を含む保存値を事前に置く
    vi.stubEnv("VITE_MIRROR", "1");
    setMobileViewport();
    localStorage.setItem(MOBILE_MIRROR_KEY, JSON.stringify({
      widgets: [
        { id: "normal", uid: "normal", x: 0, y: 0, w: 4, h: 3 },
        { id: "live", uid: "live", x: 0, y: 1, w: 4, h: 3 },
        { id: "excluded", uid: "excluded", x: 0, y: 2, w: 4, h: 3 },
      ],
    }));

    // Act
    renderDashboard();

    // Assert: 通常 widget のみ残り、liveState/mirrorExcluded は描画されない
    await waitFor(() => expect(screen.getByText("通常widget")).toBeTruthy());
    expect(screen.queryByText("死活widget")).toBeNull();
    expect(screen.queryByText("除外widget")).toBeNull();
  });

  it("localStorage が例外を投げる (private window 等) 場合は既定レイアウトに fallback する", async () => {
    // Arrange: getItem が常に投げる環境を模す
    vi.stubEnv("VITE_MIRROR", "1");
    setMobileViewport();
    const getItemSpy = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("private window: storage blocked");
    });

    // Act
    renderDashboard();

    // Assert: 例外で落ちず、既定レイアウト (liveState/mirrorExcluded を含まない) が出る
    await waitFor(() => expect(screen.getByText("通常widget")).toBeTruthy());
    expect(screen.queryByText("死活widget")).toBeNull();
    expect(screen.queryByText("除外widget")).toBeNull();

    getItemSpy.mockRestore();
  });

  it("運用 (非写し) ではキーが接尾辞無しのままで、カスタマイズ保存が従来どおり動く (比較対照)", async () => {
    // Arrange: 運用は server レイアウトの GET と WebSocket 接続が初回表示に要るので
    // jsdom に無い WebSocket を最小限スタブする (写しは useWebSocket が早期 return するため
    // 他の 3 ケースでは不要)。
    vi.stubEnv("VITE_MIRROR", "");
    setMobileViewport();
    vi.stubGlobal("fetch", vi.fn(async () =>
      new Response(JSON.stringify({ widgets: [] }), { headers: { "content-type": "application/json" } })));
    vi.stubGlobal("WebSocket", class {
      close(): void { /* noop */ }
    });
    renderDashboard();

    // Act
    await waitFor(() => expect(screen.getByRole("button", { name: /カスタマイズ/ })).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: /カスタマイズ/ }));
    fireEvent.click(screen.getByRole("button", { name: "保存" }));

    // Assert: 写し用キーではなく運用キーに書かれる
    await waitFor(() => {
      expect(localStorage.getItem("cti.dashboard.layout.mobile")).not.toBeNull();
    });
    expect(localStorage.getItem("cti.dashboard.layout.mobile:mirror")).toBeNull();
  });
});
