// 左右フリック (期間切替) の回帰テスト。
//
// ⚠ モバイル固有の故障は PC / Playwright では再現しない。幾何を直接与えて確かめる。
// jsdom は実レイアウトを持たないため、overflow 判定に要る DOM プロパティは明示する。

import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { createElement } from "react";

import { useHorizontalSwipe } from "./useHorizontalSwipe";

function Panel(props: {
  onNext: () => void;
  onPrev: () => void;
  disabled?: boolean;
  withScrollableChild?: boolean;
}) {
  const ref = useHorizontalSwipe<HTMLDivElement>(props);
  return createElement(
    "div",
    { ref, "data-testid": "panel" },
    props.withScrollableChild
      ? createElement(
          "div",
          { "data-testid": "scrollable", style: { overflowX: "auto" } },
          createElement("button", { "data-testid": "child" }, "item"),
        )
      : null,
  );
}

function ev(type: string, touches: Array<{ clientX: number; clientY: number }>): Event {
  const e = new Event(type, { bubbles: true, cancelable: true });
  Object.assign(e, { touches, changedTouches: touches });
  return e;
}

/** 最後の指が離れた形の touchend (touches は空、changedTouches に離れた指)。 */
function endAt(x: number, y: number): Event {
  const e = new Event("touchend", { bubbles: true, cancelable: true });
  Object.assign(e, { touches: [], changedTouches: [{ clientX: x, clientY: y }] });
  return e;
}

function setup(p: { disabled?: boolean; withScrollableChild?: boolean } = {}) {
  const onNext = vi.fn();
  const onPrev = vi.fn();
  render(createElement(Panel, { onNext, onPrev, ...p }));
  return { panel: screen.getByTestId("panel") as HTMLDivElement, onNext, onPrev };
}

describe("useHorizontalSwipe", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("左へ払うと次へ", () => {
    const { panel, onNext, onPrev } = setup();
    panel.dispatchEvent(ev("touchstart", [{ clientX: 200, clientY: 100 }]));
    panel.dispatchEvent(ev("touchmove", [{ clientX: 160, clientY: 102 }]));
    panel.dispatchEvent(endAt(120, 102));
    expect(onNext).toHaveBeenCalledTimes(1);
    expect(onPrev).not.toHaveBeenCalled();
  });

  it("右へ払うと前へ", () => {
    const { panel, onNext, onPrev } = setup();
    panel.dispatchEvent(ev("touchstart", [{ clientX: 100, clientY: 100 }]));
    panel.dispatchEvent(ev("touchmove", [{ clientX: 140, clientY: 98 }]));
    panel.dispatchEvent(endAt(180, 98));
    expect(onPrev).toHaveBeenCalledTimes(1);
    expect(onNext).not.toHaveBeenCalled();
  });

  it("移動が小さければ切り替えない (タップを奪わない)", () => {
    const { panel, onNext, onPrev } = setup();
    panel.dispatchEvent(ev("touchstart", [{ clientX: 200, clientY: 100 }]));
    panel.dispatchEvent(ev("touchmove", [{ clientX: 180, clientY: 100 }]));
    panel.dispatchEvent(endAt(170, 100));
    expect(onNext).not.toHaveBeenCalled();
    expect(onPrev).not.toHaveBeenCalled();
  });

  it("縦優勢ならスクロールに返す", () => {
    const { panel, onNext, onPrev } = setup();
    panel.dispatchEvent(ev("touchstart", [{ clientX: 200, clientY: 100 }]));
    panel.dispatchEvent(ev("touchmove", [{ clientX: 190, clientY: 60 }]));
    panel.dispatchEvent(endAt(100, 20));
    expect(onNext).not.toHaveBeenCalled();
    expect(onPrev).not.toHaveBeenCalled();
  });

  // ⚠ 一度縦と決めたら touchend まで変えない (途中で横へ戻っても切り替えない)。
  it("方向は一度決めたら変わらない", () => {
    const { panel, onNext } = setup();
    panel.dispatchEvent(ev("touchstart", [{ clientX: 200, clientY: 100 }]));
    panel.dispatchEvent(ev("touchmove", [{ clientX: 198, clientY: 60 }]));
    panel.dispatchEvent(ev("touchmove", [{ clientX: 100, clientY: 60 }]));
    panel.dispatchEvent(endAt(100, 60));
    expect(onNext).not.toHaveBeenCalled();
  });

  // ⚠ 割り込まれた操作を切り替えとして確定させない。
  it("touchcancel は副作用なしで中断する", () => {
    const { panel, onNext, onPrev } = setup();
    panel.dispatchEvent(ev("touchstart", [{ clientX: 200, clientY: 100 }]));
    panel.dispatchEvent(ev("touchmove", [{ clientX: 120, clientY: 100 }]));
    panel.dispatchEvent(ev("touchcancel", []));
    panel.dispatchEvent(endAt(100, 100));
    expect(onNext).not.toHaveBeenCalled();
    expect(onPrev).not.toHaveBeenCalled();
  });

  // ⚠ 指が 1 本残っている間は確定しない (多指操作の途中で発火させない)。
  it("最後の指が離れるまで確定しない", () => {
    const { panel, onNext } = setup();
    panel.dispatchEvent(ev("touchstart", [{ clientX: 200, clientY: 100 }]));
    panel.dispatchEvent(ev("touchmove", [{ clientX: 120, clientY: 100 }]));
    panel.dispatchEvent(ev("touchend", [{ clientX: 120, clientY: 100 }]));
    expect(onNext).not.toHaveBeenCalled();
  });

  it("横スクロールできる領域の上では操作を奪わない", () => {
    const { panel, onNext } = setup({ withScrollableChild: true });
    const scrollable = screen.getByTestId("scrollable") as HTMLDivElement;
    Object.defineProperty(scrollable, "scrollWidth", { value: 500, configurable: true });
    Object.defineProperty(scrollable, "clientWidth", { value: 200, configurable: true });
    scrollable.scrollLeft = 100;
    const child = screen.getByTestId("child");
    child.dispatchEvent(ev("touchstart", [{ clientX: 200, clientY: 100 }]));
    panel.dispatchEvent(ev("touchmove", [{ clientX: 120, clientY: 100 }]));
    panel.dispatchEvent(endAt(100, 100));
    expect(onNext).not.toHaveBeenCalled();
  });

  it("disabled では一切反応しない", () => {
    const { panel, onNext, onPrev } = setup({ disabled: true });
    panel.dispatchEvent(ev("touchstart", [{ clientX: 200, clientY: 100 }]));
    panel.dispatchEvent(ev("touchmove", [{ clientX: 120, clientY: 100 }]));
    panel.dispatchEvent(endAt(100, 100));
    expect(onNext).not.toHaveBeenCalled();
    expect(onPrev).not.toHaveBeenCalled();
  });
});
