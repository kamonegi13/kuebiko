"""特異な名前を複数共有するときの緩和 (2026-08-31) の境界を固定する。

利用者報告「Fire Ant の記事が統合されず別々に新着へ上がる」から。実測すると
Sygnia の技術ブログとプレスリリースが **cos=0.6915** で、0.70 に 0.0085 足りず
割れていた。同じ帯 (0.60-0.70) に 14 日で 128 組が滞留していた。

ここで固定するのは「どこまで緩めるか」ではなく **緩めてよい条件**:
- 名前を 2 つ以上共有する → 緩める
- 同じ名前が type 違いで 2 回出ただけ → 緩めない
- 1 つの名前しか共有しない → 緩めない (ランサム流出サイトが接着する)
"""

from __future__ import annotations

from src.eventnews.grouping import distinct_shared_names, required_cos
from src.eventnews.models import (
    COS_THRESHOLD,
    FOCAL_CVE_COS,
    SHARED_NAMES_COS,
)


def _cves(n: int) -> frozenset[tuple[str, str]]:
    return frozenset(("cve", f"CVE-2026-{1000 + i}") for i in range(n))


def test_fire_ant_pair_is_relaxed() -> None:
    """報告された実例。malware_family を 2 つ共有し cos=0.6915 だった。"""
    shared = [("malware_family", "BridgeAgent"), ("malware_family", "TacTap")]
    ents = frozenset(shared)
    assert required_cos(ents, ents, shared) == SHARED_NAMES_COS
    assert SHARED_NAMES_COS <= 0.6915, "この緩和で Fire Ant が繋がること"


def test_same_name_in_two_types_counts_once() -> None:
    """``kimsuky`` は actor と malware_family の両方に出る (実例)。

    entity の数で数えると 1 つの根拠が 2 つに見え、別キャンペーンが繋がる。
    """
    shared = [("actor", "kimsuky"), ("malware_family", "Kimsuky")]
    assert distinct_shared_names(shared) == 1
    ents = frozenset(shared)
    assert required_cos(ents, ents, shared) == COS_THRESHOLD


def test_single_shared_actor_is_not_relaxed() -> None:
    """ランサム流出サイトは「同じグループ・別の被害者」で actor 1 名を共有する。

    実測では cos 0.678-0.695 に来るので、緩めると別々の被害者が 1 事象になる
    (Dark Project / Play / Arcusmedia)。
    """
    shared = [("actor", "play")]
    ents = frozenset(shared)
    assert required_cos(ents, ents, shared) == COS_THRESHOLD


def test_focal_cve_still_wins() -> None:
    """既存の焦点 CVE 例外は据え置き (より緩い側が優先)。"""
    shared = [("cve", "CVE-2026-1000"), ("cve", "CVE-2026-1001")]
    ents = _cves(2)
    assert required_cos(ents, ents, shared) == FOCAL_CVE_COS


def test_bulk_advisory_sharing_two_cves_gets_the_names_relaxation() -> None:
    """一括アドバイザリ同士でも、**複数の CVE を共有**するなら繋いでよい。

    焦点 CVE の例外は「両方が CVE を数個しか持たない」ことを求めるため、Cisco や
    Dell の複数脆弱性アドバイザリは対象外だった。実測でこれらが 4 事象に割れていた。
    """
    shared = [("cve", "CVE-2026-1000"), ("cve", "CVE-2026-1001")]
    bulk = _cves(10)
    assert required_cos(bulk, bulk, shared) == SHARED_NAMES_COS


def test_nothing_shared_keeps_the_default() -> None:
    assert required_cos(frozenset(), frozenset(), []) == COS_THRESHOLD
