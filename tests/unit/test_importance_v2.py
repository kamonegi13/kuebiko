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
    corrected_axes,
    derive,
    derive_severity,
    importance_level,
    stated_cvss,
    stated_loss_usd,
    stated_victim_count,
    subject_kev,
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
            (0.0, "poc", "S2"),
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


class TestExtraAxesRules:
    """s23 (``.9``、2026-10-08): 本文由来の 5 欄を消費する 2 規則。

    欄が NULL (s21 以前・要約入力) なら ``ax.get(...)`` は ``None`` を返し発火しない
    (``.8`` と同じ結果になる — ``_AXES`` は 5 欄を一切含まない)。
    """

    def test_distribution_compromise_true_is_s3_even_for_malware_category(self) -> None:
        # .8 なら「新しい手口を含む脅威の分析」で S2 (test_malware_analysis_is_s2_and_...)
        inp = _inp(
            category="malware",
            article_type="research",
            axes=_axes(distribution_compromise="true"),
        )

        assert derive_severity(inp) == ("S3", "distribution_compromise")

    def test_distribution_compromise_false_does_not_change_the_8_result(self) -> None:
        inp = _inp(category="malware", article_type="research", axes=_axes())
        with_false = replace(inp, axes=_axes(distribution_compromise="false"))

        assert derive_severity(inp) == derive_severity(with_false) == ("S2", "malware")

    def test_distribution_compromise_does_not_resurrect_non_cyber_or_derivative(self) -> None:
        # (a) は「サイバーの深刻さが付く記事」だけを引き上げる。政策・派生記事は対象外のまま
        geo = _inp(category="geopolitical", axes=_axes(distribution_compromise="true"))
        assert derive_severity(geo) == (None, "non_cyber")

        derivative = _inp(article_type="opinion", axes=_axes(distribution_compromise="true"))
        assert derive_severity(derivative) == (None, "derivative")

    def test_distribution_compromise_does_not_fire_without_axes(self) -> None:
        # 軸自体が無い記事 (no_axes) は s23 の欄も無いので .8 と同じ no_axes のまま
        assert derive_severity(_inp(axes={})) == (None, "no_axes")

    def test_large_org_confirmed_disruption_is_s3(self) -> None:
        # .8 なら single_org の確認済み disruption は S2 ("harm")
        inp = _inp(axes=_axes(impact="disruption", confirmation="confirmed", victim_size="large"))

        assert derive_severity(inp) == ("S3", "large_org_disruption")

    @pytest.mark.parametrize("victim_size", ["medium", "small", "unknown", None])
    def test_large_org_rule_requires_large_victim_size(self, victim_size: str | None) -> None:
        extra = {} if victim_size is None else {"victim_size": victim_size}
        inp = _inp(axes=_axes(impact="disruption", confirmation="confirmed", **extra))

        assert derive_severity(inp) == ("S2", "harm")

    def test_large_org_rule_requires_confirmation(self) -> None:
        inp = _inp(axes=_axes(impact="disruption", confirmation="possible", victim_size="large"))

        assert derive_severity(inp)[0] != "S3"


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


class TestStatedLoss:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("attackers stole $388 million in crypto", 388e6),
            ("US$1.5 billion was drained", 1.5e9),
            ("被害額は 3.88 億ドル", 3.88e8),
            ("損害は 150 億円", 1e8),
            ("no money mentioned", 0.0),
            # 売上・販売価格・身代金の要求額は被害額ではない
            ("売上50億ドルの医療機関への初期アクセス権が販売されている", 0.0),
            ("Access to the firm was listed on a forum for $1,500.", 0.0),
            ("The gang demanded a $50 million ransom.", 0.0),
        ],
    )
    def test_reads_largest_amount(self, text: str, expected: float) -> None:
        assert stated_loss_usd(text) == pytest.approx(expected)

    def test_large_theft_is_s3_without_critical_infra(self) -> None:
        # 暗号資産取引所は重要インフラに含めず、金額の規模で拾う
        inp = _inp(axes=_axes(impact="unauthorized_access", confirmation="confirmed"), loss_usd=4e8)

        assert derive_severity(inp) == ("S3", "large_loss")

    def test_loss_without_harm_does_not_raise(self) -> None:
        assert derive_severity(_inp(loss_usd=4e8))[0] == "S1"


