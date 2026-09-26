// ジョブの状態判定 (2026-09-26): 実行中は last_run ではなく running_since で判る。
import { describe, expect, it } from "vitest";
import type { JobView } from "../../api/jobs";
import { categoryForId, jobHealth, jobStatus } from "./categories";

function job(over: Partial<JobView>): JobView {
  return {
    id: "x", kind: "bespoke", title: "x", description: "", disable_impact: "",
    protection: "optional", enabled: true, schedule_type: "interval", hour: null, minute: null,
    day_of_week: null, day: null, interval_minutes: 60, offset_minutes: 0, debounce_hours: null,
    schedule_label: "", next_run_at: null, is_paused: false, last_run: null, danger_note: null,
    respects_analysis_window: false, heavy: false, max_runtime_minutes: 5,
    running_since: null, chain_id: null, running_step: null,
    ...over,
  };
}

describe("jobStatus / jobHealth", () => {
  it("走っている最中は前回の結果より実行中を優先する", () => {
    const j = job({
      running_since: "2026-09-26T01:00:00+00:00",
      last_run: { last_run_at: "2026-09-26T00:00:00+00:00", status: "succeeded", detail: "" },
    });
    expect(jobStatus(j)).toBe("running");
    expect(jobHealth(j)).toBe("running");
  });

  it("走っていなければ前回の結果", () => {
    const j = job({ last_run: { last_run_at: "2026-09-26T00:00:00+00:00", status: "failed", detail: "" } });
    expect(jobHealth(j)).toBe("failed");
  });

  it("実行記録が無ければ none", () => {
    expect(jobHealth(job({}))).toBe("none");
  });
});

describe("categoryForId", () => {
  it("毎時チェーンは収集と定常処理に入る (その他に落ちない)", () => {
    expect(categoryForId("hourly-collect").key).toBe("collect");
    expect(categoryForId("hourly-upkeep").key).toBe("upkeep");
    expect(categoryForId("ledger-reassess-hourly").key).toBe("upkeep");
  });
});
