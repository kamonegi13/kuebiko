"""段B-2a: 問いの型 (QuestionFrame) — 文型 + スロット + 仮説骨格。

設計: docs/pir_brief_design.md §6d/§6e。

**この抽象が正しいかの判定は 1 つ**: 既存の常設 4 問を frame から組み立てて
**現行の問い文と 1 文字も違わず再現できるか**。再現できないなら、それは現実に合って
いない抽象であり、E/H を載せる土台にならない。

**§6e (公開ツール制約) の要件**: コードが所有するのは型だけ。スロット値・決心はデータ。
よって frame は「決心が要る」と宣言するだけで、決心の**一覧を持たない**。
"""

from __future__ import annotations

import pytest

from src.assessment.question_frame import (
    FRAME_BY_ID,
    FRAME_PRESENCE,
    SCOPE_ALL_CI,
    render_question,
)

#: 現行 STANDING_SEEDS の問い文 (src/assessment/standing.py の SSoT と一致させる)。
_EXISTING_TITLES = {
    "cn": "中国の国家アクターは日本の重要インフラに対する事前配置を進めているか",
    "kp": "北朝鮮の国家アクターは日本の重要インフラに対する事前配置を進めているか",
    "ru": "ロシアの国家アクターは日本の重要インフラに対する事前配置を進めているか",
    "ir": "イランの国家アクターは日本の重要インフラに対する事前配置を進めているか",
}


def _presence_values(nation: str) -> dict[str, str]:
    return {
        "subject": nation,
        "target_country": "JP",
        "target_scope": SCOPE_ALL_CI,
        "action": "prepositioning",
    }


class TestReproducesExistingQuestions:
    """抽象が現実に合っているかの唯一の判定。"""

    @pytest.mark.parametrize("nation", sorted(_EXISTING_TITLES))
    def test_renders_existing_seed_title_verbatim(self, nation: str) -> None:
        assert render_question(FRAME_PRESENCE, _presence_values(nation)) == _EXISTING_TITLES[nation]

    def test_seed_titles_in_code_still_match_this_expectation(self) -> None:
        """standing.py 側が変わったらこのテストが気付く (二重管理の検知)。

        frame へ seed を移すのは後段 (§6e)。それまでは両方が同じ文を持つので、
        片方だけ変えたら落ちる関門を置く。
        """
        from src.assessment.standing import STANDING_SEEDS

        actual = {s.nation: s.title for s in STANDING_SEEDS}

        assert actual == _EXISTING_TITLES


class TestSlotVocabularyIsSSoT:
    """スロット値は既存の語彙辞書から引く (複製辞書を作らない)。"""

    def test_country_label_comes_from_vocab(self) -> None:
        assert render_question(FRAME_PRESENCE, _presence_values("cn")).startswith("中国")

    def test_intent_label_comes_from_diamond_model(self) -> None:
        from src.cti.diamond_model import INTENT_LABELS_JA

        rendered = render_question(FRAME_PRESENCE, _presence_values("cn"))

        assert INTENT_LABELS_JA["prepositioning"] in rendered

    def test_every_slot_declares_a_domain(self) -> None:
        """domain 無しのスロットを許すと、そこだけ語彙統制が効かなくなる。"""
        for frame in FRAME_BY_ID.values():
            assert all(s.domain for s in frame.slots), frame.frame_id

    def test_unknown_value_falls_back_to_raw_not_crash(self) -> None:
        """語彙に無い値でも問い文は出す (編集途中の値で画面を壊さない)。"""
        values = {**_presence_values("cn"), "subject": "zz"}

        assert "zz" in render_question(FRAME_PRESENCE, values)


class TestFrameContract:
    def test_missing_slot_value_raises(self) -> None:
        """穴が埋まっていない問いを黙って作らせない (§6d: 問いは不変、動くのは答え)。"""
        with pytest.raises(KeyError):
            render_question(FRAME_PRESENCE, {"subject": "cn"})

    def test_frame_carries_hypotheses(self) -> None:
        from src.synthesis.grounded.hypotheses import POSTURE_HYPOTHESES

        assert FRAME_PRESENCE.hypotheses == POSTURE_HYPOTHESES

    def test_frame_requires_a_decision_but_does_not_enumerate_them(self) -> None:
        """要件 6: 決心との紐付けは必須。ただし決心の一覧はコードが持たない (§6e)。

        運用者ごとに決心が違うため、frame は「要る」と宣言するだけにする。
        """
        assert FRAME_PRESENCE.requires_decision is True
        assert not hasattr(FRAME_PRESENCE, "decisions")

    def test_frame_ids_are_unique(self) -> None:
        assert len(FRAME_BY_ID) == len({f.frame_id for f in FRAME_BY_ID.values()})

    def test_template_placeholders_match_declared_slots(self) -> None:
        """文型の穴とスロット定義がずれると、埋め忘れが実行時まで出ない。"""
        import string

        for frame in FRAME_BY_ID.values():
            placeholders = {
                name for _, name, _, _ in string.Formatter().parse(frame.template) if name
            }

            assert placeholders == {s.slot_id for s in frame.slots}, frame.frame_id


