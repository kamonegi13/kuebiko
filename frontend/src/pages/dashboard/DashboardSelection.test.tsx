import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen, fireEvent } from "@testing-library/react";
import { DashboardSelectionProvider, useDashboardSelection } from "./DashboardSelection";

afterEach(() => cleanup());

// mini_map / geo_ranking widget が同じ選択を共有する配線そのもの (widget の描画は別テスト)。
function Probe() {
  const { selectedIso, selectIso } = useDashboardSelection();
  return (
    <div>
      <span data-testid="selected">{selectedIso ?? "none"}</span>
      <button onClick={() => selectIso("JP")}>select-jp</button>
      <button onClick={() => selectIso("US")}>select-us</button>
      <button onClick={() => selectIso(null)}>clear</button>
    </div>
  );
}

describe("DashboardSelection (国選択の共有 state)", () => {
  it("Provider 配下では選択が反映される", () => {
    render(
      <DashboardSelectionProvider>
        <Probe />
      </DashboardSelectionProvider>,
    );
    expect(screen.getByTestId("selected").textContent).toBe("none");
    fireEvent.click(screen.getByText("select-jp"));
    expect(screen.getByTestId("selected").textContent).toBe("JP");
  });

  it("同じ国をもう一度選ぶと解除される (クリックしなおしで解除)", () => {
    render(
      <DashboardSelectionProvider>
        <Probe />
      </DashboardSelectionProvider>,
    );
    fireEvent.click(screen.getByText("select-jp"));
    expect(screen.getByTestId("selected").textContent).toBe("JP");
    fireEvent.click(screen.getByText("select-jp"));
    expect(screen.getByTestId("selected").textContent).toBe("none");
  });

  it("別の国を選ぶと切り替わる (× 相当の null でも解除できる)", () => {
    render(
      <DashboardSelectionProvider>
        <Probe />
      </DashboardSelectionProvider>,
    );
    fireEvent.click(screen.getByText("select-jp"));
    fireEvent.click(screen.getByText("select-us"));
    expect(screen.getByTestId("selected").textContent).toBe("US");
    fireEvent.click(screen.getByText("clear"));
    expect(screen.getByTestId("selected").textContent).toBe("none");
  });

  it("Provider の外では no-op (選択してもクラッシュせず常に未選択)", () => {
    render(<Probe />);
    expect(screen.getByTestId("selected").textContent).toBe("none");
    fireEvent.click(screen.getByText("select-jp"));
    expect(screen.getByTestId("selected").textContent).toBe("none");
  });

  it("hasMap を省略すると既定で false (地図 widget 無しの配置と同じ扱い)", () => {
    function HasMapProbe() {
      const { hasMap } = useDashboardSelection();
      return <span data-testid="has-map">{String(hasMap)}</span>;
    }
    render(
      <DashboardSelectionProvider>
        <HasMapProbe />
      </DashboardSelectionProvider>,
    );
    expect(screen.getByTestId("has-map").textContent).toBe("false");
  });

  it("hasMap を渡すと Provider 配下の消費者に反映される (DashboardPage がレイアウトから算出して渡す)", () => {
    function HasMapProbe() {
      const { hasMap } = useDashboardSelection();
      return <span data-testid="has-map">{String(hasMap)}</span>;
    }
    render(
      <DashboardSelectionProvider hasMap={true}>
        <HasMapProbe />
      </DashboardSelectionProvider>,
    );
    expect(screen.getByTestId("has-map").textContent).toBe("true");
  });
});
