"""アクター単位の汚染の見張り (2026-09-27)。

主題判定の平均精度 (92%) は、特定のアクターに集中した誤りを隠す。APT37 は主題 26 記事中 17 記事が
無人機の記事 (別名 Reaper の衝突) だった。アクターの言及がサイバー以外 (地政学) の記事に偏るものを
週次で確認候補に出す。
"""

from __future__ import annotations

from src.cti.actor_contamination import ActorSkew, find_skewed_actors, skew_lines


def _rows(actor: str, geo: int, cyber: int) -> list[tuple[str, str, str]]:
    return [(actor, "geopolitical", f"geo {i}") for i in range(geo)] + [
        (actor, "breach", f"cyber {i}") for i in range(cyber)
    ]


def test_flags_actor_mostly_in_geopolitical_articles() -> None:
    rows = _rows("apt37", 17, 9) + _rows("apt28", 3, 40) + _rows("tiny", 3, 0)
    got = find_skewed_actors(rows, exclude=frozenset())
    assert [s.actor for s in got] == ["apt37"]  # tiny は件数不足、apt28 は偏りなし
    assert got[0].geo == 17 and got[0].total == 26


def test_excluded_actors_are_not_flagged() -> None:
    rows = _rows("noname", 20, 2)
    assert find_skewed_actors(rows, exclude=frozenset({"noname"})) == []


def test_lines() -> None:
    lines, warn = skew_lines([ActorSkew("apt37", 26, 17, ("geo 1",))])
    assert warn and "apt37" in lines[0] and "65%" in lines[0]
    assert skew_lines([]) == (["アクターの偏り: なし"], False)
