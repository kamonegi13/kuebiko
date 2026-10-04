import { describe, expect, it, afterEach } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { SiteSwitch } from "./SiteSwitch";

afterEach(() => cleanup());

describe("SiteSwitch", () => {
  it("標準面では「標準」が現在、「アドバンスド」がリンク", () => {
    render(<SiteSwitch current="standard" otherOrigin="https://mirror.kuebiko.example" />);
    const current = screen.getByText("標準");
    expect(current.getAttribute("aria-current")).toBe("page");
    const other = screen.getByText("アドバンスド");
    expect(other.closest("a")?.getAttribute("href")).toBe(
      "https://mirror.kuebiko.example/app/eventnews",
    );
  });

  it("アドバンスド面では「標準」がリンク", () => {
    render(<SiteSwitch current="advanced" otherOrigin="https://kuebiko.example" />);
    const current = screen.getByText("アドバンスド");
    expect(current.getAttribute("aria-current")).toBe("page");
    const other = screen.getByText("標準");
    expect(other.closest("a")?.getAttribute("href")).toBe("https://kuebiko.example/news");
  });

  it("相手サイトのオリジン未設定なら無効表示 (リンクにしない)", () => {
    render(<SiteSwitch current="standard" otherOrigin="" />);
    const other = screen.getByText("アドバンスド");
    expect(other.closest("a")).toBeNull();
  });
});