class TestStatedVictimCount:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("約31万2000件の個人情報が流出した可能性がある", 312_000),
            ("約 1850 万件のユーザー記録が流出したことを確認した", 18_500_000),
            ("利用者1万2345人の個人情報を含むファイルが公開された", 12_345),
            ("250名の当選結果が不正アクセスにより改ざんされた", 250),
            ("The breach exposed 3.2 million customer records.", 3_200_000),
            ("Data of 150,000 patients was stolen in the attack.", 150_000),
            ("続報では 20 万件超の報告書が複製されたことが明らかになった", 200_000),
        ],
    )
    def test_reads_victim_count_in_harm_sentence(self, text: str, expected: int) -> None:
        assert stated_victim_count(text) == expected

    @pytest.mark.parametrize(
        "text",
        [
            # 被害者の数でない数字 (利用規模・送金・送信・ファイル・試行) は規模に数えない
            "週間約20万ダウンロードの正規パッケージが侵害された",
            "攻撃者のウォレットから 2,655 件の送金が確認された",
            "侵害されたアカウントから約1,500件のフィッシングメールを送信した",
            "約100GB・12万3,456ファイルが窃取されたと主張",
            "同社のサービスには 500 万人の利用者がいる",
            "メールサーバーが不正利用され、約60万件の不審メール送信に悪用された",
            # 攻撃者の主張する件数では重くしない (基準の文 §4)
            "ShinyHunters が 3 億件の患者データを窃取したと主張している",
            "4,000 万件のアカウントが流出し、フォーラムで販売されている",
            # 要約の打ち消しの注記
            "今回の事案を「顧客情報60万件が流出した」と表現することはできません",
            "「45万人分の個人情報が漏えいした」",
            "約60万件という送信数から大規模な侵害を連想する可能性がある",
            "10万件の個人情報の流出は確認されていません",
            "no numbers here",
        ],
    )
    def test_ignores_counts_that_are_not_victims(self, text: str) -> None:
        assert stated_victim_count(text) == 0


class TestImportanceLevel:
    @pytest.mark.parametrize(
        ("severity", "relevant", "expected"),
        [
            ("S3", True, 1),
            ("S3", False, 2),
            ("S2", True, 3),
            ("S2", False, 4),
            ("S1", True, 5),
            ("S1", False, 6),
        ],
    )
    def test_severity_comes_before_relevance(
        self, severity: str, relevant: bool, expected: int
    ) -> None:
        assert importance_level(severity, relevant) == expected  # type: ignore[arg-type]  # Literal の列挙

    def test_no_severity_has_no_level(self) -> None:
        assert importance_level(None, True) is None

    def test_record_exposes_level(self) -> None:
        rec = derive(
            _inp(axes=_axes(impact="data_exposure", confirmation="confirmed"), victim_country="JP")
        )

        assert (rec.severity, rec.relevant, rec.level) == ("S2", True, 3)


class TestNoAxes:
    @pytest.mark.parametrize("category", ["breach", "incident", "malware", "research"])
    def test_articles_without_axes_get_no_severity(self, category: str) -> None:
        assert derive_severity(_inp(category=category, axes={})) == (None, "no_axes")

    def test_vulnerability_without_axes_still_uses_cvss(self) -> None:
        assert derive_severity(_inp(category="vulnerability", axes={}, max_cvss=9.8))[0] == "S2"

    def test_japan_relation_is_recorded_without_axes(self) -> None:
        rec = derive(_inp(axes={}, victim_country="JP"))

        assert (rec.severity, rec.jp, rec.level) == (None, "affected", None)


