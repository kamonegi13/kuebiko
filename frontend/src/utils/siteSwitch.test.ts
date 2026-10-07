import { describe, expect, it } from "vitest";
import { entryRedirect, mirrorSwitchHref, preferredSite, publicSwitchHref, rememberSite, MIRROR_HOME_PATH, PUBLIC_NEWS_PATH } from "./siteSwitch";

describe("mirrorSwitchHref (公開サイト → アドバンスド)", () => {
  it("同一オリジンの相対パスを返す", () => {
    expect(mirrorSwitchHref()).toBe(MIRROR_HOME_PATH);
    expect(mirrorSwitchHref()).toBe("/app");
  });
});

describe("publicSwitchHref (アドバンスド → 公開サイト)", () => {
  it("同一オリジンの相対パスを返す", () => {
    expect(publicSwitchHref()).toBe(PUBLIC_NEWS_PATH);
    expect(publicSwitchHref()).toBe("/news");
  });
});

describe("標準 / 拡張の選択を覚える", () => {
  it("拡張を選んでいたら入口 (/ と /news) から拡張のダッシュボードへ", () => {
    expect(entryRedirect("/", "advanced")).toBe("/app");
    expect(entryRedirect("/news/", "advanced")).toBe("/app");
  });

  it("奥の画面・標準を選んだとき・未選択では振り向けない", () => {
    expect(entryRedirect("/news/e/123", "advanced")).toBeNull();
    expect(entryRedirect("/", "standard")).toBeNull();
    expect(entryRedirect("/", null)).toBeNull();
  });

  it("選択を保存して読み戻せる", () => {
    rememberSite("advanced");
    expect(preferredSite()).toBe("advanced");
    rememberSite("standard");
    expect(preferredSite()).toBe("standard");
  });
});
