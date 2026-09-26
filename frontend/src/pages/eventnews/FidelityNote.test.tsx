// 構成記事の固有情報との照合欄 (2026-09-26): 点数でなく欠けたものの一覧を出す。
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { FidelityNote } from "./EventNewsDetail";

afterEach(() => cleanup());

describe("FidelityNote", () => {
  it("欠けた固有情報を一覧で出す", () => {
    render(<FidelityNote fidelity={{ checked: 5, missing: [{ type: "cve", value: "CVE-2026-2" }] }} />);
    expect(screen.getByText(/5 件のうち、要約に無いもの 1 件/)).toBeTruthy();
    expect(screen.getByText("CVE-2026-2")).toBeTruthy();
  });

  it("欠けが無ければそう書く", () => {
    render(<FidelityNote fidelity={{ checked: 3, missing: [] }} />);
    expect(screen.getByText(/3 件は、すべて要約に含まれている/)).toBeTruthy();
  });
});