class TestCorrectedAxes:
    def test_body_count_raises_magnitude_to_s3(self) -> None:
        # 要約から付けた軸が規模を落とした例 (本文には 31 万件と書かれている)
        ax = corrected_axes(_axes(impact="data_exposure", confirmation="possible"), 312_000)

        assert ax["magnitude"] == "100k_1m"
        assert derive_severity(_inp(axes=ax)) == ("S3", "large_magnitude")

    def test_body_count_lowers_overestimated_magnitude(self) -> None:
        ax = corrected_axes(
            _axes(impact="data_exposure", confirmation="confirmed", magnitude="100k_1m"), 133
        )

        assert ax["magnitude"] == "lt_1k"
        assert derive_severity(_inp(axes=ax))[0] == "S2"

    def test_large_magnitude_without_body_count_is_unknown(self) -> None:
        ax = corrected_axes(_axes(impact="data_exposure", magnitude="ge_1m"), 0)

        assert ax["magnitude"] == "unknown"

    def test_small_magnitude_without_body_count_is_kept(self) -> None:
        assert corrected_axes(_axes(magnitude="1k_100k"), 0)["magnitude"] == "1k_100k"

    def test_claimed_only_never_gets_large_magnitude(self) -> None:
        ax = corrected_axes(_axes(impact="data_exposure", confirmation="claimed_only"), 300_000)

        assert ax["magnitude"] == "unknown"

    def test_missing_axes_stay_missing(self) -> None:
        assert corrected_axes({}, 500_000) == {}


class TestSubjectKev:
    def test_old_cve_on_kev_mentioned_in_passing_does_not_count(self) -> None:
        kev = frozenset({"CVE-2024-38475", "CVE-2026-1111"})

        assert not subject_kev(["CVE-2024-38475", "CVE-2026-2222"], kev, 2026)
        assert subject_kev(["CVE-2026-1111"], kev, 2026)

    def test_previous_year_cve_still_counts(self) -> None:
        assert subject_kev(["cve-2025-9999"], frozenset({"CVE-2025-9999"}), 2026)


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

        # Assert: 軸が無い "no_axes" も記録される (§1 — 投稿済みは全件記録対象)
        assert summary == {"recorded": 3, "rule_version": RULE_VERSION}
        cells = repo.importance_v2_crosstab(since="2000-01-01")
        by = {(c["severity"], c["strategic_weight"], c["relevant"]): c["count"] for c in cells}
        assert by == {
            ("S3", None, True): 1,
            (None, "heavy", True): 1,
            (None, None, False): 1,  # 軸なしの事案は深刻さを付けない (版 .8、no_axes)
        }

    def test_article_without_axes_still_gets_jp_and_relevance(
        self, repo: RunHistoryRepository
    ) -> None:
        """軸が無い記事も記録の対象になる (深刻さは付けず ``no_axes``)。
        日本との関係・関連性は軸に関係なく計算される。"""
        record_importance_v2(repo)

        rows = repo.pending_importance_inputs(
            since="2000-01-01", rule_version="unused-check", limit=10
        )
        assert "no_axes" in rows
        assert rows["no_axes"].axes == {}

    def test_victim_count_in_summary_reaches_the_record(self, repo: RunHistoryRepository) -> None:
        # Arrange: 軸は規模を落としているが、要約に 31 万件の流出が書かれている
        rid = repo.start_run(RunRecord(started_at=datetime.now(UTC), pipeline="x", dry_run=False))
        repo.add_article(
            ArticleRecord(
                run_id=rid,
                article_id="leak",
                title="t",
                url="https://kuebiko.example/leak",
                status="posted",
                importance="high",
                category="breach",
                article_type="breaking",
                summary="約31万2000件の個人情報が流出した可能性がある。",
                created_at=datetime.now(UTC),
            )
        )
        repo.set_severity_axes("leak", _axes(impact="data_exposure", confirmation="possible"), "m")

        # Act
        record_importance_v2(repo)

        # Assert
        by = {
            (c["severity"], c["relevant"]): c["count"]
            for c in repo.importance_v2_crosstab(since="2000-01-01")
        }
        assert by[("S3", False)] == 1

    def test_second_run_records_nothing_new(self, repo: RunHistoryRepository) -> None:
        record_importance_v2(repo)

        assert record_importance_v2(repo)["recorded"] == 0

    def test_old_rule_version_is_recorded_again(
        self, repo: RunHistoryRepository, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        record_importance_v2(repo)
        monkeypatch.setattr("src.ui.services.importance_v2_job.RULE_VERSION", "next")

        assert record_importance_v2(repo)["recorded"] == 3

    def test_flag_off_skips(
        self, repo: RunHistoryRepository, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("IMPORTANCE_V2_RECORD", "0")

        assert record_importance_v2(repo) == {"skipped": "flag_off"}
