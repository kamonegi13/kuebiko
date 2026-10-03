"""重要度の再設計 — 深刻さと関連性を分けて導く (2026-10-03、記録のみの段 M1・M2)。

固定する不変条件:
- 深刻さは日本・SIR・注視国で動かない (事象を平たく評価する — 利用者方針)
- 政策・地政学はサイバーの深刻さの対象外で、戦略上の重みを持つ
- 派生記事 (解説・まとめ) には深刻さを付けない
- 関連性は「誰が・どこで」の SIR だけを数える (「何が起きたか」の SIR は数えない)
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.cti.importance_v2 import (
    RULE_VERSION,
    ImportanceInputs,
    derive,
    derive_severity,
    stated_cvss,
)
from src.storage.run_history import ArticleRecord, RunHistoryRepository, RunRecord
from src.ui.services.importance_v2_job import record_importance_v2

_AXES = {
    "scope": "single_org",
    "impact": "none",
    "confirmation": "not_applicable",
    "magnitude": "unknown",
    "exploitation": "not_applicable",
    "actor": "unknown",
    "target": "general_org",
}


def _inp(**kw: object) -> ImportanceInputs:
    base = ImportanceInputs(
        category="breach",
        article_type="breaking",
        axes=_AXES,
        on_kev=False,
        max_cvss=0.0,
        victim_country="",
        involved_countries=frozenset(),
        mentioned_countries=frozenset(),
        sir_ids=frozenset(),
    )
    return replace(base, **kw)  # type: ignore[arg-type]  # テスト用の部分上書き


def _axes(**kw: str) -> dict[str, str]:
    return {**_AXES, **kw}


class TestSeverity:
    def test_japan_and_sir_do_not_change_severity(self) -> None:
        # Arrange: 同じ事象を、日本・注視国・SIR の有無だけ変えて並べる
        plain = _inp(axes=_axes(impact="data_exposure", confirmation="confirmed"))
        jp = replace(
            plain,
            victim_country="JP",
            involved_countries=frozenset({"JP", "CN"}),
            sir_ids=frozenset({"pir_jp_targeted", "pir_china_apt"}),
        )

        # Act / Assert
        assert derive(plain).severity == derive(jp).severity == "S2"
        assert derive(jp).relevant and not derive(plain).relevant

    @pytest.mark.parametrize("category", ["geopolitical", "policy"])
    def test_non_cyber_has_no_severity_but_strategic_weight(self, category: str) -> None:
        rec = derive(
            _inp(
                category=category,
                axes=_axes(impact="destructive", confirmation="confirmed", scope="national"),
                involved_countries=frozenset({"RU"}),
            )
        )

        assert rec.severity is None
        assert rec.strategic_weight == "heavy"

    def test_strategic_weight_follows_watched_nations_and_article_type(self) -> None:
        geo = _inp(category="geopolitical")

        assert derive(geo).strategic_weight == "light"
        watched = replace(geo, involved_countries=frozenset({"KP"}))
        assert derive(watched).strategic_weight == "heavy"
        assert derive(replace(watched, article_type="opinion")).strategic_weight == "moderate"

    def test_cyber_articles_have_no_strategic_weight(self) -> None:
        assert derive(_inp(involved_countries=frozenset({"CN"}))).strategic_weight is None

    @pytest.mark.parametrize("article_type", ["opinion", "recap", "tutorial"])
    def test_derivative_articles_have_no_severity(self, article_type: str) -> None:
        sev, basis = derive_severity(
            _inp(
                article_type=article_type,
                axes=_axes(impact="data_exposure", confirmation="confirmed", scope="national"),
            )
        )

        assert (sev, basis) == (None, "derivative")

    def test_kev_is_s3_for_any_cyber_category(self) -> None:
        assert derive_severity(_inp(category="incident", on_kev=True)) == ("S3", "kev")

    @pytest.mark.parametrize(
        ("cvss", "exploitation", "expected"),
        [
            (5.0, "exploited_in_wild", "S3"),
            (9.8, "disclosed_only", "S2"),
            (7.5, "poc", "S2"),
            (7.5, "disclosed_only", "S1"),
            (0.0, "not_applicable", "S1"),
        ],
    )
    def test_vulnerability_uses_exploitation_and_cvss(
        self, cvss: float, exploitation: str, expected: str
    ) -> None:
        inp = _inp(category="vulnerability", max_cvss=cvss, axes=_axes(exploitation=exploitation))

        assert derive_severity(inp)[0] == expected

    def test_critical_cvss_alone_is_not_s3(self) -> None:
        # 試算で、悪用のない CVSS 9.8 を S3 にすると medium の 120 件が S3 に上がった
        inp = _inp(category="vulnerability", max_cvss=9.8)

        assert derive_severity(inp)[0] == "S2"

    def test_malware_analysis_is_s2_and_state_wide_campaign_s3(self) -> None:
        # 正解集: 新しいマルウェアの解析は「新しい手口を含む脅威の分析」で S2
        single = _inp(category="malware", article_type="research")

        assert derive_severity(single) == ("S2", "malware")
        state = replace(single, axes=_axes(scope="sector_wide", actor="state"))
        assert derive_severity(state)[0] == "S3"

    def test_leak_site_claim_alone_is_s1(self) -> None:
        # 正解集: ランサムウェアの暴露サイトへの掲載のみ (業務停止の記述なし) は S1
        claim = _inp(axes=_axes(impact="data_exposure", confirmation="claimed_only"))

        assert derive_severity(claim)[0] == "S1"
        disruption = replace(claim, axes=_axes(impact="disruption", confirmation="claimed_only"))
        assert derive_severity(disruption) == ("S2", "claimed_disruption")

    def test_confirmed_breach_with_possible_leak_is_s2(self) -> None:
        inp = _inp(axes=_axes(impact="data_exposure", confirmation="possible"))

        assert derive_severity(inp) == ("S2", "harm")

    def test_ransomware_listing_read_as_destructive_stays_s1(self) -> None:
        # 掲載だけの記事は「暗号化 (destructive)」と読まれやすいが、停止の報道が無ければ S1
        inp = _inp(axes=_axes(impact="destructive", confirmation="claimed_only"))

        assert derive_severity(inp)[0] == "S1"

    def test_malware_category_leak_listing_is_not_treated_as_analysis(self) -> None:
        inp = _inp(
            category="malware", axes=_axes(impact="data_exposure", confirmation="claimed_only")
        )

        assert derive_severity(inp)[0] == "S1"

    def test_multi_org_unauthorized_access_only_is_s2(self) -> None:
        inp = _inp(
            axes=_axes(
                scope="multi_org_or_provider",
                impact="unauthorized_access",
                confirmation="confirmed",
            )
        )

        assert derive_severity(inp) == ("S2", "harm")

    def test_large_magnitude_harm_is_s3_even_if_unconfirmed(self) -> None:
        inp = _inp(axes=_axes(impact="data_exposure", confirmation="possible", magnitude="ge_1m"))

        assert derive_severity(inp) == ("S3", "large_magnitude")

    def test_state_actor_on_critical_target_is_s3(self) -> None:
        inp = _inp(category="apt", axes=_axes(actor="state", target="defense"))

        assert derive_severity(inp) == ("S3", "state_on_critical")

    def test_no_harm_incident_is_s1(self) -> None:
        assert derive_severity(_inp(category="incident")) == ("S1", "incident")


class TestStatedCvss:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("CVSS v3.1 のスコアは 9.8 です", 9.8),
            ("CVSSv4.0: 9.3 と 7.5 の 2 件", 9.3),
            ("CVSS 4.0 で critical に分類", 9.0),
            ("深刻度は「緊急」、CVSS は未公表", 9.0),
            ("バージョン 10.2 で修正", 0.0),
            ("CVSS 99.9 は誤記", 0.0),
        ],
    )
    def test_reads_cvss_written_in_text(self, text: str, expected: float) -> None:
        assert stated_cvss(text) == expected


class TestRelevance:
    @pytest.mark.parametrize(
        ("kw", "expected"),
        [
            ({"sir_ids": frozenset({"pir_jp_targeted"})}, "targeted"),
            ({"victim_country": "JP"}, "affected"),
            ({"sir_ids": frozenset({"pir_jp_company_breach"})}, "affected"),
            ({"mentioned_countries": frozenset({"JP"})}, "mentioned"),
            ({}, "none"),
        ],
    )
    def test_japan_relation(self, kw: dict[str, object], expected: str) -> None:
        assert derive(_inp(**kw)).jp == expected

    def test_japan_mention_alone_is_not_relevant(self) -> None:
        assert not derive(_inp(mentioned_countries=frozenset({"JP"}))).relevant

    def test_what_happened_sirs_do_not_count_as_relevance(self) -> None:
        # 「新しい PoC の脆弱性」「重要インフラ」は事象の種類 = 深刻さの側の話
        rec = derive(_inp(sir_ids=frozenset({"pir_new_poc_vuln", "pir_critical_infra"})))

        assert not rec.relevant
        assert rec.sir_ids == ("pir_critical_infra", "pir_new_poc_vuln")  # 生の該当は残す

    def test_who_where_sir_counts_as_relevance(self) -> None:
        assert derive(_inp(sir_ids=frozenset({"pir_dprk_apt"}))).relevant

    def test_watched_nation_involvement_is_relevant(self) -> None:
        rec = derive(_inp(involved_countries=frozenset({"IR", "US"})))

        assert rec.relevant
        assert rec.nations == ("IR",)


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    r = RunHistoryRepository(db_path=tmp_path / "v2.db")
    rid = r.start_run(RunRecord(started_at=datetime.now(UTC), pipeline="x", dry_run=False))
    for aid, cat in (("a1", "vulnerability"), ("a2", "geopolitical"), ("no_axes", "breach")):
        r.add_article(
            ArticleRecord(
                run_id=rid,
                article_id=aid,
                title="t",
                url=f"https://kuebiko.example/{aid}",
                status="posted",
                importance="high",
                category=cat,
                article_type="breaking",
                victim_country_iso="JP" if aid == "a1" else None,
                created_at=datetime.now(UTC),
            )
        )
    r.set_severity_axes("a1", _axes(exploitation="exploited_in_wild"), "m")
    r.set_severity_axes("a2", _AXES, "m")
    r.add_article_entities("a2", [("involved_country", "cn"), ("pir", "pir_geo_china")])
    return r


class TestRecording:
    def test_records_articles_with_axes_and_reads_entities(
        self, repo: RunHistoryRepository
    ) -> None:
        # Act
        summary = record_importance_v2(repo)

        # Assert
        assert summary == {"recorded": 2, "rule_version": RULE_VERSION}
        cells = repo.importance_v2_crosstab(since="2000-01-01")
        by = {(c["severity"], c["strategic_weight"], c["relevant"]): c["count"] for c in cells}
        assert by == {("S3", None, True): 1, (None, "heavy", True): 1}

    def test_second_run_records_nothing_new(self, repo: RunHistoryRepository) -> None:
        record_importance_v2(repo)

        assert record_importance_v2(repo)["recorded"] == 0

    def test_old_rule_version_is_recorded_again(
        self, repo: RunHistoryRepository, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        record_importance_v2(repo)
        monkeypatch.setattr("src.ui.services.importance_v2_job.RULE_VERSION", "next")

        assert record_importance_v2(repo)["recorded"] == 2

    def test_flag_off_skips(
        self, repo: RunHistoryRepository, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("IMPORTANCE_V2_RECORD", "0")

        assert record_importance_v2(repo) == {"skipped": "flag_off"}
