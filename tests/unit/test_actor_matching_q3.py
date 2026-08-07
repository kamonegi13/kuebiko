"""Phase 1 Q3: actor 名の word-boundary 一致 (substring 誤爆の排除)。"""

from __future__ import annotations

import pytest

from src.cti.actor_normalizer import ActorAlias, ActorAliasRegistry


def _reg(*actors: ActorAlias) -> ActorAliasRegistry:
    return ActorAliasRegistry(actors=tuple(actors))


@pytest.mark.unit
def test_apt1_does_not_match_apt10() -> None:
    # 旧 substring 実装では "APT10" の中に "APT1" が誤爆していた
    reg = _reg(
        ActorAlias(id="apt1", canonical="APT1"),
        ActorAlias(id="apt10", canonical="APT10"),
    )
    found = {a.id for a in reg.find_all("APT10 が新たな攻撃を実施")}
    assert found == {"apt10"}  # apt1 は混入しない


@pytest.mark.unit
def test_no_substring_false_positive_apt3_apt30() -> None:
    reg = _reg(ActorAlias(id="apt3", canonical="APT3"))
    assert reg.find_all("APT30 の活動を観測") == []
    assert reg.find("APT30 の活動") is None


@pytest.mark.unit
def test_actor_adjacent_to_japanese_is_matched() -> None:
    # 日本語混在で ASCII/CJK 境界に \b が無くても拾えること (現行挙動の保持)
    reg = _reg(ActorAlias(id="lazarus", canonical="Lazarus"))
    assert reg.find("Lazarusが暗号資産を窃取") is not None
    detail = reg.find_all("Lazarus型のマルウェアを使用")
    assert detail and detail[0].id == "lazarus"


@pytest.mark.unit
def test_multiword_actor_in_jp_context() -> None:
    reg = _reg(ActorAlias(id="volt", canonical="Volt Typhoon"))
    assert reg.find("中国系 Volt Typhoon の事前配置が判明") is not None


@pytest.mark.unit
def test_punctuation_and_standalone_boundaries() -> None:
    reg = _reg(ActorAlias(id="apt1", canonical="APT1"))
    assert reg.find("APT1") is not None  # 単独
    assert reg.find("（APT1）による攻撃") is not None  # 全角括弧
    assert reg.find("see APT1. ") is not None  # ピリオド
    assert reg.find("xAPT1y") is None  # 前後に ASCII 英数 → 一致しない


@pytest.mark.unit
def test_alias_also_word_boundary() -> None:
    # alias 側も同じ規則
    reg = _reg(ActorAlias(id="apt41", canonical="APT41", aliases=("Wicked Panda",)))
    assert reg.find("Wicked Panda の新ツール") is not None
    assert reg.find_all("APT41 と Wicked Panda")[0].id == "apt41"
    # APT4 (架空) は APT41 に誤爆しない
    reg2 = _reg(ActorAlias(id="apt4", canonical="APT4"))
    assert reg2.find_all("APT41 の活動") == []


@pytest.mark.unit
def test_case_insensitive() -> None:
    reg = _reg(ActorAlias(id="lazarus", canonical="Lazarus"))
    assert reg.find("lazarus group") is not None
    assert reg.find("LAZARUS") is not None
