"""threat_actor_doctrine (国家アクター → 標的分野 doctrine) のテスト。

核心: (1) 国家アクター判定が nation 属性で state/criminal を分ける、(2) doctrine が
公的勧告由来の標的分野を返す、(3) doctrine ∪ 観測で分野を union する、(4) 戦略 intent が
金融犯罪を除外する。
"""

from __future__ import annotations

from src.cti.nisc_sectors import NISC_SECTORS
from src.cti.threat_actor_doctrine import (
    ALLIED_NATIONS,
    JP_TARGETING_ACTORS,
    PREPOSITIONING_DOCTRINE_ACTORS,
    STATE_ACTOR_SECTORS,
    STATE_NATIONS,
    STRATEGIC_INTENTS,
    actor_target_niscs,
    dominant_strategic_intent,
    is_state_actor,
    is_state_nation,
    jp_targeting_grounds,
    prepositioning_grounds,
)


class TestStateNation:
    def test_major_state_nations(self) -> None:
        for n in ("cn", "ru", "kp", "ir", "CN"):
            assert is_state_nation(n) is True

    def test_non_state(self) -> None:
        assert is_state_nation("") is False
        assert is_state_nation("us") is False  # 同盟国は「敵性国家アクター」レンズ対象外
        assert is_state_nation(None) is False


class TestDoctrineIntegrity:
    def test_sectors_valid(self) -> None:
        for name, sectors in STATE_ACTOR_SECTORS.items():
            assert sectors, name
            for s in sectors:
                assert s in NISC_SECTORS, f"{name}: {s}"

    def test_keys_are_lowercase(self) -> None:
        for name in STATE_ACTOR_SECTORS:
            assert name == name.lower()

    def test_volt_typhoon_prepositioning_lifelines(self) -> None:
        # CISA AA24-038A: Volt Typhoon = 通信/電力/水道/交通の事前潜伏
        sectors = STATE_ACTOR_SECTORS["volt typhoon"]
        assert "it_telecom" in sectors
        assert "electricity" in sectors
        assert "water" in sectors


class TestActorTargetNiscs:
    def test_doctrine_beats_sparse_observation(self) -> None:
        # 観測が 'defense' のみでも doctrine で lifeline 分野が補完される (Volt Typhoon 実態)
        got = actor_target_niscs(["Volt Typhoon"], observed_niscs=["defense"])
        assert "it_telecom" in got and "electricity" in got
        assert "defense" in got  # 観測も union

    def test_alias_match(self) -> None:
        got = actor_target_niscs(["BRONZE SILHOUETTE", "Vanguard Panda"], observed_niscs=[])
        assert "it_telecom" in got  # Volt Typhoon の別名

    def test_unknown_actor_falls_back_to_observation(self) -> None:
        got = actor_target_niscs(["架空アクター"], observed_niscs=["finance"])
        assert got == frozenset({"finance"})

    def test_no_signal_returns_empty(self) -> None:
        assert actor_target_niscs(["架空アクター"], observed_niscs=[]) == frozenset()


class TestDoctrineDictionaryConsistency:
    """doctrine の全 key が actor 辞書 (seed yaml) の名前に解決すること。

    2026-07-17 発見の dead key 再発防止: STATE_ACTOR_SECTORS の tick/bronze butler が
    辞書に不在で board の doctrine 射影が一度も発火していなかった。辞書からアクターを
    削除・改名したら doctrine も同時に直す (このテストが強制する)。
    """

    def _dictionary_names(self) -> set[str]:
        from src.cti.actor_normalizer import load_actor_aliases

        reg = load_actor_aliases()
        assert reg.actors, "辞書 seed (config/cti/actor_aliases.yaml) がロードできない"
        return {n.lower() for a in reg.actors for n in a.all_names}

    def test_state_actor_sectors_keys_resolve(self) -> None:
        names = self._dictionary_names()
        missing = [k for k in STATE_ACTOR_SECTORS if k not in names]
        assert not missing, f"辞書に解決できない doctrine key (dead key): {missing}"

    def test_jp_targeting_keys_resolve(self) -> None:
        names = self._dictionary_names()
        missing = [k for k in JP_TARGETING_ACTORS if k not in names]
        assert not missing, f"辞書に解決できない日本標的 doctrine key: {missing}"

    def test_prepositioning_keys_resolve(self) -> None:
        names = self._dictionary_names()
        missing = [k for k in PREPOSITIONING_DOCTRINE_ACTORS if k not in names]
        assert not missing, f"辞書に解決できない事前配置 doctrine key: {missing}"


