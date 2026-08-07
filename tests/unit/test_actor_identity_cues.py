"""曖昧アクターの同一性 cue 照合 (docs/actor_identity_cue_design.md) の unit test。

2026-07-30 実測: 一般語衝突アクター (play/deadlock/tick 等) の言及 entity の 59% が
誤検出疑い。根因はジャンル語 cue (CTI 記事なら常に成立) で曖昧解消していたこと。
本テストは実際の誤検出事例を固定化し、同一性証拠 (E0-E7) への置換を検証する。
"""

from __future__ import annotations

import pytest

from src.cti.actor_normalizer import (
    ActorAlias,
    ActorAliasRegistry,
    has_identity_evidence,
)

# 実誤検出事例に基づく合成テキスト (ジャンル語を含む = 旧 cue なら誤マッチした文脈)
_PLAY_VERB_TEXT = (
    "Cheap TV streaming devices play a key role in the ad-fraud network. "
    "The malware authors monetize infected ransomware-adjacent botnets."
)
_DEADLOCK_TECH_TEXT = (
    "Siemens SIMATIC S7-1500 CPU の脆弱性により deadlock 状態が発生し、"
    "サイバー攻撃によるサービス拒否につながる可能性がある。"
)
_TICK_SPY_TEXT = (
    "中央アジアを標的としたサイバースパイ活動で新種のバックドアを確認。"
    "感染チェーンは tick のタイミングで payload を復号する。"
)


def _play() -> ActorAlias:
    return ActorAlias(
        id="play",
        canonical="Play",
        aliases=("PlayCrypt",),
        ambiguous=True,
        family="ransom_group",
    )


def _deadlock() -> ActorAlias:
    return ActorAlias(id="deadlock", canonical="Deadlock", ambiguous=True)


def _tick() -> ActorAlias:
    return ActorAlias(
        id="tick",
        canonical="Tick",
        aliases=("Bronze Butler", "REDBALDKNIGHT"),
        mitre_group="G0060",
        associated_malware=("Daserf", "Datper"),
        ambiguous=True,
        context_cues=("bronze butler", "daserf", "datper"),
    )


def _registry(*actors: ActorAlias) -> ActorAliasRegistry:
    return ActorAliasRegistry(actors=tuple(actors))


class TestFalsePositivesRejected:
    """旧 cue で誤マッチしていた実事例が、同一性証拠なしでは落ちること。"""

    def test_play_verb_usage_rejected(self) -> None:
        assert _registry(_play()).find_all(_PLAY_VERB_TEXT) == []

    def test_deadlock_technical_term_rejected(self) -> None:
        assert _registry(_deadlock()).find_all(_DEADLOCK_TECH_TEXT) == []

    def test_tick_word_with_spy_context_rejected(self) -> None:
        # 2026-07-30 の実報告事例: 「サイバースパイ活動」+ 単語 tick
        assert _registry(_tick()).find_all(_TICK_SPY_TEXT) == []


class TestIdentityEvidenceAccepted:
    """正当な言及 (同一性証拠あり) は従来どおりマッチすること。"""

    def test_adjacent_ransomware_qualifier(self) -> None:
        # E4: 正当な言及は必ず修飾付きで書かれる
        text = "Play ransomware has claimed responsibility for the attack."
        assert [a.id for a in _registry(_play()).find_all(text)] == ["play"]

    def test_adjacent_group_qualifier(self) -> None:
        text = "The Play group exfiltrated data before encryption."
        assert [a.id for a in _registry(_play()).find_all(text)] == ["play"]

    def test_japanese_order_qualifier(self) -> None:
        # E5: 和文語順「ランサムウェア Play」
        text = "ランサムウェア Play が国内製造業を攻撃した。"
        assert [a.id for a in _registry(_play()).find_all(text)] == ["play"]

    def test_victim_record_title(self) -> None:
        # E6: ransomware.live 被害者レコード形式 (title 先頭 "play:")
        text = "play: Acme Corporation (US)\n被害企業の詳細情報。"
        assert [a.id for a in _registry(_play()).find_all(text)] == ["play"]

    def test_other_alias_cooccurrence(self) -> None:
        # E1: 別名 PlayCrypt の共起
        text = "The play operation, also known as PlayCrypt, resurfaced."
        assert [a.id for a in _registry(_play()).find_all(text)] == ["play"]

    def test_associated_malware_cooccurrence(self) -> None:
        # E2: 関連マルウェア Daserf の共起 (tick)
        text = "tick の攻撃で Daserf バックドアが使用された。"
        assert [a.id for a in _registry(_tick()).find_all(text)] == ["tick"]

    def test_mitre_group_id_cooccurrence(self) -> None:
        # E3: MITRE Group ID の共起
        text = "The tick intrusion set (G0060) targeted Japanese defense."
        assert [a.id for a in _registry(_tick()).find_all(text)] == ["tick"]

    def test_multiword_alias_is_self_evidence(self) -> None:
        # E0: 複数語の別名 (Bronze Butler) はそれ自体が固有 = 単独でマッチ
        text = "Bronze Butler が日本の防衛産業を標的にしている。"
        assert [a.id for a in _registry(_tick()).find_all(text)] == ["tick"]

    def test_explicit_specific_cue(self) -> None:
        # E7: 手書き固有 cue (datper)
        text = "tick 関連の datper 検体が解析された。"
        assert [a.id for a in _registry(_tick()).find_all(text)] == ["tick"]


