import { describe, expect, it } from "vitest";
import type { KeyEvent } from "../../api/spotlight";
import { splitByRecency } from "./keyEvents";

const NOW = Date.parse("2026-09-29T00:00:00Z");

function ev(id: string, published_at: string): KeyEvent {
  return {
    article_id: id,
    title: id,
    url: "",
    feed_title: "",
    importance: "medium",
    published_at,
    source_basis: null as unknown as KeyEvent["source_basis"],
  };
}

describe("splitByRecency", () => {
  it("splits the last 24 hours from the rest of the 7-day window, newest first", () => {
    const events = [
      ev("old", "2026-09-24T00:00:00Z"),
      ev("new", "2026-09-28T12:00:00Z"),
      ev("newer", "2026-09-28T20:00:00Z"),
    ];

    const { recent, earlier } = splitByRecency(events, NOW);

    expect(recent.map((e) => e.article_id)).toEqual(["newer", "new"]);
    expect(earlier.map((e) => e.article_id)).toEqual(["old"]);
  });

  it("puts events without a date into the earlier group", () => {
    const { recent, earlier } = splitByRecency([ev("x", "")], NOW);

    expect(recent).toEqual([]);
    expect(earlier.map((e) => e.article_id)).toEqual(["x"]);
  });
});
