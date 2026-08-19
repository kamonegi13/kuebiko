// summarizer 判定基準エディタの draft hook の回帰テスト。
//
// この repo で最初の frontend テスト。動機は 2 つ:
//   I-1 保存ペイロードの sections 配列順は GET 順を保持する — 表示側で並べ替え・
//        検索絞り込みをしているため、更新が index ベースに変わった瞬間に別フィールドを
//        書き潰す。人手では気付けないので機械で固定する。
//   重複 field_id の修復手段 — 更新キーが field_id なので重複カードは相互汚染し、
//        backend も保存を拒否する。画面から直せることをここで保証する。

import { describe, expect, it } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createElement, type ReactNode } from "react";
import { useRubricDraft } from "./useRubricDraft";
import type { RubricSection, SummarizerRubric } from "../../../api/promptRubric";

function section(fieldId: string, body: string): RubricSection {
  return { field_id: fieldId, kind: "rubric", title: fieldId, body, note: "" };
}

function rubric(sections: RubricSection[]): SummarizerRubric {
  return {
    schema_version: 1,
    intro: "intro",
    sections,
    examples: [{ label: "例1", json_text: "{}" }],
  };
}

function wrapper({ children }: { children: ReactNode }) {
  // preview は 600ms デバウンス後にしか発火しないので、テスト中は通信が起きない
  // (retry も切って、万一発火しても即座に終わるようにする)。
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
  return createElement(QueryClientProvider, { client }, children);
}

function render(saved: SummarizerRubric) {
  return renderHook(() => useRubricDraft(saved), { wrapper });
}

describe("useRubricDraft の配列順 (I-1)", () => {
  it("本文を編集しても sections の並びは変わらない", () => {
    // Arrange
    const saved = rubric([section("summary", "a"), section("title_ja", "b"), section("iocs", "c")]);
    const { result } = render(saved);

    // Act: 真ん中のカードを編集
    act(() => result.current.updateSectionBody("title_ja", "編集後"));

    // Assert: 並びは GET 順のまま、対象フィールドだけが変わる
    expect(result.current.draft?.sections.map((s) => s.field_id)).toEqual([
      "summary",
      "title_ja",
      "iocs",
    ]);
    expect(result.current.draft?.sections[1].body).toBe("編集後");
    expect(result.current.draft?.sections.map((s) => s.body)).toEqual(["a", "編集後", "c"]);
  });

  it("取消は対象フィールドだけを保存値へ戻す", () => {
    // Arrange
    const saved = rubric([section("summary", "a"), section("title_ja", "b")]);
    const { result } = render(saved);
    act(() => result.current.updateSectionBody("summary", "x"));
    act(() => result.current.updateSectionBody("title_ja", "y"));

    // Act
    act(() => result.current.revertSection("summary"));

    // Assert
    expect(result.current.draft?.sections.map((s) => s.body)).toEqual(["a", "y"]);
    expect(result.current.dirty).toBe(true);
  });

  it("編集が無ければ dirty にならない", () => {
    const { result } = render(rubric([section("summary", "a")]));
    expect(result.current.dirty).toBe(false);
  });
});

describe("useRubricDraft の重複 field_id", () => {
  const duplicated = () =>
    rubric([section("summary", "a"), section("title_ja", "b"), section("summary", "a2")]);

  it("重複している field_id を検出する", () => {
    const { result } = render(duplicated());
    expect([...result.current.duplicateFieldIds]).toEqual(["summary"]);
  });

  it("重複が無ければ空集合", () => {
    const { result } = render(rubric([section("summary", "a"), section("title_ja", "b")]));
    expect(result.current.duplicateFieldIds.size).toBe(0);
  });

  it("削除は index 指定で、同じ field_id のもう一方を巻き込まない", () => {
    // Arrange
    const { result } = render(duplicated());

    // Act: 後ろ側 (index 2) だけを消す
    act(() => result.current.removeSectionAt(2));

    // Assert
    expect(result.current.draft?.sections.map((s) => s.field_id)).toEqual(["summary", "title_ja"]);
    expect(result.current.draft?.sections[0].body).toBe("a");
  });

  it("一意な field_id のカードは削除できない (schema の不変量を壊さない安全弁)", () => {
    // Arrange
    const { result } = render(duplicated());

    // Act: title_ja は重複していないので消えてはいけない
    act(() => result.current.removeSectionAt(1));

    // Assert
    expect(result.current.draft?.sections.map((s) => s.field_id)).toEqual([
      "summary",
      "title_ja",
      "summary",
    ]);
  });

  it("範囲外の index は無視する", () => {
    const { result } = render(duplicated());
    act(() => result.current.removeSectionAt(99));
    expect(result.current.draft?.sections).toHaveLength(3);
  });
});