class TestFnPatternsFromSampling:
    """FN サンプル実測 (2026-07-31、41件中8件) で見つけた正当言及パターンの救済。"""

    def test_victim_publication_title(self) -> None:
        # 被害者公表ニュース: 「Anubis、新たな被害組織として X を公開」
        anubis = ActorAlias(id="anubis", canonical="Anubis", ambiguous=True)
        text = "Anubis、新たな被害組織として FÉTIS Group と SECOM Engineering を公開"
        assert [a.id for a in _registry(anubis).find_all(text)] == ["anubis"]

    def test_ransomware_digest_tally_line(self) -> None:
        # ダイジェスト集計行: "Everest - 7 victims"
        everest = ActorAlias(id="everest", canonical="Everest", ambiguous=True)
        text = "Daily Ransomware Digest\nTop Groups: Everest - 7 victims\nQilin - 4 victims"
        assert [a.id for a in _registry(everest).find_all(text)] == ["everest"]

    def test_cyber_extortion_qualifier(self) -> None:
        # "Kairos cyber extortion group" (cyber が隣接修飾)
        kairos = ActorAlias(id="kairos", canonical="Kairos", ambiguous=True)
        text = "paid a $1 million ransom to the Kairos cyber extortion group"
        assert [a.id for a in _registry(kairos).find_all(text)] == ["kairos"]

    def test_mountain_everest_still_rejected(self) -> None:
        # 追加パターンが山のエベレストまで通さないこと
        everest = ActorAlias(id="everest", canonical="Everest", ambiguous=True)
        text = "エベレスト近郊に中国のドローン研究拠点が誕生。Everest base camp の測候所。"
        assert _registry(everest).find_all(text) == []


class TestNonAmbiguousUnaffected:
    """非曖昧アクター (希少語名) は名前一致のみで従来どおりマッチ。"""

    def test_rare_name_matches_without_evidence(self) -> None:
        lazarus = ActorAlias(id="lazarus", canonical="Lazarus")
        text = "Lazarus が暗号資産取引所を攻撃した。"
        assert [a.id for a in _registry(lazarus).find_all(text)] == ["lazarus"]


class TestRollbackFlag:
    """ACTOR_IDENTITY_CUES=0 で旧挙動 (ジャンル cue) に復帰できること。"""

    def test_flag_off_restores_genre_cue_behavior(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ACTOR_IDENTITY_CUES", "0")
        # 旧挙動: ransomware ジャンル語の共起で play がマッチしてしまう
        assert [a.id for a in _registry(_play()).find_all(_PLAY_VERB_TEXT)] == ["play"]


class TestHasIdentityEvidenceUnit:
    def test_no_evidence_returns_false(self) -> None:
        assert has_identity_evidence(_play(), "Play", _PLAY_VERB_TEXT) is False

    def test_case_insensitive(self) -> None:
        text = "PLAY RANSOMWARE hits another target"
        assert has_identity_evidence(_play(), "Play", text) is True


class TestSelfNamedMalwareGuard:
    """2026-08-01: actor 名と同名の malware が E2 を自己証明化する穴の regression。"""

    def _actor(self) -> ActorAlias:
        return ActorAlias(
            id="akira_ransom",
            canonical="Akira ransomware",
            aliases=("Akira",),
            ambiguous=True,
            associated_malware=("Akira", "Megazord"),
        )

    def test_self_named_malware_is_not_evidence(self) -> None:
        # 本文に "Akira" しか無い (= マッチ名自身) → 同一性証拠にならない
        assert not has_identity_evidence(
            self._actor(), "Akira", "Akira is a classic anime film from 1988."
        )

    def test_distinct_malware_still_counts(self) -> None:
        assert has_identity_evidence(
            self._actor(), "Akira", "Akira deployed the Megazord encryptor on the network."
        )

    def test_victim_record_format_still_matches(self) -> None:
        assert has_identity_evidence(self._actor(), "Akira", "akira: Northwood Country Club")
