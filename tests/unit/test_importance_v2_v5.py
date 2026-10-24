"""深刻さ v5 の決定 (利用者判断 2026-10-24、P1〜P7) を A (importance_v2) に反映した規則。

正解集 data/eval/severity_gold_v5_final.jsonl の決め:
P1 単一の民間事業者の停止・侵害は S2 / P2 委託先経由は S2、影響を受けた組織の数が書いてあれば S3 /
P3 国家系の解析 (標的が確認なら S3・推定なら S2: 軸だけでは区別できず A は変えない) /
P4 悪用の観測なしは S2 / P5 続報は新しい被害の事実があるときだけ、復旧・再開のみは深刻さなし /
P6 誤送付・設定不備の漏えいはサイバーの事象 / P7 重要インフラは NISC の 15 分野。
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from src.cti.importance_v2 import (
    RULE_VERSION,
    ImportanceInputs,
    build_inputs,
    derive_severity,
    is_recovery_only,
    stated_org_count,
)

_AXES = {
    "scope": "single_org",
    "impact": "none",
    "confirmation": "not_applicable",
    "magnitude": "unknown",
    "exploitation": "not_applicable",
    "actor": "unknown",
    "target": "general_org",
}


def _axes(**kw: str) -> dict[str, str]:
    return {**_AXES, **kw}


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
    return replace(base, **kw)  # type: ignore[arg-type]


def test_rule_version_bumped_for_v5() -> None:
    assert RULE_VERSION == "2026-10-24.12"


class TestP1SingleOperator:
    def test_large_single_org_outage_is_s2(self) -> None:
        inp = _inp(axes=_axes(impact="disruption", confirmation="confirmed", victim_size="large"))
        assert derive_severity(inp) == ("S2", "harm")


class TestP2Provider:
    _provider = _axes(scope="multi_org_or_provider", impact="disruption", confirmation="confirmed")

    def test_provider_breach_without_org_count_is_s2(self) -> None:
        assert derive_severity(_inp(axes=self._provider)) == ("S2", "provider_unquantified")

    def test_provider_breach_with_org_count_is_s3(self) -> None:
        inp = _inp(axes=self._provider, affected_org_count=300)
        assert derive_severity(inp) == ("S3", "provider_org_count")

    def test_possible_provider_breach_with_org_count_is_s3(self) -> None:
        axes = _axes(scope="multi_org_or_provider", impact="disruption", confirmation="possible")
        assert derive_severity(_inp(axes=axes, affected_org_count=787)) == (
            "S3",
            "provider_org_count",
        )

    def test_sector_wide_stays_s3_without_count(self) -> None:
        axes = _axes(scope="sector_wide", impact="data_exposure", confirmation="confirmed")
        assert derive_severity(_inp(axes=axes)) == ("S3", "confirmed_wide_harm")

    def test_claimed_only_provider_with_count_is_not_s3(self) -> None:
        axes = _axes(
            scope="multi_org_or_provider", impact="disruption", confirmation="claimed_only"
        )
        sev, _ = derive_severity(_inp(axes=axes, affected_org_count=50))
        assert sev != "S3"

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("少なくとも 300 組織が影響を受けた", 300),
            ("医療機関最大 787 件に不正アクセスの影響が及ぶおそれ", 787),
            ("at least 300 organizations were affected by the breach", 300),
            ("顧客 1 万社超が利用する人材紹介プラットフォームを停止", 0),
            ("同社は 3,000 社以上のクライアントを持つ", 0),
            ("影響を受けた個人は 12 万人", 0),
            ("", 0),
        ],
    )
    def test_stated_org_count(self, text: str, expected: int) -> None:
        assert stated_org_count(text) == expected


class TestP3P4Unchanged:
    def test_state_actor_on_critical_stays_s3(self) -> None:
        axes = _axes(actor="state", target="government", confirmation="possible")
        assert derive_severity(_inp(axes=axes)) == ("S3", "state_on_critical")

    def test_poc_without_exploitation_is_s2(self) -> None:
        inp = _inp(category="vulnerability", axes=_axes(exploitation="poc"))
        assert derive_severity(inp) == ("S2", "poc")

    def test_observed_exploitation_is_s3(self) -> None:
        inp = _inp(category="vulnerability", axes=_axes(exploitation="exploited_in_wild"))
        assert derive_severity(inp) == ("S3", "exploited")


class TestP5Followup:
    @pytest.mark.parametrize(
        "title",
        [
            "ニッスイ子会社・日水物流、全17拠点で通常の入出荷業務を再開",
            "ヤマト運輸「クロネコ代金後払い」10月9日13時に再開 支払期限を17日延長",
            "Services restored after ransomware attack",
        ],
    )
    def test_recovery_only_title_has_no_severity(self, title: str) -> None:
        axes = _axes(impact="disruption", confirmation="confirmed", scope="sector_wide")
        assert derive_severity(_inp(axes=axes, title=title)) == (None, "recovery_only")

    @pytest.mark.parametrize(
        "title",
        [
            "システム再開も新たに顧客情報 5 万件の流出が判明",
            "不正アクセスで 126 万件の個人情報が漏えい",
            "サービス復旧後に被害が拡大していたことが判明",
            "「支払い・解約できない」My SoftBankが不正アクセスで一時停止 復旧長期化か?",
            "一部顧客のデータは復旧困難 IDCフロンティアにランサムウェア、495社・自治体に影響",
            "ランサム復旧「3日」「50日」「73日」の分岐点 サイバー攻撃に学ぶ",
            "第3報で侵入経路・攻撃手法を特定―システムログも閲覧、再開日時は未定",
            "ShinyHunters がバックアップ用として /shinypog を再開、メインサイトは未だアクセス不能",
        ],
    )
    def test_new_damage_title_is_not_recovery_only(self, title: str) -> None:
        assert not is_recovery_only(title)

    def test_empty_title_is_not_recovery_only(self) -> None:
        assert not is_recovery_only("")


class TestP6Accident:
    def test_misdelivery_with_large_count_is_cyber_s3(self) -> None:
        axes = _axes(impact="data_exposure", confirmation="confirmed", actor="insider_or_accident")
        inp = _inp(axes={**axes, "magnitude": "100k_1m"})
        assert derive_severity(inp) == ("S3", "large_magnitude")


class TestP7Nisc:
    def test_critical_infra_target_of_university_is_not_critical(self) -> None:
        axes = _axes(impact="disruption", confirmation="confirmed", target="critical_infra")
        inp = _inp(axes=axes, victim_sector="education")
        assert derive_severity(inp) == ("S2", "harm")

    def test_critical_infra_target_without_sector_stays_critical(self) -> None:
        axes = _axes(impact="disruption", confirmation="confirmed", target="critical_infra")
        assert derive_severity(_inp(axes=axes)) == ("S3", "critical_disruption")

    def test_sector_alone_does_not_raise_general_org_to_critical(self) -> None:
        axes = _axes(impact="disruption", confirmation="confirmed", target="general_org")
        inp = _inp(axes=axes, victim_sector="financial")
        assert derive_severity(inp) == ("S2", "harm")

    def test_non_nisc_outage_is_s2(self) -> None:
        axes = _axes(impact="disruption", confirmation="confirmed", target="general_org")
        assert derive_severity(_inp(axes=axes)) == ("S2", "harm")


class TestBuildInputsV5:
    def test_build_inputs_fills_org_count_and_sector(self) -> None:
        inp = build_inputs(
            category="incident",
            article_type="breaking",
            axes=_AXES,
            cves=[],
            kev=frozenset(),
            year=2026,
            victim_country="JP",
            involved_countries=[],
            mentioned_countries=[],
            sir_ids=[],
            text="証券会社のシステムが停止。少なくとも 40 組織が影響を受けた。",
            title="証券会社でシステム停止",
            victim_sector="financial",
        )
        assert inp.affected_org_count == 40
        assert inp.victim_sector == "financial"
