import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";
import { ModelAssignmentView, MODEL_HISTORY_HREF } from "./ModelAssignmentView";
import type { ModelStepRow } from "../../api/pages";

afterEach(cleanup);

const META = {
  fast: { label: "軽量 (fast)", description: "d", examples: "e" },
  narrative: { label: "散文生成 (narrative)", description: "d", examples: "e" },
};

const STEPS: ModelStepRow[] = [
  {
    step: "triage",
    tier: "fast",
    model: "gemma4:26b",
    tier_model: "gemma4:26b",
    source: "tier",
    is_external: false,
    differs_from_tier: false,
    inherits_from: null,
  },
  {
    step: "article_summary",
    tier: "fast",
    model: "kuebiko-sft:26b",
    tier_model: "gemma4:26b",
    source: "override",
    is_external: false,
    differs_from_tier: true,
    inherits_from: null,
  },
  {
    step: "event_news",
    tier: "narrative",
    model: "claudecode:sonnet",
    tier_model: "gemma4:31b",
    source: "override",
    is_external: true,
    differs_from_tier: true,
    inherits_from: null,
  },
];

function renderView() {
  return render(
    <ModelAssignmentView
      tiers={{ fast: "gemma4:26b", narrative: "gemma4:31b" }}
      meta={META}
      steps={STEPS}
      configVersion={42}
      configUpdatedAt="2026-10-24T01:02:03+00:00"
    />,
  );
}

describe("モデル割当の表示専用画面", () => {
  it("選択のプルダウンと保存のボタンを持たない", () => {
    renderView();

    expect(screen.queryAllByRole("combobox")).toHaveLength(0);
    expect(screen.queryByRole("button", { name: /保存/ })).toBeNull();
  });

  it("版番号と最終更新を出す", () => {
    renderView();

    expect(screen.getByText(/v42/)).toBeTruthy();
    expect(screen.getByText(/最終更新/)).toBeTruthy();
  });

  it("処理ごとの実際のモデルを一覧する", () => {
    renderView();

    const row = screen.getByTestId("step-article_summary");
    expect(within(row).getByText("kuebiko-sft:26b")).toBeTruthy();
  });

  it("外部モデルを使う処理に印を付ける", () => {
    renderView();

    expect(within(screen.getByTestId("step-event_news")).getByText("外部")).toBeTruthy();
    expect(within(screen.getByTestId("step-triage")).queryByText("外部")).toBeNull();
  });

  it("ティア割当と食い違う処理に印と割当値を出す", () => {
    renderView();

    const row = screen.getByTestId("step-article_summary");
    expect(within(row).getByText("上書き")).toBeTruthy();
    expect(within(row).getByText(/gemma4:26b/)).toBeTruthy();
    expect(within(screen.getByTestId("step-triage")).queryByText("上書き")).toBeNull();
  });

  it("緊急の戻しとして版履歴 (model_tiers) へのリンクを置く", () => {
    renderView();

    const link = screen.getByRole("link", { name: /版履歴/ });
    expect(link.getAttribute("href")).toBe(MODEL_HISTORY_HREF);
    expect(MODEL_HISTORY_HREF).toContain("key=model_tiers");
  });
});