class TestJpTargetingDoctrine:
    def test_keys_are_lowercase_and_grounded(self) -> None:
        for name, ground in JP_TARGETING_ACTORS.items():
            assert name == name.lower()
            assert ground.strip(), name  # 接地 (公的一次ソース) 必須

    def test_grounds_returned_for_alias(self) -> None:
        got = jp_targeting_grounds(["Earth Kasha"])
        assert got and "警察庁" in got[0]

    def test_non_listed_actor_returns_empty(self) -> None:
        # Volt Typhoon は日本標的の公的名指しが無い — 載せない (強制バイアス禁止)。
        # 対日関連は事前配置 doctrine 経由で独立に評価される
        assert jp_targeting_grounds(["Volt Typhoon"]) == ()
        assert prepositioning_grounds(["Volt Typhoon"]) != ()

    def test_grounds_deduplicated_across_aliases(self) -> None:
        got = jp_targeting_grounds(["APT10", "MenuPass", "Stone Panda"])
        assert len(got) == 1


class TestStateActor:
    """国家系アクター述語 (2026-07-18 ユーザー判断: Qilin は APT ではない)。

    nation 帰属 (ru 等) は事実として辞書に残すが、「国家系」レンズ (PIR actor_nations /
    脅威評価の敵性国家ベースライン / CI board 行動レーン) からは非国家系 family を除外する。
    """

    def test_criminal_ransomware_with_nation_is_not_state_actor(self) -> None:
        assert is_state_actor("ru", "ransom_group") is False  # Qilin/LockBit 型
        assert is_state_actor("ru", "hacktivist") is False  # NoName057(16) 型
        assert is_state_actor("ru", "spider") is False

    def test_state_families_and_family_less_state_actors(self) -> None:
        assert is_state_actor("ru", "sandworm") is True
        assert is_state_actor("cn", "panda") is True
        assert is_state_actor("cn", None) is True  # family 未設定の国家系は従来どおり
        assert is_state_actor("cn", "") is True

    def test_non_adversary_nation_is_not_state_actor(self) -> None:
        assert is_state_actor("us", None) is False
        assert is_state_actor(None, "sandworm") is False


class TestAlliedNations:
    def test_disjoint_from_adversaries_and_home(self) -> None:
        assert not (ALLIED_NATIONS & STATE_NATIONS)
        assert "jp" not in ALLIED_NATIONS  # 自国は同盟枠でなく専用の重み (R3/R2)

    def test_lowercase(self) -> None:
        assert all(n == n.lower() for n in ALLIED_NATIONS)


class TestDominantIntent:
    def test_prepositioning_wins_over_espionage(self) -> None:
        # 事前潜伏は最優先で炙り出す (I&W 最上位)
        assert (
            dominant_strategic_intent([("espionage", 5), ("prepositioning", 2)]) == "prepositioning"
        )

    def test_financial_excluded(self) -> None:
        # ランサム等の金融犯罪 intent は国家アクター行動レンズから除外
        assert dominant_strategic_intent([("financial", 10)]) is None

    def test_espionage_when_only_strategic(self) -> None:
        assert dominant_strategic_intent([("financial", 10), ("espionage", 3)]) == "espionage"

    def test_empty(self) -> None:
        assert dominant_strategic_intent([]) is None

    def test_all_intents_are_strategic(self) -> None:
        assert "financial" not in STRATEGIC_INTENTS
        assert "prepositioning" in STRATEGIC_INTENTS
