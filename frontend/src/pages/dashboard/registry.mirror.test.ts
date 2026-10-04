import { describe, expect, test } from "vitest";
import { WIDGET_REGISTRY } from "./registry";

// 写し (匿名の静的コピー) のダッシュボードは、registry から liveState / mirrorExcluded を除いた
// widget を固定レイアウトで並べる (DashboardPage.buildMirrorLayout が参照する)。印の付け忘れ /
// 付けすぎは写しに「いまの状態」や外したデータを出す・出すべき widget を消すことになる。
describe("ダッシュボード widget の写し用の印", () => {
  test("死活・直近の実行・システム状態は liveState", () => {
    expect(WIDGET_REGISTRY.health.liveState).toBe(true);
    expect(WIDGET_REGISTRY.recent_runs.liveState).toBe(true);
    expect(WIDGET_REGISTRY.status_strip.liveState).toBe(true);
  });

  test("購読ソースの一覧に依存するソース貢献度は mirrorExcluded", () => {
    expect(WIDGET_REGISTRY.source_contribution.mirrorExcluded).toBe(true);
    expect(WIDGET_REGISTRY.source_contribution.liveState).toBeFalsy();
  });

  test("写しに残る widget が 1 つ以上あり、印の付いたものを含まない", () => {
    const remaining = Object.entries(WIDGET_REGISTRY).filter(
      ([, def]) => !def.liveState && !def.mirrorExcluded,
    );
    const ids = remaining.map(([id]) => id);

    expect(ids.length).toBeGreaterThan(0);
    expect(ids).not.toContain("health");
    expect(ids).not.toContain("source_contribution");
  });
});
