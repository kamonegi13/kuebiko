"""アクター・ミッション脅威評価 (actor_threat) のテスト。

docs/actor_mission_threat_design.md §4.7 の受け入れ基準をコードとして固定する。
核心の不変条件:
- テンポはティアに影響しない (休眠の R3 が Critical、 高テンポ犯罪が Critical に届かない)
- 能力 unknown ≠ low、 能力の証拠不足でティアは下がらない
- 対日関連度の証拠は doctrine (公的名指し) と観測 (victim 列) のみ — 下駄なし
- 評価対象は kind=group のみ
"""

from __future__ import annotations

from src.cti.actor_normalizer import ActorAlias
from src.ui.services.actor_threat import (
    JP_SUSTAINED_MIN,
    ActorThreatAssessment,
    assess_actor,
)
from src.ui.services.threat_operations import ActorActivity

_NO_KEV: frozenset[str] = frozenset()


def _alias(**kw: object) -> ActorAlias:
    base: dict[str, object] = {"id": "x", "canonical": "X", "kind": "group"}
    base.update(kw)
    return ActorAlias.model_validate(base)


def _activity(**kw: object) -> ActorActivity:
    base: dict[str, object] = {
        "actor_id": "x",
        "canonical": "X",
        "aliases": (),
        "nation": "",
        "family": "",
        "mitre_group": "",
        "sponsor": "",
        "description": "",
        "total_articles": 0,
        "daily_counts": (0,) * 12,
        "sparkline": "",
        "last_seen_iso": "",
        "top_sectors": (),
        "top_countries": (),
        "top_cves": (),
        "top_ttps": (),
    }
    base.update(kw)
    return ActorActivity(**base)  # type: ignore[arg-type]


def _ttps(n: int) -> tuple[str, ...]:
    return tuple(f"T1{i:03d} Technique {i}" for i in range(n))


class TestAcceptanceVoltTyphoon:
    """罠1の本丸: 観測 JP 被害 0 の休眠事前配置アクターが最上位に立つこと。"""

    def test_critical_dormant_with_zero_observation(self) -> None:
        alias = _alias(
            id="volt_typhoon",
            canonical="Volt Typhoon",
            aliases=("Vanguard Panda", "BRONZE SILHOUETTE"),
            nation="cn",
            mitre_ttps=_ttps(20),
        )
        got = assess_actor(alias, None, _NO_KEV)
        assert got is not None
        assert got.tier == "critical"
        assert got.relevance_band == 3
        assert got.activity_state == "dormant"
        # 駆動要因に doctrine 接地と「暗域≠安全」の注記が出る (透明性)
        assert any("事前配置 doctrine" in f for f in got.relevance_factors)
        assert any("暗域=不明≠安全" in f for f in got.activity_factors)

    def test_dormancy_does_not_lower_tier(self) -> None:
        """休眠 (観測0) と活動中で ティアが同じ = テンポはティア外の検証。"""
        alias = _alias(
            id="volt_typhoon",
            canonical="Volt Typhoon",
            nation="cn",
            mitre_ttps=_ttps(20),
        )
        dormant = assess_actor(alias, None, _NO_KEV)
        active = assess_actor(
            alias, _activity(total_articles=30, is_spike=True, spike_ratio=4.0), _NO_KEV
        )
        assert dormant is not None and active is not None
        assert dormant.tier == active.tier == "critical"
        assert dormant.activity_state == "dormant"
        assert active.activity_state == "spiking"


class TestAcceptanceQilin:
    """罠2: 高テンポ JP ランサムは High 止まり (テンポが戦略脅威を埋めない)。"""

    def test_high_spiking_not_critical(self) -> None:
        alias = _alias(
            id="qilin",
            canonical="Qilin",
            associated_malware=("Qilin", "Agenda"),
        )
        activity = _activity(
            total_articles=49,
            is_spike=True,
            spike_ratio=3.0,
            top_cves=("CVE-2024-21762",),
            victim_country_sectors=(
                ("JP", "healthcare", 20),
                ("JP", "manufacturing", 29),
                ("US", "enterprise", 30),
            ),
        )
        got = assess_actor(alias, activity, frozenset({"CVE-2024-21762"}))
        assert got is not None
        assert got.tier == "high"  # R2 (JP 持続標的) × medium — Critical に届かない
        assert got.relevance_band == 2
        assert got.activity_state == "spiking"
        assert any("日本の被害 49 件" in f for f in got.relevance_factors)

    def test_jp_defgov_victim_elevates_to_critical(self) -> None:
        """同じランサムでも日本の防衛/政府に実被害が観測されれば R3 → Critical。
        (観測事実がティアを決める — 下駄でなくデータ駆動の検証)"""
        alias = _alias(id="qilin", canonical="Qilin", associated_malware=("Qilin", "Agenda"))
        activity = _activity(
            total_articles=10,
            top_cves=("CVE-2024-21762",),
            victim_country_sectors=(("JP", "defense", 1), ("JP", "manufacturing", 9)),
        )
        got = assess_actor(alias, activity, frozenset({"CVE-2024-21762"}))
        assert got is not None
        assert got.tier == "critical"
        assert got.relevance_band == 3


