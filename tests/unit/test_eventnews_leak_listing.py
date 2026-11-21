"""流出サイトの掲載の形 (束と 1 件) を近さの統合候補から外す (2026-11-21)。"""

from __future__ import annotations

import pytest

from src.eventnews.leak_listing import is_leak_form, is_leak_listing_pair

_LOCKBIT = [("actor", "lockbit")]


@pytest.mark.parametrize(
    ("aid", "title", "expected"),
    [
        ("rl_abc", "anything", True),
        ("x", "LockBit 5.0、ダークウェブ上で 5 件の新規被害組織を掲載", True),
        ("x", "Akira ランサムウェア、米国企業 2 社をリークサイトに掲載", True),
        ("x", "LockBit: victim-a.example (US)", True),
        ("x", "LockBit5: sun-clinical.example", True),
        # 英字の題だがグループ名と一致しない (「ARTEX: …」のような普通の題)
        ("x", "ARTEX: 攻撃者が AI エージェントで攻撃を大規模化する手口", False),
        ("x", "FBI 警告:ハッカーが AI ツールを悪用", False),
        ("x", "LockBit の新しい暗号化手法を解析", False),
    ],
)
def test_is_leak_form(aid: str, title: str, expected: bool) -> None:
    assert is_leak_form(aid, title, ["lockbit"]) is expected


def test_pair_requires_group_names_only() -> None:
    a = ("x", "LockBit 5.0、被害組織 6 件を掲載 (リークサイト)")
    b = ("y", "LockBit: victim-a.example (US)")

    assert is_leak_listing_pair(_LOCKBIT, a, b) is True
    # 被害組織・道具の名前を共有する組には当てない
    assert is_leak_listing_pair([("victim_org", "binance")], a, b) is False
    assert is_leak_listing_pair(_LOCKBIT + [("tool", "cairn")], a, b) is False
    assert is_leak_listing_pair([], a, b) is False


def test_pair_without_leak_form_is_not_blocked() -> None:
    a = ("x", "LockBit の運営者を起訴")
    b = ("y", "LockBit 幹部、米司法省が起訴")

    assert is_leak_listing_pair(_LOCKBIT, a, b) is False
