// usePullToRefresh の回帰テスト。
// pullDistance / readyToRelease は React state (indicator の矢印→スピナー切替に
// レンダリング反映が必要) なので、DOM 直操作の useSwipeToClose と違い act() で包んで
// 状態更新を確定させてから読む。

import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, screen } from "@testing-library/react";
import { createElement } from "react";
import { PULL_THRESHOLD_PX, usePullToRefresh } from "./usePullToRefresh";

function TestContainer({
  onRefresh,
  disabled,
  getScrollTop,
}: {
  onRefresh?: () => void;
  disabled?: boolean;
  getScrollTop?: () => number;
}) {
  const { containerRef, pullDistance, readyToRelease } = usePullToRefresh<HTMLDivElement>({
    onRefresh,
    disabled,
    getScrollTop,
  });
  return createElement(
    "div",
    { ref: containerRef, "data-testid": "container" },
    createElement("span", { "data-testid": "pull-distance" }, String(pullDistance)),
    createElement("span", { "data-testid": "ready" }, String(readyToRelease)),
  );
}

function touchEvent(type: string, touches: Array<{ clientY: number }>): Event {
  const ev = new Event(type, { bubbles: true, cancelable: true });
  const withX = touches.map((t) => ({ clientX: 0, clientY: t.clientY }));
  Object.assign(ev, { touches: withX, changedTouches: withX });
  return ev;
}

function renderContainer(props: { onRefresh?: () => void; disabled?: boolean; getScrollTop?: () => number }) {
  render(createElement(TestContainer, props));
  return screen.getByTestId("container");
}

