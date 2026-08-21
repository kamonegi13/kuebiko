// useSwipeToClose の回帰テスト。
//
// jsdom は実レイアウトを持たないため、幅・overflow 判定に必要な DOM プロパティは
// テスト側で明示的に上書きする (getBoundingClientRect / scrollWidth / clientWidth /
// scrollLeft はいずれも jsdom 既定では常に 0 を返す)。速度計算は Date.now() 差分に
// 依存するため vi.useFakeTimers() + vi.setSystemTime() で時刻を確定させる
// (⚠ vi.spyOn(Date, "now").mockReturnValueOnce(...) の有限キューは、jsdom が
// Event 構築時に内部で timeStamp 算出用の Date.now() を余分に呼ぶため値がずれて
// 誤検証になる — フェイクタイマーなら呼び出し回数に依存せず常に同じ現在時刻を返すため安全)。

import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { createElement } from "react";
import { useSwipeToClose } from "./useSwipeToClose";

function TestPanel({
  onClose,
  disabled,
  withScrollableChild,
}: {
  onClose: () => void;
  disabled?: boolean;
  withScrollableChild?: boolean;
}) {
  const ref = useSwipeToClose<HTMLDivElement>({ onClose, disabled });
  return createElement(
    "div",
    { ref, "data-testid": "panel" },
    withScrollableChild
      ? createElement(
          "div",
          { "data-testid": "scrollable", style: { overflowX: "auto" } },
          createElement("button", { "data-testid": "scroll-child" }, "item"),
        )
      : null,
  );
}

function mockPanelWidth(el: HTMLElement, width: number): void {
  el.getBoundingClientRect = () =>
    ({ width, height: 0, top: 0, left: 0, right: width, bottom: 0, x: 0, y: 0, toJSON: () => ({}) }) as DOMRect;
}

function touchEvent(type: string, touches: Array<{ clientX: number; clientY: number }>): Event {
  const ev = new Event(type, { bubbles: true, cancelable: true });
  Object.assign(ev, { touches, changedTouches: touches });
  return ev;
}

function renderPanel(props: { onClose: () => void; disabled?: boolean; withScrollableChild?: boolean }) {
  render(createElement(TestPanel, props));
  const panel = screen.getByTestId("panel") as HTMLDivElement;
  mockPanelWidth(panel, 300);
  return panel;
}