class TestAcceptanceDoctrineActors:
    def test_mirrorface_critical_via_jp_doctrine(self) -> None:
        alias = _alias(
            id="mirrorface",
            canonical="MirrorFace",
            aliases=("Earth Kasha",),
            nation="cn",
            mitre_ttps=_ttps(16),
            associated_malware=("LODEINFO", "NOOPDOOR", "ANEL"),
        )
        got = assess_actor(alias, None, _NO_KEV)
        assert got is not None
        assert got.tier == "critical"
        assert any("警察庁" in f for f in got.relevance_factors)

    def test_sandworm_critical_via_ics_and_disruption(self) -> None:
        """対日 doctrine 無しでも ICS 破壊能力 × disruption で Critical に届く
        (正しい理由で正しい答え — 対日バイアスなしの検証)。"""
        alias = _alias(
            id="sandworm",
            canonical="Sandworm",
            nation="ru",
            mitre_ttps=(*_ttps(20), "T0813 Denial of Control"),
            associated_malware=("BlackEnergy", "Industroyer", "NotPetya"),
        )
        activity = _activity(
            total_articles=8,
            top_intents=(("disruption", 5), ("espionage", 2)),
            victim_country_sectors=(("UA", "electricity", 5),),
        )
        got = assess_actor(alias, activity, _NO_KEV)
        assert got is not None
        assert got.tier == "critical"
        assert got.capability_band == "high"  # ICS 単独 High
        assert any("ICS/OT" in f for f in got.capability_factors)

    def test_turla_espionage_stays_high(self) -> None:
        """諜報専業の R2 × high は Critical に昇格しない (Critical の予約)。"""
        alias = _alias(
            id="turla",
            canonical="Turla",
            nation="ru",
            mitre_ttps=_ttps(30),
            associated_malware=("Snake", "Kazuar", "Carbon", "Crutch"),
        )
        activity = _activity(
            total_articles=6,
            top_cves=("CVE-2023-38831",),
            top_intents=(("espionage", 6),),
        )
        got = assess_actor(alias, activity, frozenset({"CVE-2023-38831"}))
        assert got is not None
        assert got.tier == "high"
        assert got.relevance_band == 2  # 標的分野 doctrine × 敵性国家


class TestUnknownIsNotLow:
    def test_new_actor_without_evidence_is_unrated(self) -> None:
        """辞書データ皆無・観測なしの新興アクター → 未評価 (Watch/低ではない)。"""
        alias = _alias(id="unc9999", canonical="UNC9999")
        got = assess_actor(alias, None, _NO_KEV)
        assert got is not None
        assert got.tier is None
        assert got.capability_band == "unknown"
        assert got.coverage_note is not None
        assert "低" in got.coverage_note  # unknown ≠ low の明示

    def test_capability_unknown_does_not_lower_r3(self) -> None:
        """R3 だが能力データ無し → High が下限 (能力不足でティアは沈まない)。"""
        alias = _alias(id="mirrorface", canonical="MirrorFace", nation="cn")
        got = assess_actor(alias, None, _NO_KEV)
        assert got is not None
        assert got.tier == "high"
        assert got.capability_band == "unknown"

    def test_state_actor_with_observation_is_rated(self) -> None:
        alias = _alias(id="apt5", canonical="APT5", nation="cn")
        got = assess_actor(alias, _activity(total_articles=2), _NO_KEV)
        assert got is not None
        assert got.tier == "watch"  # R1 (敵性国家) × unknown → Watch (評価済み)


class TestNonTargets:
    def test_us_actor_is_watch(self) -> None:
        """同盟国側アクター: 敵性ベースラインが無く Watch に落ちる (対称性)。"""
        alias = _alias(id="equation", canonical="Equation Group", nation="us", mitre_ttps=_ttps(20))
        got = assess_actor(alias, _activity(total_articles=3), _NO_KEV)
        assert got is not None
        assert got.tier in ("watch", "moderate")
        assert got.relevance_band <= 1

    def test_organization_not_assessed(self) -> None:
        alias = _alias(id="prc_mss", canonical="PRC MSS", kind="organization", nation="cn")
        assert assess_actor(alias, None, _NO_KEV) is None


