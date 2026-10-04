import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { DashboardSelectionProvider } from "../DashboardSelection";
import { GeoRankingWidget, MiniMapWidget } from "./situation_geo";
import { fetchCountryArticles, fetchCyberMap } from "../../../api/geo";

// 実 Leaflet は jsdom で検証しない (地図描画は別関心)。MiniMapWidget の対象は
// 「選択を渡す/受け取る配線とオーバーレイ表示」なので、クリック可能な最小スタブに差し替える。
vi.mock("../../../components/geo/LeafletThreatMap", () => ({
  LeafletThreatMap: ({ onCountryClick, highlightIso }: {
    onCountryClick?: (iso: string, domain: "cyber" | "geopolitical") => void;
    highlightIso?: string | null;
  }) => (
    <button onClick={() => onCountryClick?.("JP", "cyber")} data-testid="map-stub">
      map (highlight={highlightIso ?? "none"})
    </button>
  ),
}));

vi.mock("../../../api/geo", async () => {
  const actual = await vi.importActual<typeof import("../../../api/geo")>("../../../api/geo");
  return {
    ...actual,
    fetchCyberMap: vi.fn(),
    fetchCountryArticles: vi.fn(),
  };
});

const MAP_DATA = {
  window_days: 7,
  generated_at: "2026-10-01T00:00:00Z",
  nodes: [
    { iso: "JP", label: "日本", lat: 35, lon: 139, count: 3, precision: "country",
      top_sector: null, sectors: [], top_intent: null, intents: [], intent_count: 0,
      source_count: 2, top_source_share: 0.5, top_source: "a", posted_count: 3, collected_count: 0,
      confidence: "medium" },
    { iso: "US", label: "米国", lat: 38, lon: -97, count: 1, precision: "country",
      top_sector: null, sectors: [], top_intent: null, intents: [], intent_count: 0,
      source_count: 1, top_source_share: 1, top_source: "b", posted_count: 1, collected_count: 0,
      confidence: "thin" },
  ],
  actors: [],
  geo_events: [],
  unplaced_count: 0,
  excluded: { non_cyber: 0, accidental: 0, unplaced: 0 },
  totals: { placed_events: 4, countries: 2 },
};

const COUNTRY_DATA = {
  iso: "JP",
  label: "日本",
  count: 1,
  articles: [
    { article_id: "a1", title: "日本を標的とした事例", url: "https://example.com/a1",
      importance: "high", category: "incident", victim_sector: null, victim_sector_label: null,
      socio_political_intent: null, intent_confidence: null, status: "posted", created_at: "2026-10-01T00:00:00Z" },
  ],
};

function renderWithProviders(children: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <DashboardSelectionProvider>{children}</DashboardSelectionProvider>
    </QueryClientProvider>,
  );
}

describe("mini_map / geo_ranking widget の国選択連動", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.mocked(fetchCyberMap).mockResolvedValue(MAP_DATA as never);
    vi.mocked(fetchCountryArticles).mockResolvedValue(COUNTRY_DATA as never);
  });

  afterEach(() => {
    cleanup();
    localStorage.clear();
  });

  it("地図で国を選ぶと、同じ dashboard のランキング widget が記事一覧に切り替わる", async () => {
    renderWithProviders(
      <>
        <MiniMapWidget />
        <GeoRankingWidget />
      </>,
    );
    await waitFor(() => expect(screen.getByTestId("map-stub")).toBeTruthy());

    fireEvent.click(screen.getByTestId("map-stub"));

    // mini_map (自身のオーバーレイ) と geo_ranking (一覧の差し替え) の両方に記事が出るため
    // getAllByText で複数件を許容する。
    await waitFor(() => expect(screen.getAllByText("日本を標的とした事例").length).toBeGreaterThan(0));
    // 地図側も選択を受け取っている (highlightIso が反映)
    expect(screen.getByTestId("map-stub").textContent).toContain("highlight=JP");
  });

  it("ランキングで国を選んでも同じ記事一覧が出て、閉じる (×) で一覧に戻る", async () => {
    renderWithProviders(<GeoRankingWidget />);
    await waitFor(() => expect(screen.getByText("日本")).toBeTruthy());

    fireEvent.click(screen.getByText("日本"));
    await waitFor(() => expect(screen.getByText("日本を標的とした事例")).toBeTruthy());

    fireEvent.click(screen.getByTitle("閉じる (選択を解除)"));
    await waitFor(() => expect(screen.getByText("日本")).toBeTruthy());
    expect(screen.queryByText("日本を標的とした事例")).toBeNull();
  });

  it("単独配置の mini_map widget でも、選択すると自身のオーバーレイに記事一覧が出る", async () => {
    renderWithProviders(<MiniMapWidget />);
    await waitFor(() => expect(screen.getByTestId("map-stub")).toBeTruthy());

    fireEvent.click(screen.getByTestId("map-stub"));

    await waitFor(() => expect(screen.getByText("日本を標的とした事例")).toBeTruthy());
  });
});