describe("usePullToRefresh", () => {
  afterEach(cleanup);

  it("最上部で70pxを超えて引いて離すと onRefresh を呼ぶ", () => {
    // Arrange
    const onRefresh = vi.fn();
    const container = renderContainer({ onRefresh, getScrollTop: () => 0 });

    // Act
    act(() => container.dispatchEvent(touchEvent("touchstart", [{ clientY: 0 }])));
    act(() => container.dispatchEvent(touchEvent("touchmove", [{ clientY: PULL_THRESHOLD_PX + 30 }])));
    act(() => container.dispatchEvent(touchEvent("touchend", [])));

    // Assert
    expect(onRefresh).toHaveBeenCalledTimes(1);
  });

  it("閾値未満で離すと onRefresh を呼ばず、pullDistance は 0 に戻る", () => {
    // Arrange
    const onRefresh = vi.fn();
    const container = renderContainer({ onRefresh, getScrollTop: () => 0 });

    // Act
    act(() => container.dispatchEvent(touchEvent("touchstart", [{ clientY: 0 }])));
    act(() => container.dispatchEvent(touchEvent("touchmove", [{ clientY: 30 }])));

    // Assert: 引いている最中は反映される
    expect(screen.getByTestId("pull-distance").textContent).toBe("30");
    expect(screen.getByTestId("ready").textContent).toBe("false");

    act(() => container.dispatchEvent(touchEvent("touchend", [])));

    // Assert
    expect(onRefresh).not.toHaveBeenCalled();
    expect(screen.getByTestId("pull-distance").textContent).toBe("0");
  });

  it("引いた量が閾値以上になると readyToRelease が true になる", () => {
    // Arrange
    const container = renderContainer({ getScrollTop: () => 0 });

    // Act
    act(() => container.dispatchEvent(touchEvent("touchstart", [{ clientY: 0 }])));
    act(() => container.dispatchEvent(touchEvent("touchmove", [{ clientY: PULL_THRESHOLD_PX }])));

    // Assert
    expect(screen.getByTestId("ready").textContent).toBe("true");
  });

  it("上方向 (引いていない) の動きは pullDistance を 0 のままにする", () => {
    // Arrange
    const onRefresh = vi.fn();
    const container = renderContainer({ onRefresh, getScrollTop: () => 0 });

    // Act
    act(() => container.dispatchEvent(touchEvent("touchstart", [{ clientY: 100 }])));
    act(() => container.dispatchEvent(touchEvent("touchmove", [{ clientY: 20 }])));
    act(() => container.dispatchEvent(touchEvent("touchend", [])));

    // Assert
    expect(onRefresh).not.toHaveBeenCalled();
    expect(screen.getByTestId("pull-distance").textContent).toBe("0");
  });

  it("最上部でない (scrollTop > 0) 場合は反応しない", () => {
    // Arrange
    const onRefresh = vi.fn();
    const container = renderContainer({ onRefresh, getScrollTop: () => 10 });

    // Act
    act(() => container.dispatchEvent(touchEvent("touchstart", [{ clientY: 0 }])));
    act(() => container.dispatchEvent(touchEvent("touchmove", [{ clientY: PULL_THRESHOLD_PX + 30 }])));
    act(() => container.dispatchEvent(touchEvent("touchend", [])));

    // Assert
    expect(onRefresh).not.toHaveBeenCalled();
    expect(screen.getByTestId("pull-distance").textContent).toBe("0");
  });

  it("スクロール中にトップから離れたら追従を打ち切る", () => {
    // Arrange: touchstart 時は top (0) だが touchmove 時には scrollTop > 0 に変化
    const onRefresh = vi.fn();
    let top = 0;
    const container = renderContainer({ onRefresh, getScrollTop: () => top });

    // Act
    act(() => container.dispatchEvent(touchEvent("touchstart", [{ clientY: 0 }])));
    top = 5;
    act(() => container.dispatchEvent(touchEvent("touchmove", [{ clientY: PULL_THRESHOLD_PX + 30 }])));
    act(() => container.dispatchEvent(touchEvent("touchend", [])));

    // Assert
    expect(onRefresh).not.toHaveBeenCalled();
    expect(screen.getByTestId("pull-distance").textContent).toBe("0");
  });

  it("disabled の間は一切反応しない", () => {
    // Arrange
    const onRefresh = vi.fn();
    const container = renderContainer({ onRefresh, disabled: true, getScrollTop: () => 0 });

    // Act
    act(() => container.dispatchEvent(touchEvent("touchstart", [{ clientY: 0 }])));
    act(() => container.dispatchEvent(touchEvent("touchmove", [{ clientY: PULL_THRESHOLD_PX + 30 }])));
    act(() => container.dispatchEvent(touchEvent("touchend", [])));

    // Assert
    expect(onRefresh).not.toHaveBeenCalled();
    expect(screen.getByTestId("pull-distance").textContent).toBe("0");
  });

  it("touchend 時に他の指が残っていれば確定しない。最後の指が離れて初めて判定する (HIGH-4)", () => {
    // Arrange
    const onRefresh = vi.fn();
    const container = renderContainer({ onRefresh, getScrollTop: () => 0 });

    // Act: 閾値を超えるまで引く
    act(() => container.dispatchEvent(touchEvent("touchstart", [{ clientY: 0 }])));
    act(() => container.dispatchEvent(touchEvent("touchmove", [{ clientY: PULL_THRESHOLD_PX + 30 }])));

    // Act: 2本目の指の touchend (主指はまだ触れている想定 = touches に1本残る)
    act(() => container.dispatchEvent(touchEvent("touchend", [{ clientY: PULL_THRESHOLD_PX + 30 }])));

    // Assert: まだ確定しない
    expect(onRefresh).not.toHaveBeenCalled();

    // Act: 最後の指が離れる (touches が空になる)
    act(() => container.dispatchEvent(touchEvent("touchend", [])));

    // Assert: ここで初めて確定する
    expect(onRefresh).toHaveBeenCalledTimes(1);
  });

  it("touchcancel は状態をリセットするだけで onRefresh を呼ばない (HIGH-5)", () => {
    // Arrange: 着信/OS 割込み等でジェスチャが強制中断されるケースを模す
    const onRefresh = vi.fn();
    const container = renderContainer({ onRefresh, getScrollTop: () => 0 });

    // Act: 閾値を超えるまで引いた直後に touchcancel が来る
    act(() => container.dispatchEvent(touchEvent("touchstart", [{ clientY: 0 }])));
    act(() => container.dispatchEvent(touchEvent("touchmove", [{ clientY: PULL_THRESHOLD_PX + 30 }])));
    expect(screen.getByTestId("ready").textContent).toBe("true");

    act(() => container.dispatchEvent(touchEvent("touchcancel", [])));

    // Assert: reload は起きない。表示はリセットされる
    expect(onRefresh).not.toHaveBeenCalled();
    expect(screen.getByTestId("pull-distance").textContent).toBe("0");
  });

  it("mid-gesture で disabled に切り替わると pullDistance がリセットされ、固まらない (MEDIUM-9)", () => {
    // Arrange
    const onRefresh = vi.fn();
    const { rerender } = render(
      createElement(TestContainer, { onRefresh, disabled: false, getScrollTop: () => 0 }),
    );
    const container = screen.getByTestId("container");

    // Act: 引いている最中 (pullDistance > 0)
    act(() => container.dispatchEvent(touchEvent("touchstart", [{ clientY: 0 }])));
    act(() => container.dispatchEvent(touchEvent("touchmove", [{ clientY: 30 }])));
    expect(screen.getByTestId("pull-distance").textContent).toBe("30");

    // Act: peek が開く等で disabled に切り替わる (unmount ではなく無効化)
    act(() => {
      rerender(createElement(TestContainer, { onRefresh, disabled: true, getScrollTop: () => 0 }));
    });

    // Assert: インジケータが引いた位置のまま固まらない
    expect(screen.getByTestId("pull-distance").textContent).toBe("0");
    expect(onRefresh).not.toHaveBeenCalled();
  });

  it("onRefresh 未指定なら既定で window.location.reload を呼ぶ", () => {
    // Arrange: jsdom の location.reload は再定義不可 (Cannot redefine property) なため
    // location オブジェクトごと差し替える
    const reloadSpy = vi.fn();
    Object.defineProperty(window, "location", {
      value: { ...window.location, reload: reloadSpy },
      writable: true,
    });
    const container = renderContainer({ getScrollTop: () => 0 });

    // Act
    act(() => container.dispatchEvent(touchEvent("touchstart", [{ clientY: 0 }])));
    act(() => container.dispatchEvent(touchEvent("touchmove", [{ clientY: PULL_THRESHOLD_PX + 30 }])));
    act(() => container.dispatchEvent(touchEvent("touchend", [])));

    // Assert
    expect(reloadSpy).toHaveBeenCalledTimes(1);
  });
});
