// ArticlePeek (ArticlePeekHost) の history 統合の回帰テスト。
//
// 動機 (2026-08-21 レビュー対応): history.back() は同期に解決しない (対応する
// popstate は次 tick 以降にしか来ない) ため、store の articleId を再入 guard に
// 使うと壊れる。以下を固定する:
//   1. requestClose (X/backdrop/フリック共通の入口) を連打しても history.back() は
//      1 回しか呼ばれない (さもなくば実ナビが full page load のこのアプリでは
//      2 段 pop → 別ページへ飛ぶ)
//   2. close が popstate 未解決のうちに別記事を open すると、popstate 側で
//      「close を消化 → 保留していた open を実行 + push」の順で正しく処理され、
//      結果として B が開いたまま・history は 1 段だけ積まれた状態になる
//   3. peek 表示中のリロードで残る「幽霊 history 段」を mount 時に replaceState で潰す
//
// history.back() は jsdom の実ナビゲーションタイミングに依存させると非決定的になる
// ため spy で no-op 化し、対応する popstate はテスト側で明示的に発火させて
// 「非同期解決」をシミュレートする (実際のブラウザでも back() 呼び出しと popstate
// 発火の間には必ずタイムラグがある)。

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createElement, type ReactNode } from "react";
import { ArticlePeekHost } from "./ArticlePeek";
import { useArticlePeekStore } from "../state/articlePeek";

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return createElement(QueryClientProvider, { client: qc }, children);
}

function renderHost() {
  return render(createElement(ArticlePeekHost), { wrapper });
}

/** グローバル click インターセプトの実経路を通す (a[href^="/app/article/"] への
 * 通常クリックをシミュレート)。テスト用の <a> は同期処理後すぐ除去する。 */
function clickArticleLink(articleId: string): void {
  const anchor = document.createElement("a");
  anchor.href = `/app/article/${articleId}`;
  document.body.appendChild(anchor);
  fireEvent.click(anchor);
  anchor.remove();
}

describe("ArticlePeekHost の history 統合", () => {
  beforeEach(() => {
    useArticlePeekStore.setState({ articleId: null });
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("requestClose を連打しても history.back() は1回しか呼ばれない (HIGH-1)", () => {
    // Arrange
    renderHost();
    clickArticleLink("A");
    expect(useArticlePeekStore.getState().articleId).toBe("A");
    const backSpy = vi.spyOn(window.history, "back").mockImplementation(() => {});

    // Act: X ボタンを連打 (対応する popstate はまだ来ていない = latch が立ったまま)
    const closeButton = screen.getByRole("button", { name: "閉じる" });
    fireEvent.click(closeButton);
    fireEvent.click(closeButton);
    fireEvent.click(closeButton);

    // Assert
    expect(backSpy).toHaveBeenCalledTimes(1);
  });

  it("close が popstate 未解決のうちに別記事を open すると、popstate 後に B が開いたまま・履歴1段になる (HIGH-2)", () => {
    // Arrange
    renderHost();
    const pushSpy = vi.spyOn(window.history, "pushState").mockImplementation(() => {});
    const backSpy = vi.spyOn(window.history, "back").mockImplementation(() => {});

    clickArticleLink("A");
    expect(pushSpy).toHaveBeenCalledTimes(1); // A を開いた分の push

    // Act 1: close を要求 (back() は no-op なので popstate はまだ来ない = 未解決のまま)
    fireEvent.click(screen.getByRole("button", { name: "閉じる" }));
    expect(backSpy).toHaveBeenCalledTimes(1);

    // Act 2: 未解決のうちに別記事 B を開こうとする → 即座には反映されない
    clickArticleLink("B");
    expect(useArticlePeekStore.getState().articleId).toBe("A"); // まだ切り替わっていない
    expect(pushSpy).toHaveBeenCalledTimes(1); // 追加 push もまだ起きていない

    // Act 3: back() の解決をシミュレート (popstate 発火)
    act(() => {
      window.dispatchEvent(new Event("popstate"));
    });

    // Assert: close → pendingOpen(B) の順で処理され、B が開いたまま・push は合計2回
    // (A を開いた分 + B を開いた分。back() は1回しか呼ばれていないので正味 +1 段)
    expect(useArticlePeekStore.getState().articleId).toBe("B");
    expect(pushSpy).toHaveBeenCalledTimes(2);
    expect(backSpy).toHaveBeenCalledTimes(1);
  });

  it("mount 時に history.state の幽霊 articlePeek marker を replaceState で潰す (HIGH-6)", () => {
    // Arrange: peek 表示中にリロードされた状態を模す — history.state に marker が
    // 残ったまま store は articleId:null で立ち上がる
    window.history.pushState({ articlePeek: true }, "");
    const replaceSpy = vi.spyOn(window.history, "replaceState").mockImplementation(() => {});

    // Act
    renderHost();

    // Assert
    expect(replaceSpy).toHaveBeenCalledWith(null, "");
    expect(useArticlePeekStore.getState().articleId).toBeNull();
  });

  it("marker が無ければ mount 時に replaceState を呼ばない", () => {
    // Arrange
    window.history.pushState(null, "");
    const replaceSpy = vi.spyOn(window.history, "replaceState").mockImplementation(() => {});

    // Act
    renderHost();

    // Assert
    expect(replaceSpy).not.toHaveBeenCalled();
  });
});
