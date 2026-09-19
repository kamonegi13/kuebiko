"""識別子の取り違え (近いが違う値) を直す (2026-09-19、SYNTHESIS §53 の接地違反)。

審判が接地違反として挙げた実例は ``151.0.7922.138`` を ``151.0.79222.138`` と書いた
1 桁挿入だった。version/cvss は表記の変種が無限なので**一律の強制は本文を壊す**
(実測: カタログに無い version/cvss 58 件のうち 57 件は 7.5 / 9.1 のような短い値で、
置換すると正しい記述が消える)。**長い値が 1 文字だけ違う**ときだけ取り違えと見なす。

実測 (3 腕 116 件): 長さ 6 以上 + 編集距離 1 で発火するのは実例 1 件のみ。
"""

from __future__ import annotations

import pytest

from src.tools.identifier_catalog import IdentifierCatalog, near_miss_value


@pytest.mark.parametrize(
    ("written", "known", "expected"),
    [
        # 実例: 1 桁挿入した版数 → カタログの値に直す
        ("151.0.79222.138", {"151.0.7922.138"}, "151.0.7922.138"),
        # 短い値は偶然 1 文字違いになる。直したら別の数字に化ける
        ("6.0", {"3.0", "7.5"}, None),
        # 完全一致は取り違えではない (呼び出し側が先に弾く前提だが二重に守る)
        ("151.0.7922.138", {"151.0.7922.138"}, None),
        # 2 文字以上違えば別の値とみなす (勝手に直さない)
        ("151.0.79222.1388", {"151.0.7922.138"}, None),
        # 候補が 2 つ以上あるときは決められない → 直さない
        ("10.0.19045.1", {"10.0.19045.2", "10.0.19045.3"}, None),
        ("7.4.9", set(), None),
    ],
)
def test_near_miss(written: str, known: set[str], expected: str | None) -> None:
    assert near_miss_value(written, known) == expected


def test_threshold_is_explicit_not_magic() -> None:
    """長さ下限は実測から置いた定数で、呼び出し側から見えること。"""
    from src.tools.identifier_catalog import NEAR_MISS_MIN_LEN

    assert NEAR_MISS_MIN_LEN >= 6
    assert (
        near_miss_value("1" * (NEAR_MISS_MIN_LEN - 1), {"2" + "1" * (NEAR_MISS_MIN_LEN - 2)})
        is None
    )


# ---------- 解決経路への配線 ----------


def _catalog(*values: str) -> IdentifierCatalog:
    from src.tools.identifier_catalog import build_catalog

    return build_catalog([" ".join(values)])


def test_resolve_text_repairs_a_corrupted_version() -> None:
    """1 桁挿入した版数はカタログの値へ直し、直したことを数える。"""
    from src.tools.identifier_catalog import resolve_text

    cat = _catalog("Chrome 151.0.7922.138 で修正された")
    out, stats = resolve_text("修正版は 151.0.79222.138 である", cat)

    assert "151.0.7922.138" in out
    assert "151.0.79222.138" not in out
    assert stats.literal_repaired == 1
    assert stats.literal_flagged == 0  # 直したものは「計数のみ」に数えない


def test_resolve_text_leaves_short_values_alone() -> None:
    """短い値は偶然 1 文字違いになる。直すと別の数字に化けるので触らない。"""
    from src.tools.identifier_catalog import resolve_text

    cat = _catalog("バージョン 3.0 が影響を受ける")
    out, stats = resolve_text("バージョン 6.0 が影響を受ける", cat)

    assert "6.0" in out
    assert stats.literal_repaired == 0
    assert stats.literal_flagged == 1
