"""段B-3: 問いの起草と資格判定 — 単発 (RFI) から常設への昇格関門。

設計 docs/pir_brief_design.md §6c「ユーザの任意の問いに対応できるか — 否、そして否でよい」:

> 作るべきは大きな型のライブラリではなく**この資格判定**であり、
> 通った問いを型へ写像する経路である。

**資格要件 6 つのうち 3 つは型の選択が構造的に保証する**:
- 要件 1 (競合仮説が立つ) — 型が仮説骨格を持つので、型を選んだ時点で満たされる
- 要件 4 (予測でない) — 予測の型を用意していないので、選びようがない
- 要件 5 (自分側データを要しない) — FFIR の型を用意していないので、同上

よって関門が機械的に見るのは要件 2 (証拠条件が宣言的) と 6 (決心が言える) で、
要件 3 (答えが時間で動きうる) だけが**機械に判定できない**。3 は人の明示的な確認を
求める (黙って通さない)。
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from typing import Any

import pytest

from src.assessment.question_draft import QuestionDraft, qualify, question_text
from src.assessment.question_frame import SCOPE_ALL_CI

_COND: dict[str, Any] = {
    "all": [
        {"property": "intent", "op": "eq", "value": "prepositioning"},
        {"property": "actor_nation", "op": "in", "value": ["cn"]},
    ]
}


def _draft(**over: Any) -> QuestionDraft:
    base: dict[str, Any] = {
        "frame_id": "presence",
        "slots": {
            "subject": "cn",
            "target_country": "JP",
            "target_scope": SCOPE_ALL_CI,
            "action": "prepositioning",
        },
        "decision": "警報を出すか / 監視態勢を上げるか",
        "evidence_condition": _COND,
        "answer_can_move_ack": True,
    }
    base.update(over)
    return QuestionDraft(**base)


class TestQualifyingDraftPasses:
    def test_complete_draft_qualifies(self) -> None:
        result = qualify(_draft())

        assert result.ok is True
        assert result.failures == ()

    def test_question_text_comes_from_the_frame(self) -> None:
        assert question_text(_draft()) == (
            "中国の国家アクターは日本の重要インフラに対する事前配置を進めているか"
        )


class TestRequirement6Decision:
    """要件 6 — どの決心を支えるかが言えない問いは常設にしない。"""

    def test_missing_decision_fails(self) -> None:
        result = qualify(_draft(decision=""))

        assert result.ok is False
        assert any("決心" in f for f in result.failures)

    def test_whitespace_only_decision_fails(self) -> None:
        assert qualify(_draft(decision="   ")).ok is False


class TestRequirement2DeclarativeEvidence:
    """要件 2 — どの記事が証拠かを機械判定できること。"""

    def test_unknown_property_fails(self) -> None:
        cond = {"property": "not_a_property", "op": "eq", "value": "x"}

        result = qualify(_draft(evidence_condition=cond))

        assert result.ok is False
        assert any("not_a_property" in f for f in result.failures)

    def test_wrong_operator_for_kind_fails(self) -> None:
        """set 型に eq は無い — 型と演算子の不一致を通さない。"""
        cond = {"property": "actor_nation", "op": "eq", "value": "cn"}

        assert qualify(_draft(evidence_condition=cond)).ok is False

    def test_empty_condition_fails(self) -> None:
        """空条件は routing では catch-all だが、問いの証拠としては全件該当で無意味。"""
        result = qualify(_draft(evidence_condition={}))

        assert result.ok is False
        assert any("証拠条件" in f for f in result.failures)


class TestRequirement3IsHumanJudgement:
    def test_unacknowledged_time_variance_fails(self) -> None:
        """機械に判定できない要件を、黙って通さない。"""
        result = qualify(_draft(answer_can_move_ack=False))

        assert result.ok is False
        assert any("時間で動き" in f for f in result.failures)


class TestFrameAndSlots:
    def test_unknown_frame_fails(self) -> None:
        result = qualify(_draft(frame_id="prediction"))

        assert result.ok is False
        assert any("型" in f for f in result.failures)

    def test_missing_slot_fails(self) -> None:
        result = qualify(_draft(slots={"subject": "cn"}))

        assert result.ok is False
        assert any("スロット" in f for f in result.failures)

    def test_slot_value_outside_vocabulary_fails(self) -> None:
        """語彙外の値は**常設では**通さない — 問い文に生値が出るのを防ぐ。

        表示側は生値に fallback するが (画面を壊さないため)、台帳に載る問いは
        語彙統制の内側でなければならない。
        """
        result = qualify(_draft(slots={**_draft().slots, "subject": "zz"}))

        assert result.ok is False
        assert any("zz" in f for f in result.failures)

    def test_all_failures_are_reported_not_just_the_first(self) -> None:
        """起草者が 1 つ直すたびに次が出る、を避ける。"""
        result = qualify(_draft(decision="", answer_can_move_ack=False))

        assert len(result.failures) >= 2


class TestStructurallyGuaranteedRequirements:
    """要件 1/4/5 は型の選択が保証する — 関門で再判定しない。"""

    def test_every_frame_supplies_competing_hypotheses(self) -> None:
        """要件 1: 型を選んだ時点で競合仮説が付く。"""
        from src.assessment.question_frame import FRAME_BY_ID

        assert all(len(f.hypotheses) >= 3 for f in FRAME_BY_ID.values())

    def test_no_prediction_or_own_side_frame_exists(self) -> None:
        """要件 4/5: 予測・FFIR の型を用意しない = 選べない (禁止を構造で担保)。

        原則 G (予測しない) / 原則 F (自分側データを持たない) は、指示ではなく
        **型の不在**で守る。
        """
        from src.assessment.question_frame import FRAME_BY_ID

        assert not {"prediction", "forecast", "exposure", "ffir"} & set(FRAME_BY_ID)


class TestDraftIsData:
    """§6e — 問いはデータ。コードに焼き込まない。"""

    def test_draft_is_serializable_to_plain_dict(self) -> None:
        from dataclasses import asdict

        d = asdict(_draft())

        assert d["frame_id"] == "presence"
        assert d["slots"]["subject"] == "cn"

    def test_draft_is_immutable(self) -> None:
        draft = _draft()

        with pytest.raises(FrozenInstanceError):
            draft.decision = "別の決心"  # type: ignore[misc]


class TestHarvestableProperties:
    """段B-3c: 証拠条件は**収穫時に読める**プロパティだけで書けること。

    収穫は articles の数列 + entity キーしか見ない。そこに無い property
    (kev / keyword_list / llm_* 等) を条件に書くと、**静かに全件不一致**になり
    「器はあるが証拠が来ない問い」ができる。起草時に弾く (関門で止める)。
    """

    def test_non_harvestable_property_fails(self) -> None:
        cond = {"property": "kev", "op": "is_true", "value": True}

        result = qualify(_draft(evidence_condition=cond))

        assert result.ok is False
        assert any("収穫" in f and "kev" in f for f in result.failures)

    def test_nested_non_harvestable_property_is_caught(self) -> None:
        """入れ子の奥でも見逃さない。"""
        cond = {
            "all": [
                {"property": "intent", "op": "eq", "value": "espionage"},
                {"any": [{"property": "keyword_list", "op": "in", "value": ["x"]}]},
            ]
        }

        assert qualify(_draft(evidence_condition=cond)).ok is False

    def test_all_frames_can_be_expressed_with_harvestable_properties(self) -> None:
        """A/E/H の証拠条件が allowlist の内側で書けること (型と収穫の整合)。"""
        from src.assessment.question_draft import HARVESTABLE_PROPERTIES

        assert {
            "intent",
            "victim_country",
            "victim_sector",
            "actor_nation",
            "involved_country",
            "importance",
            "category",
        } <= HARVESTABLE_PROPERTIES

    def test_harvestable_set_is_a_subset_of_the_catalog(self) -> None:
        """カタログに無い名前を allowlist に書いても意味がない (綴り間違いの検知)。"""
        from src.assessment.question_draft import HARVESTABLE_PROPERTIES
        from src.cti.routing_rules import PROPERTY_BY_ID

        assert set(PROPERTY_BY_ID) >= HARVESTABLE_PROPERTIES

    def test_literal_valued_properties_without_a_safe_blank_are_excluded(self) -> None:
        """既定値を持たない Literal (article_type / stance) は allowlist に入れない。

        収穫の signals ではこの 2 つに固定値を詰めている (型を満たすためだけ)。
        allowlist に入れると、DB 値が空の行 (実測 0.16% / 0.3%) がその固定値を指す
        条件に**誤って一致する**。入れるなら先に番兵の問題を解くこと。
        """
        from src.assessment.question_draft import HARVESTABLE_PROPERTIES

        assert not {"article_type", "stance"} & HARVESTABLE_PROPERTIES
