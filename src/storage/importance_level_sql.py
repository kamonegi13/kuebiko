"""記事の重要度 6 段階 (importance_level) を SQL の ORDER BY で導くための式。

SSoT は ``src/cti/importance_v2.py:importance_level()`` (深刻さ × 関連性 → 1〜6、
深刻さが無ければ None)。SQL は Python 関数をそのまま呼べないため、この CASE 式は
それを**鏡写しした別表現**であり、重複ではなく対照 — 2 つが食い違わないことは
``tests/unit/test_importance_level_sql.py`` が全 6 通り + None で固定する。
``importance_level()`` の決まりごとを変えたら、ここも同時に直すこと。

一覧の「重要度順」ソートは並べ替えだけなので、値そのものは Python 側
(``src/storage/repo_importance_v2.py:importance_v2_by_article``) から
``importance_level()`` を直接呼んで返す — ORDER BY に埋める式を重複させない。
"""

from __future__ import annotations

#: {v} = article_importance_v2 への別名 (severity / relevant 列を持つテーブルの別名)。
LEVEL_CASE_EXPR = (
    "CASE {v}.severity"
    " WHEN 'S3' THEN (CASE WHEN {v}.relevant = 1 THEN 1 ELSE 2 END)"
    " WHEN 'S2' THEN (CASE WHEN {v}.relevant = 1 THEN 3 ELSE 4 END)"
    " WHEN 'S1' THEN (CASE WHEN {v}.relevant = 1 THEN 5 ELSE 6 END)"
    " ELSE NULL END"
)


def level_scalar_subquery(article_id_expr: str) -> str:
    """1 記事分の重要度レベルを ``article_importance_v2`` への相関サブクエリで返す式。

    ``article_id_expr`` は記事側の article_id 列参照 (例: ``"articles.article_id"``)。
    JOIN ではなく相関サブクエリにするのは、既存の WHERE 句が列名を table 修飾なしで
    参照しているため (JOIN すると article_id / created_at が両テーブルに在って
    ambiguous になる)。
    """
    return (
        f"(SELECT {LEVEL_CASE_EXPR.format(v='_lv')} FROM article_importance_v2 _lv"
        f" WHERE _lv.article_id = {article_id_expr})"
    )


def min_level_subquery_for_event(item_id_expr: str) -> str:
    """事象 1 件分の最良 (最小) レベル = 構成記事のうち最小の level。

    ``item_id_expr`` は event_items 側の id 列参照 (例: ``"event_items.id"``)。
    """
    return (
        "(SELECT MIN(" + LEVEL_CASE_EXPR.format(v="_lv") + ") FROM event_item_members _m"
        " JOIN article_importance_v2 _lv ON _lv.article_id = _m.article_id"
        f" WHERE _m.item_id = {item_id_expr})"
    )


def order_by_level_then_recency(level_expr: str, recency_expr: str) -> str:
    """level ASC (NULL は最後) → 既定の新しい順、の ORDER BY 本体。

    先頭の ``ORDER BY`` は呼び手が付ける。
    """
    return (
        f"(CASE WHEN {level_expr} IS NULL THEN 1 ELSE 0 END) ASC, {level_expr} ASC, {recency_expr}"
    )


__all__ = [
    "LEVEL_CASE_EXPR",
    "level_scalar_subquery",
    "min_level_subquery_for_event",
    "order_by_level_then_recency",
]