class TestRoutingIsNotEvidence:
    """自前の配信判断 (japan_targeted_count = victim∪channel) は関連度の証拠にしない。"""

    def test_channel_only_count_does_not_raise_relevance(self) -> None:
        alias = _alias(id="foo", canonical="FooBar")
        # japan_watch 配信 5 件 (channel 由来) だが victim=JP 観測は 0
        activity = _activity(total_articles=5, japan_targeted_count=5)
        got = assess_actor(alias, activity, _NO_KEV)
        assert got is not None
        assert got.relevance_band == 0
        # victim=JP が観測されて初めて上がる
        activity2 = _activity(
            total_articles=5,
            japan_targeted_count=5,
            victim_country_sectors=(("JP", "manufacturing", JP_SUSTAINED_MIN),),
        )
        got2 = assess_actor(alias, activity2, _NO_KEV)
        assert got2 is not None
        assert got2.relevance_band == 2


class TestIntentNoiseGuard:
    def test_single_intent_article_does_not_jump_tier(self) -> None:
        """観測 intent 1 件 (誤分類あり得る) では R3 に跳ねない。"""
        alias = _alias(id="apt5", canonical="APT5", nation="cn")
        activity = _activity(total_articles=1, top_intents=(("prepositioning", 1),))
        got = assess_actor(alias, activity, _NO_KEV)
        assert got is not None
        assert got.relevance_band == 1  # 敵性国家ベースラインのみ

    def test_sustained_prepositioning_observation_reaches_r3(self) -> None:
        alias = _alias(id="apt5", canonical="APT5", nation="cn")
        activity = _activity(total_articles=4, top_intents=(("prepositioning", 3),))
        got = assess_actor(alias, activity, _NO_KEV)
        assert got is not None
        assert got.relevance_band == 3


class TestTransparency:
    def test_every_assessment_has_rule_and_factors(self) -> None:
        alias = _alias(id="apt10", canonical="APT10", nation="cn", mitre_ttps=_ttps(16))
        got = assess_actor(alias, _activity(total_articles=2), _NO_KEV)
        assert isinstance(got, ActorThreatAssessment)
        assert got.tier_rule
        assert got.relevance_factors
        assert got.capability_factors
        assert got.activity_factors


class TestNonStateFamilies:
    """nation 帰属つき犯罪系は敵性国家扱いしない (2026-07-18: Qilin は APT ではない)。"""

    def test_ru_ransomware_gets_no_state_baseline(self) -> None:
        # nation=ru でも family=ransom_group なら「敵性国家」要因は立たない
        alias = _alias(
            id="lockbit",
            canonical="LockBit",
            nation="ru",
            family="ransom_group",
            associated_malware=("LockBit 3.0", "StealBit", "LockBit Green"),
        )
        got = assess_actor(alias, _activity(total_articles=5), _NO_KEV)
        assert got is not None
        assert got.relevance_band == 0  # 観測 (JP/同盟被害) が無ければ関連度なし
        assert not any("敵性国家" in f for f in got.relevance_factors)

    def test_jp_hitting_ransomware_unchanged_high(self) -> None:
        # JP 持続被害の犯罪系は従来どおり観測経路で High (受け入れ基準の不変確認)
        alias = _alias(
            id="qilin",
            canonical="Qilin",
            nation="ru",
            family="ransom_group",
            associated_malware=("Qilin", "Agenda"),
        )
        activity = _activity(
            total_articles=20,
            top_cves=("CVE-2024-21762",),
            victim_country_sectors=(("JP", "manufacturing", 12),),
        )
        got = assess_actor(alias, activity, frozenset({"CVE-2024-21762"}))
        assert got is not None
        assert got.tier == "high"
        assert got.relevance_band == 2

    def test_ru_ransomware_disruption_intent_does_not_elevate(self) -> None:
        # 犯罪系の disruption 観測は R2 (敵性国家×disruption) に乗らない
        alias = _alias(id="killnet", canonical="KillNet", nation="ru", family="hacktivist")
        activity = _activity(total_articles=6, top_intents=(("disruption", 5),))
        got = assess_actor(alias, activity, _NO_KEV)
        assert got is not None
        assert got.relevance_band == 0