class TestFrameIsGenericNotUseCaseSpecific:
    """§6e: コードが所有するのは汎用フレームだけ。用途固有はデータ。"""

    def test_template_has_no_hardcoded_country_or_sector(self) -> None:
        """「日本」「重要インフラ」が文型に焼き込まれていないこと。

        焼き込むと、別用途の運用者がこの型を使えない (公開ツールの制約)。
        """
        for frame in FRAME_BY_ID.values():
            for token in ("日本", "重要インフラ", "中国", "北朝鮮", "ロシア", "イラン"):
                assert token not in frame.template, f"{frame.frame_id}: {token}"

    def test_frame_module_owns_no_question_instances(self) -> None:
        """型モジュールが具体的な問い (seed) を持たないこと。"""
        import src.assessment.question_frame as qf

        assert not [n for n in dir(qf) if "SEED" in n.upper()]


class TestTrendFrame:
    """型 E 趨勢 — 観測バイアスが**一級の競合仮説**であること (CLAUDE.md §7)。"""

    def test_renders_readable_question(self) -> None:
        from src.assessment.question_frame import FRAME_TREND

        rendered = render_question(
            FRAME_TREND,
            {"target_country": "JP", "target_scope": SCOPE_ALL_CI, "threat": "espionage"},
        )

        assert rendered == "日本の重要インフラに対する諜報・情報窃取は悪化しているか"

    def test_observation_artifact_is_a_first_class_hypothesis(self) -> None:
        """「収集量を重要性の代理にしない」— 観測の変化を必ず競わせる。

        これを外すと E は「記事が増えた=悪化した」を言う機械になる。
        """
        from src.assessment.question_frame import FRAME_TREND

        assert any("observation" in h.id for h in FRAME_TREND.hypotheses)

    def test_offers_improvement_not_only_worsening(self) -> None:
        """方向中立 — 悪化だけを仮説に置くと脅威過大に倒れる (POSTURE と同じ規律)。"""
        from src.assessment.question_frame import FRAME_TREND

        ids = {h.id for h in FRAME_TREND.hypotheses}

        assert {"trend_worsening", "trend_flat", "trend_improving"} <= ids


class TestThresholdFrame:
    """型 H 閾値・段階 — 決心 (警報・態勢) に直結する型。"""

    def test_renders_readable_question(self) -> None:
        from src.assessment.question_frame import FRAME_THRESHOLD

        rendered = render_question(
            FRAME_THRESHOLD,
            {"subject": "cn", "target_country": "JP", "target_scope": SCOPE_ALL_CI},
        )

        assert rendered == "中国による日本の重要インフラへの活動は平時の水準を越えたか"

    def test_detection_improvement_is_a_competing_hypothesis(self) -> None:
        """「見えるようになっただけ」を競わせる — E の観測バイアスとは別物。

        E = 我々の網・世界の報道量が変わった (集計の歪み)
        H = 我々の検知能力が上がった (同じ活動がいま見えている)
        """
        from src.assessment.question_frame import FRAME_THRESHOLD

        assert any("detection" in h.id for h in FRAME_THRESHOLD.hypotheses)

    def test_normal_range_is_the_null_hypothesis(self) -> None:
        """既定は「平時の変動内」— 越えたと言うには証拠が要る (fail-closed)。"""
        from src.assessment.question_frame import FRAME_THRESHOLD

        assert any(h.id == "threshold_within_normal" for h in FRAME_THRESHOLD.hypotheses)


class TestFramesAreDistinct:
    def test_trend_and_threshold_are_separate_frames(self) -> None:
        """E は傾き、H は線を越えたか。混ぜると「増えています」で決心に届かない。"""
        assert {"presence", "trend", "threshold"} <= set(FRAME_BY_ID)

    def test_every_frame_has_at_least_three_competing_hypotheses(self) -> None:
        """資格要件 1 — 答えが 2 つ以上の排他的な見立てに割れること。"""
        for frame in FRAME_BY_ID.values():
            assert len(frame.hypotheses) >= 3, frame.frame_id
