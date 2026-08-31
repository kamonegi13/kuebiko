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


def test_merged_into_is_updatable() -> None:
    """遡及統合が ``merged_into`` を立てられること。

    ⚠ update_event_item は allowlist 制御で、**列名が無いと黙って無視される**。
    読む側 (一覧・詳細・公開面・写し) は全経路が merged_into を見て除外するのに、
    書く側の allowlist に無いまま統合スクリプトを走らせると、
    「成功した」と表示されて 1 件も統合されない。
    """
    from src.storage.repo_eventnews import _EVENT_ITEM_UPDATABLE_COLUMNS

    assert "merged_into" in _EVENT_ITEM_UPDATABLE_COLUMNS


class TestDifferentVictimsAreDifferentEvents:
    """名指しの被害者が食い違うなら、cos がいくら高くても別の事象。

    身代金リークサイトの投稿は「アクター: 被害者 (国)」という**ほぼ同一の書式**
    なので、被害者が違っても cos が 0.75-0.78 に来る (実測 2026-08-31)。actor 名を
    共有するだけで既定の 0.70 を超えるため、**アクター名を辞書へ入れた瞬間に
    そのグループの被害者が全部 1 事象へ潰れる**。
    """

    def test_different_victims_are_blocked(self) -> None:
        from src.eventnews.grouping import blocked_by_different_victims

        a = frozenset({("actor", "xpl0itrs"), ("victim_org", "bmwgroup")})
        b = frozenset({("actor", "xpl0itrs"), ("victim_org", "gruppospaggiariparma")})
        assert blocked_by_different_victims(a, b)

    def test_same_victim_is_allowed(self) -> None:
        from src.eventnews.grouping import blocked_by_different_victims

        a = frozenset({("actor", "qilin"), ("victim_org", "atf")})
        b = frozenset({("victim_org", "atf")})
        assert not blocked_by_different_victims(a, b)

    def test_one_side_without_a_named_victim_is_not_blocked(self) -> None:
        """技術解説や続報は被害者名を持たないことがある。塞ぐと正しい合流が落ちる。"""
        from src.eventnews.grouping import blocked_by_different_victims

        a = frozenset({("actor", "qilin"), ("victim_org", "atf")})
        b = frozenset({("actor", "qilin")})
        assert not blocked_by_different_victims(a, b)

    def test_an_article_covering_several_victims_still_joins(self) -> None:
        """「新たに 3 つの被害者」型の記事は、そのうち 1 つと重なれば繋がる。"""
        from src.eventnews.grouping import blocked_by_different_victims

        a = frozenset({("victim_org", "x"), ("victim_org", "y"), ("victim_org", "z")})
        b = frozenset({("victim_org", "y")})
        assert not blocked_by_different_victims(a, b)


class TestActorNameAloneIsNotEnough:
    """アクター名だけの共有では繋がない (2026-08-31)。

    アクターは「誰が」であって「何が起きたか」ではない。同じ攻撃者の別作戦は必ず
    アクター名を共有するので、単独で辺の根拠にすると活動が活発な攻撃者ほど 1 事象へ
    潰れる。実測: Kimsuky の 6 記事が 1 つになり、うち 3 件は別作戦だった。
    """

    def test_actor_alone_is_blocked(self) -> None:
        from src.eventnews.grouping import shared_is_actor_name_only

        assert shared_is_actor_name_only([("actor", "kimsuky")])

    def test_same_name_as_actor_and_malware_is_still_actor_alone(self) -> None:
        """⚠ 型ではなく名前で見る。``kimsuky`` は両方の型に抽出される (実例)。"""
        from src.eventnews.grouping import shared_is_actor_name_only

        assert shared_is_actor_name_only([("actor", "kimsuky"), ("malware_family", "Kimsuky")])

    def test_actor_plus_a_tool_is_allowed(self) -> None:
        """作戦を分けるのは「何を使ったか」。ENKI の Kimsuky 報告 3 件がこの形。"""
        from src.eventnews.grouping import shared_is_actor_name_only

        assert not shared_is_actor_name_only(
            [("actor", "kimsuky"), ("tool", "Chrome Remote Desktop")]
        )

    def test_a_cve_alone_is_still_enough(self) -> None:
        """CVE・被害者名・マルウェア名は「何が起きたか」を指すので 1 つで足りる。"""
        from src.eventnews.grouping import shared_is_actor_name_only

        assert not shared_is_actor_name_only([("cve", "CVE-2026-1000")])
        assert not shared_is_actor_name_only([("victim_org", "atf")])
        assert not shared_is_actor_name_only([("malware_family", "Medusa")])


def test_tool_is_a_join_signal() -> None:
    from src.eventnews.models import JOIN_ENTITY_TYPES

    assert "tool" in JOIN_ENTITY_TYPES


def test_the_loader_reads_every_join_entity_type() -> None:
    """読み込み SQL の型一覧が ``JOIN_ENTITY_TYPES`` から導かれていること。

    ⚠ 2026-08-31: 定数に ``tool`` を足したのに、毎時ジョブの SQL が型名をベタ書き
    していたため一切読み込まれず、**変更が丸ごと無効**だった。定数と SQL が別々に
    存在する限り必ずずれるので、導出になっていることをここで固定する。
    """
    from src.eventnews.models import JOIN_ENTITY_TYPES
    from src.ui.services.eventnews_hourly_job import (
        _SQL_ENTITIES_BY_ID,
        _SQL_ENTITY_COUNTS,
    )

    for entity_type in JOIN_ENTITY_TYPES:
        assert f"'{entity_type}'" in _SQL_ENTITIES_BY_ID, entity_type
        assert f"'{entity_type}'" in _SQL_ENTITY_COUNTS, entity_type
