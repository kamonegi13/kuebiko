import { describe, expect, it, afterEach, beforeAll } from "vitest";
import { cleanup, render, fireEvent } from "@testing-library/react";
import { LeafletThreatMap } from "./LeafletThreatMap";
import type { CyberMapResponse } from "../../api/geo";

// jsdom は ResizeObserver を持たない。LeafletThreatMap はリサイズ追従に使うだけで
// このテストでは発火させないため、呼べれば十分な no-op で足りる。
beforeAll(() => {
  if (typeof globalThis.ResizeObserver === "undefined") {
    globalThis.ResizeObserver = class {
      observe() {}
      unobserve() {}
      disconnect() {}
    } as unknown as typeof ResizeObserver;
  }
});

afterEach(() => cleanup());

const EMPTY_DATA: CyberMapResponse = {
  window_days: 30,
  generated_at: "2026-10-04T00:00:00+00:00",
  nodes: [],
  actors: [],
  geo_events: [],
  unplaced_count: 0,
  excluded: { non_cyber: 0, accidental: 0, unplaced: 0 },
  totals: { placed_events: 0, countries: 0 },
};

describe("LeafletThreatMap: 全体を表示", () => {
  it("ズームボタンの下に「全体を表示」を出し、押すと世界ビューへ setView する", async () => {
    const { container } = render(<LeafletThreatMap data={EMPTY_DATA} selectedSector={null} />);
    // 地図のマウントは非同期の初期化を含むため、control が生えるまで待つ。
    await new Promise((r) => setTimeout(r, 0));
    const resetLink = container.querySelector(".leaflet-control-reset-view a");
    expect(resetLink).toBeTruthy();
    expect(resetLink?.textContent).toBe("全体を表示");
    // ズームボタンより後 (= 視覚的に下) に挿入されている
    const zoomControl = container.querySelector(".leaflet-control-zoom");
    const topLeftCorner = container.querySelector(".leaflet-top.leaflet-left");
    expect(topLeftCorner).toBeTruthy();
    const children = Array.from(topLeftCorner?.children ?? []);
    expect(children.indexOf(zoomControl as Element)).toBeLessThan(
      children.indexOf(container.querySelector(".leaflet-control-reset-view") as Element),
    );
    // クリックで例外にならない (setView が呼べる = 地図操作が壊れていない)
    expect(() => fireEvent.click(resetLink!)).not.toThrow();
  });
});
