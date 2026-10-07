import { describe, expect, it, afterEach } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { OpsLoginLanding } from "./OpsLoginLanding";

afterEach(() => cleanup());

const base = { read_only: true, authenticated: false, auth_available: true, remote_write: false };

describe("運用画面の未ログインの入口", () => {
  it("ログインと公開版 (拡張) の 2 つだけを出し、標準 / 拡張の切り替えは出さない", () => {
    render(<OpsLoginLanding flags={{ ...base, public_site_origin: "https://kuebiko.example" }} />);

    expect(screen.getByText("ログインして運用画面を開く")).toBeTruthy();
    expect(screen.getByText("公開版を開く").closest("a")?.getAttribute("href")).toBe(
      "https://kuebiko.example/app",
    );
    expect(screen.queryByText("拡張")).toBeNull();
  });

  it("公開版のオリジンが未設定なら案内しない", () => {
    render(<OpsLoginLanding flags={base} />);

    expect(screen.queryByText("公開版を開く")).toBeNull();
  });
});
