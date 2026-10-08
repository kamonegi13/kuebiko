"""Spotlight の「線でたどった関連事象」の節 (GraphRAG、2026-09-29)。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from src.spotlight.graph_context import (
    graph_context_enabled,
    insert_after_candidates,
    render_graph_context,
)

END = datetime(2026, 9, 5, tzinfo=UTC)


@dataclass(frozen=True)
class Rel:
    a: str
    b: str
    rel_type: str
    basis: tuple[str, ...] = ()
    extra: dict[str, str] = field(default_factory=dict)


def _render(rels: dict[str, list[Any]], first: dict[str, datetime], **kw: Any) -> str:
    members = {"art1": ["E1"], "art2": ["E2"]}
    heads = {"E9": "過去の事象", "E2": "候補 2 の事象"}
    return render_graph_context(
        ["art1", "art2"],
        members=members,
        relations=rels,
        first_reported=first,
        headlines=heads,
        window_end=END,
        actor_name=lambda v: v.upper(),
        **kw,
    )


class TestRender:
    def test_line_to_outside_event_carries_headline_date_and_basis(self) -> None:
        rels = {"E1": [Rel("E1", "E9", "same_actor", ("actor:apt1",))]}
        first = {"E9": datetime(2026, 8, 1, tzinfo=UTC)}

        text = _render(rels, first)

        assert "記事 [1] ↔ 候補外の事象「過去の事象」(2026-08-01): 同じアクター" in text
        assert "攻撃者: APT1" in text
        assert "線に無い事象どうしの関係は推測しない" in text

    def test_line_between_candidates_uses_article_numbers(self) -> None:
        rels = {"E1": [Rel("E1", "E2", "follow_up", ("victim:acme",))]}
        first = {"E2": datetime(2026, 9, 1, tzinfo=UTC)}

        assert "記事 [1] ↔ 記事 [2]: 続報 (共有: 被害組織: acme)" in _render(rels, first)

    def test_events_reported_after_the_window_are_excluded(self) -> None:
        """今の関係表から引くので、窓より後の事象を混ぜない (未来の混入)。"""
        rels = {"E1": [Rel("E1", "E9", "same_actor", ("actor:apt1",))]}
        first = {"E9": datetime(2026, 9, 10, tzinfo=UTC)}

        assert _render(rels, first) == ""

    def test_event_lines_come_before_same_actor_lines(self) -> None:
        rels = {
            "E1": [
                Rel("E1", "E9", "same_actor", ("actor:apt1",)),
                Rel("E1", "E2", "incident", ()),
            ]
        }
        first = {"E9": datetime(2026, 8, 30, tzinfo=UTC), "E2": datetime(2026, 8, 1, tzinfo=UTC)}

        text = _render(rels, first)

        assert text.index("同じ出来事の関連") < text.index("同じアクター")

    def test_line_cap(self) -> None:
        # アクターを散らす (別名の「同じアクター」40 本) — ハブ抑制 (同じアクターは節全体で
        # 上限 SAME_ACTOR_HUB_CAP 本) に巻き込まれず、max_lines による切り詰めだけを見る
        rels = {"E1": [Rel("E1", f"X{i}", "same_actor", (f"actor:apt{i}",)) for i in range(40)]}
        first = {f"X{i}": datetime(2026, 8, 1, tzinfo=UTC) for i in range(40)}
        heads = {f"X{i}": f"事象 {i}" for i in range(40)}

        text = render_graph_context(
            ["art1"],
            members={"art1": ["E1"]},
            relations=rels,
            first_reported=first,
            headlines=heads,
            window_end=END,
            actor_name=str,
            max_lines=25,
        )

        assert text.count("\n- ") == 25
        assert "(25 本 / 全 40 本)" in text


class TestInsert:
    def test_inserted_after_candidate_list_before_next_section(self) -> None:
        prompt = "前置き\n## この SIR にマッチした直近の article (2 件)\n[1] a\n\n## 前期\nx"

        out = insert_after_candidates(prompt, "## 線\n- l\n\n")

        assert out.index("[1] a") < out.index("## 線") < out.index("## 前期")

    def test_empty_block_leaves_prompt_unchanged(self) -> None:
        assert insert_after_candidates("p", "") == "p"


def test_flag_defaults_off(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("SPOTLIGHT_GRAPH_CONTEXT", raising=False)
    assert graph_context_enabled() is False
    monkeypatch.setenv("SPOTLIGHT_GRAPH_CONTEXT", "1")
    assert graph_context_enabled() is True
