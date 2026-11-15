"""深掘りの要点ごとの裏付け関門 (DEEP_DIVE_POINT_GROUNDING)。固定の合成データ。"""

from __future__ import annotations

import pytest
from structlog.testing import capture_logs

from src.digest.point_grounding import PointSource, ground_points, point_violations
from src.digest.recap_points import RecapPoint, RecapPointSection, RecapPointsOutput
from src.tools import n20_flags

SRC = {
    "a1": PointSource("Acme Corp suffers data theft by ZetaGroup\nZetaGroup stole 19 databases."),
    "a2": PointSource("ミラージュ社への侵入\nマサビスイッチの欠陥 CVE-2026-1111 が悪用された。"),
    "a3": PointSource("Beta Ltd outage\nNothing else.", attribution="Example Daily"),
    "a4": PointSource("OmegaGang ransomware hits Gamma Inc\nOmegaGang demanded payment."),
}
CORPUS = [s.text for s in SRC.values()]


def _out(*points: RecapPoint) -> RecapPointsOutput:
    return RecapPointsOutput(sections=[RecapPointSection(heading="h", points=list(points))])


class TestPointViolations:
    def test_name_absent_from_own_sources(self) -> None:
        assert "OmegaGang" in point_violations("OmegaGang が Acme Corp を攻撃した", [SRC["a1"]])

    def test_name_present_passes(self) -> None:
        assert point_violations("ZetaGroup が Acme Corp を攻撃した", [SRC["a1"]]) == ()

    def test_katakana_latin_variant_passes(self) -> None:
        assert point_violations("Masabi Switch の欠陥が悪用された", [SRC["a2"]]) == ()

    def test_cve_absent_is_violation(self) -> None:
        assert point_violations("CVE-2026-9999 が悪用された", [SRC["a2"]]) != ()

    def test_number_with_unit(self) -> None:
        assert point_violations("ZetaGroup は 19 件のDBを盗んだ", [SRC["a1"]]) == ()
        assert "42" in point_violations("ZetaGroup は 42 組織を狙った", [SRC["a1"]])

    def test_small_counts_ignored(self) -> None:
        assert point_violations("ZetaGroup は 3 件を盗んだ", [SRC["a1"]]) == ()

    def test_attribution_only_is_not_violation(self) -> None:
        assert point_violations("Example Daily が Beta Ltd の障害を報じた", [SRC["a3"]]) == ()

    def test_katakana_only_terms_not_checked(self) -> None:
        assert point_violations("ウォレットの権限を確認する", [SRC["a1"]]) == ()

    def test_corpus_name_from_other_case_is_violation(self) -> None:
        # OmegaGang は a4 の事案の名前: a1 の要点へ移すと取り違え
        got = point_violations("OmegaGang が Acme Corp を攻撃した", [SRC["a1"]], corpus=CORPUS)
        assert "OmegaGang" in got

    def test_generic_term_absent_from_corpus_not_checked(self) -> None:
        # CloudTrail はどの記事にも無い一般の技術語 → 移植になりえず落とさない
        got = point_violations("ZetaGroup は CloudTrail を調べた", [SRC["a1"]], corpus=CORPUS)
        assert got == ()

    def test_term_in_many_articles_is_generic(self) -> None:
        many = [f"Article {i} mentions ISC advisory" for i in range(4)]
        got = point_violations("ZetaGroup と ISC", [SRC["a1"]], corpus=[*CORPUS, *many])
        assert got == ()

    def test_known_actor_name_is_checked_even_if_absent_from_corpus(self) -> None:
        from src.cti.actor_normalizer import ActorAlias, ActorAliasRegistry

        reg = ActorAliasRegistry(actors=(ActorAlias(id="x", canonical="Volt Typhoon"),))
        got = point_violations("Volt Typhoon が攻撃した", [SRC["a1"]], corpus=CORPUS, actors=reg)
        assert "Typhoon" in got or "Volt" in got

    def test_identifier_and_number_still_checked_with_corpus(self) -> None:
        assert point_violations("CVE-2026-9999 が悪用された", [SRC["a2"]], corpus=CORPUS) != ()
        assert "42" in point_violations("ZetaGroup は 42 組織", [SRC["a1"]], corpus=CORPUS)

    def test_no_material_fails_open(self) -> None:
        assert point_violations("OmegaGang が攻撃した", [PointSource("  ")]) == ()


class TestGroundPoints:
    def _transplant(self) -> RecapPointsOutput:
        return _out(
            RecapPoint(text="OmegaGang が Acme Corp を攻撃した。", article_ids=["a1"]),
            RecapPoint(text="ZetaGroup が Acme Corp を攻撃した。", article_ids=["a1"]),
        )

    def test_apply_drops_only_ungrounded(self) -> None:
        out = ground_points(self._transplant(), sources=SRC, apply=True)
        assert [p.text for p in out.sections[0].points] == ["ZetaGroup が Acme Corp を攻撃した。"]

    def test_input_not_mutated(self) -> None:
        src = self._transplant()
        ground_points(src, sources=SRC, apply=True)
        assert len(src.sections[0].points) == 2

    def test_other_articles_do_not_ground(self) -> None:
        # 名前は a1 にあるが、この要点の根拠は a3 だけ → 別の記事の名前は裏付けにならない
        out = ground_points(
            _out(RecapPoint(text="ZetaGroup が停止を起こした", article_ids=["a3"])),
            sources=SRC,
            apply=True,
        )
        assert out.sections[0].points == []

    def test_shadow_keeps_output_and_logs_counts_only(self) -> None:
        src = self._transplant()
        with capture_logs() as logs:
            out = ground_points(src, sources=SRC, apply=False)
        assert out == src
        ev = [e for e in logs if e["event"] == "deep_dive_point_ungrounded"]
        assert len(ev) == 1
        assert ev[0]["applied"] is False and ev[0]["violations"] >= 1
        assert "OmegaGang" not in str(ev[0])

    def test_unknown_source_ids_fail_open(self) -> None:
        src = _out(RecapPoint(text="OmegaGang が攻撃した", article_ids=["zz"]))
        assert ground_points(src, sources=SRC, apply=True) == src


class TestFlag:
    def test_default_on_and_values(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DEEP_DIVE_POINT_GROUNDING", raising=False)
        assert n20_flags.deep_dive_point_grounding_mode() == "on"
        for raw, want in (("0", "off"), ("shadow", "shadow"), ("1", "on"), ("x", "off")):
            monkeypatch.setenv("DEEP_DIVE_POINT_GROUNDING", raw)
            assert n20_flags.deep_dive_point_grounding_mode() == want
