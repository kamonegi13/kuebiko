"""言及の照合し直しが、今の辞書 (取込と同じ find_all) で判定すること。"""

from __future__ import annotations

from scripts.revalidate_actor_mentions import still_matches
from src.cti.actor_normalizer import ActorAlias, ActorAliasRegistry


def test_still_matches_uses_current_dictionary() -> None:
    reg = ActorAliasRegistry(
        actors=(ActorAlias(id="apt37", canonical="APT37", aliases=("ScarCruft",)),)
    )
    assert still_matches(reg, "apt37", ["ScarCruft が攻撃"])
    assert not still_matches(reg, "apt37", ["米空軍の MQ-9 Reaper 後継機", ""])
