import { describe, expect, it } from "vitest";
import {
  buildEventNewsHref, eventNewsQueryFromFilters, resolveEventNewsFilters,
} from "./eventnews";

describe("resolveEventNewsFilters (widget 保存設定 → 絞り込み値)", () => {
  it("記事フィード widget と同じ config key を読む (category/feed/channel/jp/since_hours/mode)", () => {
    const f = resolveEventNewsFilters({
      category: "vulnerability",
      feed: "JPCERT/CC",
      channel: "alert",
      jp: "targeted_affected",
      since_hours: "72",
      mode: "summary",
      per: "4",
    });
    expect(f.category).toBe("vulnerability");
    expect(f.feed).toBe("JPCERT/CC");
    expect(f.channel).toBe("alert");
    expect(f.jp).toBe("targeted_affected");
    expect(f.sinceHours).toBe(72);
    expect(f.wantSummary).toBe(true);
    expect(f.per).toBe(4);
  });

  it("mode 未指定の既定は見出しのみ (旧挙動を変えない)", () => {
    expect(resolveEventNewsFilters({}).wantSummary).toBe(false);
  });

  it("config 未指定でも widget 既定の深刻さ (注意以上+政策地政学) に落ちる", () => {
    const f = resolveEventNewsFilters(undefined);
    expect(f.severity).toEqual({ minSeverity: "S2", relevantOnly: false, includeStrategic: true });
  });
});

describe("eventNewsQueryFromFilters (絞り込み値 → fetchEventNews のクエリ)", () => {
  it("category/feed/channel/jp/since_hours/深刻さ facet を渡す", () => {
    const q = eventNewsQueryFromFilters({
      category: "malware", feed: "Security NEXT", channel: "alert", jp: "mentioned",
      per: 6, sinceHours: 24, wantSummary: false,
      severity: { minSeverity: "S3", relevantOnly: true, includeStrategic: false },
    });
    expect(q.category).toBe("malware");
    expect(q.feed).toBe("Security NEXT");
    expect(q.channel).toBe("alert");
    expect(q.jp).toBe("mentioned");
    expect(q.since_hours).toBe(24);
    expect(q.min_severity).toBe("S3");
    expect(q.relevant_only).toBe(true);
    expect(q.limit).toBe(20); // per*2 と 20 の大きい方
  });

  it("空の絞り込みは undefined で送る (backend 既定 = 絞らない を壊さない)", () => {
    const q = eventNewsQueryFromFilters({
      category: "", feed: "", channel: "", jp: "", per: 6, sinceHours: 0, wantSummary: false,
      severity: { minSeverity: "", relevantOnly: false, includeStrategic: false },
    });
    expect(q.category).toBeUndefined();
    expect(q.feed).toBeUndefined();
    expect(q.channel).toBeUndefined();
    expect(q.jp).toBeUndefined();
    expect(q.since_hours).toBeUndefined();
    expect(q.min_severity).toBeUndefined();
  });
});

describe("buildEventNewsHref (ヘッダーの deep-link)", () => {
  it("絞り込みなしは素の /app/eventnews", () => {
    expect(buildEventNewsHref({
      category: "", feed: "", channel: "",
      severity: { minSeverity: "", relevantOnly: false, includeStrategic: false },
      jp: "", sinceHours: 0,
    })).toBe("/app/eventnews");
  });

  it("絞り込みをそのまま引き継ぐ (EventNewsPage の URL クエリ名 since_hours と揃える)", () => {
    const href = buildEventNewsHref({
      category: "apt", feed: "", channel: "research",
      severity: { minSeverity: "S2", relevantOnly: true, includeStrategic: true },
      jp: "targeted_affected", sinceHours: 72,
    });
    const url = new URL(href, "http://localhost");
    expect(url.pathname).toBe("/app/eventnews");
    expect(url.searchParams.get("category")).toBe("apt");
    expect(url.searchParams.get("channel")).toBe("research");
    expect(url.searchParams.get("min_severity")).toBe("S2");
    expect(url.searchParams.get("relevant_only")).toBe("1");
    expect(url.searchParams.get("include_strategic")).toBe("1");
    expect(url.searchParams.get("jp")).toBe("targeted_affected");
    expect(url.searchParams.get("since_hours")).toBe("72");
  });

  it("feed 指定時も feed をクエリに含める (記事フィードと違い見出しは別途 feed 名になる)", () => {
    const href = buildEventNewsHref({
      category: "", feed: "JPCERT/CC", channel: "",
      severity: { minSeverity: "", relevantOnly: false, includeStrategic: false },
      jp: "", sinceHours: 0,
    });
    expect(new URL(href, "http://localhost").searchParams.get("feed")).toBe("JPCERT/CC");
  });
});
