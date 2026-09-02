"""分割ロジック (src/eventnews/split.py) の不変条件。"""

from __future__ import annotations

from src.eventnews.split import split_components


def _p(*pairs: tuple[str, str, float]) -> dict[frozenset[str], float | None]:
    return {frozenset((a, b)): v for a, b, v in pairs}


def test_chain_with_rejected_far_edge_still_holds_via_approved_edges() -> None:
    """A-B, B-C が承認なら A-C が否認でも同じ成分 (連結は辺の承認で決まる)。"""
    # Arrange
    probas = _p(("a", "b", 0.9), ("b", "c", 0.8), ("a", "c", 0.1))

    # Act
    main, rest = split_components(["a", "b", "c"], probas, edge_threshold=0.5)

    # Assert
    assert set(main) == {"a", "b", "c"} and rest == []


def test_hub_member_without_approved_edges_is_split_out() -> None:
    """どのメンバーとも承認辺を持たない記事 (一括勧告型) は外れる。"""
    # Arrange — d は全員に対して低確率
    probas = _p(
        ("a", "b", 0.9),
        ("a", "c", 0.8),
        ("b", "c", 0.85),
        ("a", "d", 0.2),
        ("b", "d", 0.3),
        ("c", "d", 0.1),
    )

    # Act
    main, rest = split_components(["a", "b", "c", "d"], probas, edge_threshold=0.5)

    # Assert
    assert set(main) == {"a", "b", "c"}
    assert rest == [["d"]]


def test_two_events_glued_by_nothing_split_into_components() -> None:
    """2 つの塊が承認辺で繋がっていなければ 2 成分に割れ、大きい方が本体。"""
    # Arrange
    probas = _p(("a", "b", 0.9), ("c", "d", 0.9), ("e", "c", 0.7), ("a", "c", 0.2))

    # Act
    main, rest = split_components(["a", "b", "c", "d", "e"], probas, edge_threshold=0.5)

    # Assert — c-d-e の 3 件が本体、a-b は **一塊のまま** 外れる
    assert set(main) == {"c", "d", "e"}
    assert [set(c) for c in rest] == [{"a", "b"}]


def test_unjudged_pairs_are_neutral_and_do_not_cause_splits() -> None:
    """判定できなかったペア (None) は中立 0.5 — 証拠の欠如を分割の根拠にしない。"""
    # Arrange — a-b だけ判定不能
    probas: dict[frozenset[str], float | None] = {frozenset(("a", "b")): None}

    # Act — ⚠ 本番閾値 0.7 で固定する。0.5 で試すと「中立 0.5 >= 0.5」で
    #   偶然通ってしまい、閾値 > 中立の逆転 (判定失敗ほど切れやすい) を見逃す
    #   (2026-09-03 の dry-run で実際にすり抜けた)。
    main, rest = split_components(["a", "b"], probas, edge_threshold=0.7)

    # Assert
    assert set(main) == {"a", "b"} and rest == []


def test_tie_keeps_the_component_of_the_earliest_member() -> None:
    """同数の成分は、並びで先に現れるメンバーの側が本体 (決定論)。"""
    # Arrange — {a,b} と {c,d} が同数
    probas = _p(("a", "b", 0.9), ("c", "d", 0.9))

    # Act
    main, rest = split_components(["a", "b", "c", "d"], probas, edge_threshold=0.5)

    # Assert
    assert set(main) == {"a", "b"}
    assert [set(c) for c in rest] == [{"c", "d"}]


def test_two_split_components_stay_as_two_groups() -> None:
    """外れる側が複数の塊なら、塊ごとに分かれて返る (単独事象へバラさない)。

    ⚠ 2026-09-03 の dry-run で、同一被害者の「公開記事 + 掲載記録」の正しい
    ペアが、群から外れる際に別々の単独事象へ割られていた。
    """
    # Arrange — 本体 {a,b,c} / 外れる塊 {d,e} と {f}
    probas = _p(
        ("a", "b", 0.9),
        ("b", "c", 0.9),
        ("d", "e", 0.95),
        ("a", "d", 0.1),
        ("a", "f", 0.1),
        ("d", "f", 0.1),
    )

    # Act
    main, rest = split_components(["a", "b", "c", "d", "e", "f"], probas, edge_threshold=0.7)

    # Assert
    assert set(main) == {"a", "b", "c"}
    assert [set(c) for c in rest] == [{"d", "e"}, {"f"}]
