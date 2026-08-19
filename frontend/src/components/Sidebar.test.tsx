// サイドバー drawer のログイン導線の回帰テスト。
//
// 動機: ログイン行は nav (overflow-y-auto) の **外側** に最下部固定で置いてある。
// drawer の高さが 100vh 固定だと、モバイルブラウザでは URL バー/ツールバーのぶん
// 可視領域が 100vh より短いため、この行が画面外に出たまま**スクロールでも到達できない**
// 状態になる (2026-08-19 に実機で発生。PC では chrome が viewport 外なので気付けない)。
// 「DOM に在る」だけでは不十分で、可視ビューポート追従の高さ指定が要る。

import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createElement, type ReactNode } from "react";
import { Sidebar } from "./Sidebar";

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return createElement(QueryClientProvider, { client: qc }, children);
}

function renderSidebar() {
  return render(
    createElement(Sidebar, {
      collapsed: false,
      mobileOpen: true,
      pathname: "/app/dashboard",
      onToggleCollapse: () => {},
      onCloseMobile: () => {},
    }),
    { wrapper },
  );
}

describe("Sidebar のログイン導線", () => {
  // この repo は vitest の globals を使わない方針のため、testing-library の
  // auto-cleanup が登録されない。明示的に片付けないと DOM が積み上がる。
  afterEach(cleanup);

  beforeEach(() => {
    // readonly instance が index.html に埋める seed を再現する
    window.__READ_ONLY__ = true;
    window.__AUTH_AVAILABLE__ = true;
    window.__AUTHENTICATED__ = false;
  });

  it("未認証の公開 instance ではログインリンクを出す", () => {
    // Act
    renderSidebar();

    // Assert
    const link = screen.getByRole("link", { name: /ログイン/ });
    expect(link.getAttribute("href")).toBe("/auth/login");
  });

  it("認証済みならログアウトリンクに変わる (保護対象外の /logout へ)", () => {
    // Arrange
    window.__AUTHENTICATED__ = true;

    // Act
    renderSidebar();

    // Assert
    const link = screen.getByRole("link", { name: /ログアウト/ });
    expect(link.getAttribute("href")).toBe("/logout");
  });

  it("Access 未設定なら認証の行そのものを出さない", () => {
    // Arrange
    window.__AUTH_AVAILABLE__ = false;

    // Act
    renderSidebar();

    // Assert
    expect(screen.queryByRole("link", { name: /ログイン|ログアウト/ })).toBeNull();
  });

  it("drawer の高さは可視ビューポートに追従する (100vh 固定に戻さない)", () => {
    // Arrange
    renderSidebar();

    // Act
    const aside = document.querySelector("aside");

    // Assert — 100vh のみだとモバイルで最下部のログイン行が画面外に出る
    expect(aside?.className).toContain("supports-[height:100dvh]:h-dvh");
  });
});
