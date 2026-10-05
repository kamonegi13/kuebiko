import { describe, expect, it } from "vitest";
import { mirrorSwitchHref, publicSwitchHref, MIRROR_HOME_PATH, PUBLIC_NEWS_PATH } from "./siteSwitch";

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
