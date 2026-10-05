import { describe, expect, it, afterEach } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { SiteSwitch } from "./SiteSwitch";

afterEach(() => cleanup());

describe("SiteSwitch", () => {
  it("標準面では「標準」が現在、「拡張」が /app への相対リンク", () => {
    render(<SiteSwitch current="standard" />);
    const current = screen.getByText("標準");
    expect(current.getAttribute("aria-current")).toBe("page");
    const other = screen.getByText("拡張");
    expect(other.closest("a")?.getAttribute("href")).toBe("/app");
  });

  it("アドバンスド面では「標準」が /news への相対リンク", () => {
    render(<SiteSwitch current="advanced" />);
    const current = screen.getByText("拡張");
    expect(current.getAttribute("aria-current")).toBe("page");
    const other = screen.getByText("標準");
    expect(other.closest("a")?.getAttribute("href")).toBe("/news");
  });
});
