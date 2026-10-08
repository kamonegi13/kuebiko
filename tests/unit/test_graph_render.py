"""src/graph/render.py の節の組み立て (GraphRAG 共通、2026-10-08) — 純粋関数。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from src.graph.render import (
    EventContext,
    PriorityHint,
    clip_sentence,
    render_relation_section,
)

END = datetime(2026, 9, 5, tzinfo=UTC)


@dataclass(frozen=True)
class Rel:
    a: str
    b: str
    rel_type: str
    basis: tuple[str, ...] = ()
    extra: dict[str, str] = field(default_factory=dict)


def _base_kwargs(**overrides: Any) -> dict[str, Any]:
    kw: dict[str, Any] = {
        "members": {"art1": ["E1"]},
        "window_end": END,
        "actor_name": lambda v: v.upper(),
    }
    kw.update(overrides)
    return kw


class TestClipSentence:
    def test_short_text_is_unchanged(self) -> None:
        assert clip_sentence("短い文。") == "短い文。"

    def test_cuts_at_sentence_boundary_not_mid_word(self) -> None:
        text = "あ" * 100 + "。" + "い" * 100 + "。"

        out = clip_sentence(text, limit=120)

        assert out == "あ" * 100 + "。"

    def test_falls_back_to_ellipsis_when_no_boundary_found(self) -> None:
        text = "あ" * 200

        out = clip_sentence(text, limit=120)

        assert out.endswith("…")
        assert len(out) == 121


class TestEnrichedLine:
    def test_other_event_kind_sector_country_and_summary_are_shown(self) -> None:
        rels = {"E1": [Rel("E1", "E9", "same_actor", ("actor:apt1",))]}
        first = {"E9": datetime(2026, 8, 1, tzinfo=UTC)}
        ctx = {
            "E9": EventContext(
                kind="exploitation",
                sector="finance",
                country="JP",
                summary="攻撃者が金融機関を狙った。",
            )
        }

        text = render_relation_section(
            ["art1"],
            relations=rels,
            first_reported=first,
            headlines={"E9": "過去の事象"},
            event_context=ctx,
            **_base_kwargs(),
        )

        assert "種別: exploitation" in text
        assert "被害: finance/JP" in text
        assert "攻撃者が金融機関を狙った。" in text

    def test_gap_label_shown_when_own_event_first_reported_is_known(self) -> None:
        rels = {"E1": [Rel("E1", "E9", "same_actor", ("actor:apt1",))]}
        first = {
            "E1": datetime(2026, 8, 20, tzinfo=UTC),
            "E9": datetime(2026, 8, 1, tzinfo=UTC),
        }

        text = render_relation_section(
            ["art1"],
            relations=rels,
            first_reported=first,
            headlines={"E9": "過去の事象"},
            **_base_kwargs(),
        )

        assert "(19 日前)" in text

    def test_no_gap_label_when_own_event_first_reported_is_unknown(self) -> None:
        """旧 render_graph_context との互換: ev 側の first_reported が無ければ時間差を出さない。"""
        rels = {"E1": [Rel("E1", "E9", "same_actor", ("actor:apt1",))]}
        first = {"E9": datetime(2026, 8, 1, tzinfo=UTC)}

        text = render_relation_section(
            ["art1"],
            relations=rels,
            first_reported=first,
            headlines={"E9": "過去の事象"},
            **_base_kwargs(),
        )

        assert "日前" not in text
        assert "日後" not in text

    def test_rarity_note_appended_to_shared_basis(self) -> None:
        rels = {"E1": [Rel("E1", "E2", "follow_up", ("cve:CVE-2026-1",))]}
        first = {"E2": datetime(2026, 9, 1, tzinfo=UTC)}

        text = render_relation_section(
            ["art1", "art2"],
            members={"art1": ["E1"], "art2": ["E2"]},
            relations=rels,
            first_reported=first,
            headlines={},
            indicator_rarity={"cve:CVE-2026-1": 3},
            rarity_window_days=60,
            window_end=END,
            actor_name=str,
        )

        assert "CVE: CVE-2026-1 (この 60 日で 3 事象)" in text

    def test_incident_confidence_word_from_probability(self) -> None:
        rels = {"E1": [Rel("E1", "E2", "incident", (), {"p": "0.91"})]}
        first = {"E2": datetime(2026, 9, 1, tzinfo=UTC)}

        text = render_relation_section(
            ["art1", "art2"],
            members={"art1": ["E1"], "art2": ["E2"]},
            relations=rels,
            first_reported=first,
            headlines={},
            window_end=END,
            actor_name=str,
        )

        assert "(確度: 強)" in text

    def test_same_actor_legend_shown_only_when_same_actor_lines_present(self) -> None:
        rels = {"E1": [Rel("E1", "E9", "same_actor", ("actor:apt1",))]}
        first = {"E9": datetime(2026, 8, 1, tzinfo=UTC)}

        text = render_relation_section(
            ["art1"],
            relations=rels,
            first_reported=first,
            headlines={"E9": "過去の事象"},
            **_base_kwargs(),
        )

        assert "攻撃者名の共有だけを示す" in text

    def test_no_same_actor_legend_when_no_same_actor_lines(self) -> None:
        rels = {"E1": [Rel("E1", "E2", "follow_up", ("victim:acme",))]}
        first = {"E2": datetime(2026, 9, 1, tzinfo=UTC)}

        text = render_relation_section(
            ["art1", "art2"],
            members={"art1": ["E1"], "art2": ["E2"]},
            relations=rels,
            first_reported=first,
            headlines={},
            window_end=END,
            actor_name=str,
        )

        assert "攻撃者名の共有だけを示す" not in text


class TestHubCap:
    def test_same_actor_lines_capped_per_actor_across_section(self) -> None:
        rels = {"E1": [Rel("E1", f"X{i}", "same_actor", ("actor:apt1",)) for i in range(10)]}
        first = {f"X{i}": datetime(2026, 8, 1, tzinfo=UTC) for i in range(10)}
        heads = {f"X{i}": f"事象 {i}" for i in range(10)}

        text = render_relation_section(
            ["art1"],
            relations=rels,
            first_reported=first,
            headlines=heads,
            same_actor_cap=3,
            **_base_kwargs(),
        )

        assert text.count("\n- ") == 3

    def test_different_actors_are_not_capped_together(self) -> None:
        rels = {"E1": [Rel("E1", f"X{i}", "same_actor", (f"actor:apt{i}",)) for i in range(10)]}
        first = {f"X{i}": datetime(2026, 8, 1, tzinfo=UTC) for i in range(10)}
        heads = {f"X{i}": f"事象 {i}" for i in range(10)}

        text = render_relation_section(
            ["art1"],
            relations=rels,
            first_reported=first,
            headlines=heads,
            same_actor_cap=3,
            **_base_kwargs(),
        )

        assert text.count("\n- ") == 10

    def test_incident_lines_are_not_hub_capped(self) -> None:
        rels = {"E1": [Rel("E1", f"X{i}", "incident", (), {"p": "0.9"}) for i in range(10)]}
        first = {f"X{i}": datetime(2026, 8, 1, tzinfo=UTC) for i in range(10)}
        heads = {f"X{i}": f"事象 {i}" for i in range(10)}

        text = render_relation_section(
            ["art1"],
            relations=rels,
            first_reported=first,
            headlines=heads,
            same_actor_cap=3,
            **_base_kwargs(),
        )

        assert text.count("\n- ") == 10


class TestPriority:
    def test_priority_matching_actor_sorts_before_non_matching(self) -> None:
        rels = {
            "E1": [
                Rel("E1", "X0", "same_actor", ("actor:apt_other",)),
                Rel("E1", "X1", "same_actor", ("actor:apt_target",)),
            ]
        }
        first = {
            "X0": datetime(2026, 8, 20, tzinfo=UTC),  # より新しいが優先対象でない
            "X1": datetime(2026, 8, 1, tzinfo=UTC),
        }
        heads = {"X0": "別アクターの事象", "X1": "狙った事象"}

        text = render_relation_section(
            ["art1"],
            relations=rels,
            first_reported=first,
            headlines=heads,
            priority=PriorityHint(actor_ids=frozenset({"apt_target"})),
            **_base_kwargs(),
        )

        assert text.index("狙った事象") < text.index("別アクターの事象")


def test_flag_not_imported_here_but_insert_after_candidates_still_works() -> None:
    from src.graph.render import insert_after_candidates

    prompt = "前置き\n## この SIR にマッチした直近の article (2 件)\n[1] a\n\n## 前期\nx"

    out = insert_after_candidates(prompt, "## 線\n- l\n\n")

    assert out.index("[1] a") < out.index("## 線") < out.index("## 前期")


class TestSameNationLegend:
    def test_same_nation_line_adds_its_legend_once(self) -> None:
        rels = {
            "E1": [
                Rel("E1", "E8", "same_nation", ("nation:kp",)),
                Rel("E1", "E9", "same_nation", ("nation:kp",)),
            ]
        }
        kw = _base_kwargs(
            relations=rels,
            first_reported={
                "E1": END.replace(day=1),
                "E8": END.replace(day=2),
                "E9": END.replace(day=3),
            },
            headlines={"E8": "別の事象 8", "E9": "別の事象 9"},
        )

        out = render_relation_section(["art1"], **kw)

        assert out.count("「同じ帰属国」の線は") == 1
        assert "「同じアクター」の線は" not in out


class TestCounterpartMerge:
    def test_lines_to_same_outside_event_are_merged_with_all_origins(self) -> None:
        rels = {
            "E1": [Rel("E1", "E9", "same_nation", ("nation:kp",))],
            "E2": [Rel("E2", "E9", "same_nation", ("nation:kp",))],
        }
        kw = _base_kwargs(
            members={"art1": ["E1"], "art2": ["E2"]},
            relations=rels,
            first_reported={
                "E1": END.replace(day=1),
                "E2": END.replace(day=2),
                "E9": END.replace(day=3),
            },
            headlines={"E9": "候補外の事象 9"},
        )

        out = render_relation_section(["art1", "art2"], **kw)

        lines = [ln for ln in out.splitlines() if ln.startswith("- ")]
        assert len(lines) == 1
        assert lines[0].startswith("- 記事 [1][2] ↔ 候補外の事象「候補外の事象 9」")
        assert "日後" not in lines[0]


class TestSameNationScope:
    def test_same_nation_between_candidates_is_dropped(self) -> None:
        rels = {
            "E1": [Rel("E1", "E2", "same_nation", ("nation:kp",))],
            "E2": [Rel("E1", "E2", "same_nation", ("nation:kp",))],
        }
        kw = _base_kwargs(
            members={"art1": ["E1"], "art2": ["E2"]},
            relations=rels,
            first_reported={"E1": END.replace(day=1), "E2": END.replace(day=2)},
            headlines={},
        )

        assert render_relation_section(["art1", "art2"], **kw) == ""

    def test_same_nation_lines_are_capped(self) -> None:
        from src.graph.render import SAME_NATION_CAP

        n = SAME_NATION_CAP + 3
        rels = {"E1": [Rel("E1", f"X{i}", "same_nation", ("nation:kp",)) for i in range(n)]}
        first = {"E1": END.replace(day=1)} | {f"X{i}": END.replace(day=2) for i in range(n)}
        kw = _base_kwargs(
            relations=rels,
            first_reported=first,
            headlines={f"X{i}": f"事象 {i}" for i in range(n)},
        )

        out = render_relation_section(["art1"], **kw)

        assert sum(1 for ln in out.splitlines() if ln.startswith("- ")) == SAME_NATION_CAP

    def test_same_nation_keeps_its_slots_when_same_actor_fills_the_section(self) -> None:
        rels = {
            "E1": [Rel("E1", f"A{i}", "same_actor", (f"actor:x{i}",)) for i in range(4)]
            + [Rel("E1", "N1", "same_nation", ("nation:kp",))]
        }
        ids = [f"A{i}" for i in range(4)] + ["N1"]
        kw = _base_kwargs(
            relations=rels,
            first_reported={"E1": END.replace(day=1)} | {i: END.replace(day=2) for i in ids},
            headlines={i: f"事象 {i}" for i in ids},
            max_lines=3,
        )

        out = render_relation_section(["art1"], **kw)

        lines = [ln for ln in out.splitlines() if ln.startswith("- ")]
        assert len(lines) == 3
        assert "同じ帰属国" in lines[-1]
