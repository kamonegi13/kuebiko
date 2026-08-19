// block 方式プロンプト編集 draft hook の回帰テスト。
// summarizer 側 (useRubricDraft.test.tsx) と同じ動機:
//   配列順の保持 — 表示は slots 順に並べ替えるが draft.sections 自体(保存ペイロード)の
//   GET 順は変えない。更新が index ベースに変わった瞬間に別 block を書き潰す事故を防ぐ。
//   重複 block id の修復手段 — 更新キーが field_id なので重複カードは相互汚染し、
//   backend も保存を拒否する。画面から直せることをここで保証する。

import { describe, expect, it } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createElement, type ReactNode } from "react";
import { useBlockDraft } from "./useBlockDraft";
import type { BlockRubric } from "../../../api/promptBlocks";
import type { RubricSection } from "../../../api/promptRubric";

function block(fieldId: string, body: string): RubricSection {
  return { field_id: fieldId, kind: "rubric", title: fieldId, body, note: "" };
}

function rubric(sections: RubricSection[]): BlockRubric {
  return { schema_version: 1, intro: "", sections, examples: [] };
}

function wrapper({ children }: { children: ReactNode }) {
  // preview は 600ms デバウンス後にしか発火しないので、テスト中は通信が起きない。
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
  return createElement(QueryClientProvider, { client }, children);
}

function render(promptId: string, saved: BlockRubric) {
  return renderHook(({ id, s }: { id: string; s: BlockRubric }) => useBlockDraft(id, s), {
    wrapper,
    initialProps: { id: promptId, s: saved },
  });
}

describe("useBlockDraft の配列順の保持", () => {
  it("本文を編集しても sections の並びは変わらない", () => {
    // Arrange
    const saved = rubric([block("mission", "a"), block("purpose", "b"), block("outlook", "c")]);
    const { result } = render("status_synthesis", saved);

    // Act: 真ん中のカードを編集
    act(() => result.current.updateBlockBody("purpose", "編集後"));

    // Assert: 並びは GET 順のまま、対象 block だけが変わる
    expect(result.current.draft?.sections.map((s) => s.field_id)).toEqual([
      "mission",
      "purpose",
      "outlook",
    ]);
    expect(result.current.draft?.sections[1].body).toBe("編集後");
    expect(result.current.draft?.sections.map((s) => s.body)).toEqual(["a", "編集後", "c"]);
  });

  it("取消は対象 block だけを保存値へ戻す", () => {
    // Arrange
    const saved = rubric([block("mission", "a"), block("purpose", "b")]);
    const { result } = render("status_synthesis", saved);
    act(() => result.current.updateBlockBody("mission", "x"));
    act(() => result.current.updateBlockBody("purpose", "y"));

    // Act
    act(() => result.current.revertBlock("mission"));

    // Assert
    expect(result.current.draft?.sections.map((s) => s.body)).toEqual(["a", "y"]);
    expect(result.current.dirty).toBe(true);
  });

  it("編集が無ければ dirty にならない", () => {
    const { result } = render("status_synthesis", rubric([block("mission", "a")]));
    expect(result.current.dirty).toBe(false);
  });
});

describe("useBlockDraft の重複 block id", () => {
  const duplicated = () =>
    rubric([block("mission", "a"), block("purpose", "b"), block("mission", "a2")]);

  it("重複している block id を検出する", () => {
    const { result } = render("status_synthesis", duplicated());
    expect([...result.current.duplicateBlockIds]).toEqual(["mission"]);
  });

  it("重複が無ければ空集合", () => {
    const { result } = render("status_synthesis", rubric([block("mission", "a"), block("purpose", "b")]));
    expect(result.current.duplicateBlockIds.size).toBe(0);
  });

  it("削除は index 指定で、同じ block id のもう一方を巻き込まない", () => {
    // Arrange
    const { result } = render("status_synthesis", duplicated());

    // Act: 後ろ側 (index 2) だけを消す
    act(() => result.current.removeBlockAt(2));

    // Assert
    expect(result.current.draft?.sections.map((s) => s.field_id)).toEqual(["mission", "purpose"]);
    expect(result.current.draft?.sections[0].body).toBe("a");
  });

  it("一意な block id のカードは削除できない (skeleton slot との不変量を壊さない安全弁)", () => {
    // Arrange
    const { result } = render("status_synthesis", duplicated());

    // Act: purpose は重複していないので消えてはいけない
    act(() => result.current.removeBlockAt(1));

    // Assert
    expect(result.current.draft?.sections.map((s) => s.field_id)).toEqual([
      "mission",
      "purpose",
      "mission",
    ]);
  });

  it("範囲外の index は無視する", () => {
    const { result } = render("status_synthesis", duplicated());
    act(() => result.current.removeBlockAt(99));
    expect(result.current.draft?.sections).toHaveLength(3);
  });
});
