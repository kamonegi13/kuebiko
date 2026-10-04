import { describe, expect, it } from "vitest";
import { mirrorSwitchHref, publicSwitchHref, MIRROR_EVENTNEWS_PATH, PUBLIC_NEWS_PATH } from "./siteSwitch";

describe("mirrorSwitchHref (公開サイト → 写し)", () => {
  it("写しのオリジンに事象ニュース一覧パスを付けて返す", () => {
    expect(mirrorSwitchHref("https://mirror.kuebiko.example")).toBe(
      `https://mirror.kuebiko.example${MIRROR_EVENTNEWS_PATH}`,
    );
  });

  it("末尾スラッシュは重複させない", () => {
    expect(mirrorSwitchHref("https://mirror.kuebiko.example/")).toBe(
      "https://mirror.kuebiko.example/app/eventnews",
    );
  });

  it("未設定なら null (導線を出さない)", () => {
    expect(mirrorSwitchHref("")).toBeNull();
    expect(mirrorSwitchHref("   ")).toBeNull();
  });
});

describe("publicSwitchHref (写し → 公開サイト)", () => {
  it("公開サイトのオリジンに固定パスを付けて返す", () => {
    expect(publicSwitchHref("https://kuebiko.example")).toBe(
      `https://kuebiko.example${PUBLIC_NEWS_PATH}`,
    );
  });

  it("末尾スラッシュは重複させない", () => {
    expect(publicSwitchHref("https://kuebiko.example/")).toBe("https://kuebiko.example/news");
  });

  it("未設定なら null (導線を出さない)", () => {
    expect(publicSwitchHref("")).toBeNull();
  });
});