describe("useSwipeToClose", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  it("幅の30%を超えて右へ動かして離すと閉じる (距離条件)", () => {
    // Arrange
    const onClose = vi.fn();
    const panel = renderPanel({ onClose });
    vi.useFakeTimers();
    vi.setSystemTime(0);

    // Act: dx=200 (300 の 66% > 30%)、dt=1000ms → velocity=0.2px/ms (閾値未満)
    panel.dispatchEvent(touchEvent("touchstart", [{ clientX: 0, clientY: 0 }]));
    vi.setSystemTime(1000);
    panel.dispatchEvent(touchEvent("touchmove", [{ clientX: 200, clientY: 0 }]));
    panel.dispatchEvent(touchEvent("touchend", []));

    // Assert
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("距離が閾値未満でも速度が0.5px/msを超えていれば閉じる (速度条件)", () => {
    // Arrange
    const onClose = vi.fn();
    const panel = renderPanel({ onClose });
    vi.useFakeTimers();
    vi.setSystemTime(0);

    // Act: dx=20 (300 の 6.7% < 30%) だが速い (dt=10ms → velocity=2.0px/ms)
    panel.dispatchEvent(touchEvent("touchstart", [{ clientX: 0, clientY: 0 }]));
    vi.setSystemTime(10);
    panel.dispatchEvent(touchEvent("touchmove", [{ clientX: 20, clientY: 0 }]));
    panel.dispatchEvent(touchEvent("touchend", []));

    // Assert
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("距離・速度とも閾値未満なら閉じずに元の位置へ戻す", () => {
    // Arrange
    const onClose = vi.fn();
    const panel = renderPanel({ onClose });
    vi.useFakeTimers();
    vi.setSystemTime(0);

    // Act: dx=50, dt=1000ms → velocity=0.05px/ms (両方とも閾値未満)
    panel.dispatchEvent(touchEvent("touchstart", [{ clientX: 0, clientY: 0 }]));
    vi.setSystemTime(1000);
    panel.dispatchEvent(touchEvent("touchmove", [{ clientX: 50, clientY: 0 }]));
    panel.dispatchEvent(touchEvent("touchend", []));

    // Assert
    expect(onClose).not.toHaveBeenCalled();
    expect(panel.style.transform).toBe("translateX(0px)");
    expect(panel.style.transition).toContain("transform");
  });

  it("縦方向優勢の動きは無視し、ページスクロールを止めない (preventDefault しない)", () => {
    // Arrange
    const onClose = vi.fn();
    const panel = renderPanel({ onClose });

    // Act: dy(50) が dx(3) を圧倒 → 横フリックと判定しない
    panel.dispatchEvent(touchEvent("touchstart", [{ clientX: 0, clientY: 0 }]));
    const move = touchEvent("touchmove", [{ clientX: 3, clientY: 50 }]);
    const preventDefaultSpy = vi.spyOn(move, "preventDefault");
    panel.dispatchEvent(move);
    panel.dispatchEvent(touchEvent("touchend", []));

    // Assert
    expect(preventDefaultSpy).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
    expect(panel.style.transform).toBe("");
  });

  it("左方向への動きは閉じない", () => {
    // Arrange
    const onClose = vi.fn();
    const panel = renderPanel({ onClose });

    // Act
    panel.dispatchEvent(touchEvent("touchstart", [{ clientX: 100, clientY: 0 }]));
    panel.dispatchEvent(touchEvent("touchmove", [{ clientX: 20, clientY: 0 }]));
    panel.dispatchEvent(touchEvent("touchend", []));

    // Assert
    expect(onClose).not.toHaveBeenCalled();
  });

  it("横スクロール可能かつ scrollLeft > 0 な祖先の上で始まったタッチはジェスチャを開始しない", () => {
    // Arrange
    const onClose = vi.fn();
    const panel = renderPanel({ onClose, withScrollableChild: true });
    const scrollable = screen.getByTestId("scrollable");
    Object.defineProperty(scrollable, "scrollWidth", { value: 500, configurable: true });
    Object.defineProperty(scrollable, "clientWidth", { value: 200, configurable: true });
    Object.defineProperty(scrollable, "scrollLeft", { value: 5, configurable: true });
    const child = screen.getByTestId("scroll-child");

    // Act: 表の中で始まった右フリック相当の動き
    child.dispatchEvent(touchEvent("touchstart", [{ clientX: 0, clientY: 0 }]));
    panel.dispatchEvent(touchEvent("touchmove", [{ clientX: 200, clientY: 0 }]));
    panel.dispatchEvent(touchEvent("touchend", []));

    // Assert: 追従も close も起きない (表のスクロール操作を奪わない)
    expect(onClose).not.toHaveBeenCalled();
    expect(panel.style.transform).toBe("");
  });

  it("scrollLeft === 0 (これ以上左へスクロールできない) なら通常どおりジェスチャを開始する", () => {
    // Arrange
    const onClose = vi.fn();
    const panel = renderPanel({ onClose, withScrollableChild: true });
    const scrollable = screen.getByTestId("scrollable");
    Object.defineProperty(scrollable, "scrollWidth", { value: 500, configurable: true });
    Object.defineProperty(scrollable, "clientWidth", { value: 200, configurable: true });
    Object.defineProperty(scrollable, "scrollLeft", { value: 0, configurable: true });
    const child = screen.getByTestId("scroll-child");
    vi.useFakeTimers();
    vi.setSystemTime(0);

    // Act
    child.dispatchEvent(touchEvent("touchstart", [{ clientX: 0, clientY: 0 }]));
    vi.setSystemTime(1000);
    panel.dispatchEvent(touchEvent("touchmove", [{ clientX: 200, clientY: 0 }]));
    panel.dispatchEvent(touchEvent("touchend", []));

    // Assert
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("disabled の間は一切反応しない", () => {
    // Arrange
    const onClose = vi.fn();
    const panel = renderPanel({ onClose, disabled: true });

    // Act
    panel.dispatchEvent(touchEvent("touchstart", [{ clientX: 0, clientY: 0 }]));
    panel.dispatchEvent(touchEvent("touchmove", [{ clientX: 200, clientY: 0 }]));
    panel.dispatchEvent(touchEvent("touchend", []));

    // Assert
    expect(onClose).not.toHaveBeenCalled();
    expect(panel.style.transform).toBe("");
  });

  it("2本指以上のタッチは無視する", () => {
    // Arrange
    const onClose = vi.fn();
    const panel = renderPanel({ onClose });

    // Act
    panel.dispatchEvent(
      touchEvent("touchstart", [
        { clientX: 0, clientY: 0 },
        { clientX: 10, clientY: 10 },
      ]),
    );
    panel.dispatchEvent(touchEvent("touchmove", [{ clientX: 200, clientY: 0 }]));
    panel.dispatchEvent(touchEvent("touchend", []));

    // Assert
    expect(onClose).not.toHaveBeenCalled();
  });

  it("swiping 中に2本目の指が触れると、追従位置を凍結残留させず元へ戻す (HIGH-3)", () => {
    // Arrange
    const onClose = vi.fn();
    const panel = renderPanel({ onClose });

    // Act: 1本指で水平優勢に動かして swiping 状態にする
    panel.dispatchEvent(touchEvent("touchstart", [{ clientX: 0, clientY: 0 }]));
    panel.dispatchEvent(touchEvent("touchmove", [{ clientX: 200, clientY: 0 }]));
    expect(panel.style.transform).toBe("translateX(200px)");

    // Act: 2本目の指が触れる (multi-touch)
    panel.dispatchEvent(
      touchEvent("touchstart", [
        { clientX: 200, clientY: 0 },
        { clientX: 10, clientY: 10 },
      ]),
    );

    // Assert: 追従位置で凍結せず、スナップバックされる
    expect(panel.style.transform).toBe("translateX(0px)");
    expect(panel.style.transition).toContain("transform");

    // Act: その後 touchend (0本) が来ても close されない (既に idle に落ちている)
    panel.dispatchEvent(touchEvent("touchend", []));
    expect(onClose).not.toHaveBeenCalled();
  });

  it("touchend 時に他の指が残っていれば確定しない。最後の指が離れて初めて判定する (HIGH-4)", () => {
    // Arrange
    const onClose = vi.fn();
    const panel = renderPanel({ onClose });

    // Act: 主指を閉じる条件 (距離30%超) まで動かす
    panel.dispatchEvent(touchEvent("touchstart", [{ clientX: 0, clientY: 0 }]));
    panel.dispatchEvent(touchEvent("touchmove", [{ clientX: 200, clientY: 0 }]));

    // Act: 2本目の指の touchend (主指はまだ触れている想定 = touches に1本残る)
    panel.dispatchEvent(touchEvent("touchend", [{ clientX: 200, clientY: 0 }]));

    // Assert: まだ確定しない
    expect(onClose).not.toHaveBeenCalled();

    // Act: 最後の指 (主指) が離れる (touches が空になる)
    panel.dispatchEvent(touchEvent("touchend", []));

    // Assert: ここで初めて確定する
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
