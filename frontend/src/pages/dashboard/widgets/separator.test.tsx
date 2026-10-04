import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { SeparatorWidget } from "./separator";

describe("SeparatorWidget (データ・機能なしの区切り線)", () => {
  it("既定 (label 未指定) は見出しを出さない", () => {
    render(<SeparatorWidget />);
    expect(screen.queryByText(/./)).toBeNull();
  });

  it("label を設定すると見出し文字列を表示する", () => {
    render(<SeparatorWidget config={{ label: "運用" }} />);
    expect(screen.getByText("運用")).toBeTruthy();
  });
});
