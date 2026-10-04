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


#: 深刻さ facet (``min_severity``) の許容値 → ``article_importance_v2.severity`` の許容集合。
#: 常に上位からの prefix (S3 ⊂ {S3,S2} ⊂ {S3,S2,S1}) — 「最良 (最小) がこの集合に入る」と
#: 「1 件でも入る」が同値になる前提 (事象側の EXISTS 化で利用)。
SEVERITY_ALLOWED: dict[str, tuple[str, ...]] = {
    "S3": ("S3",),
    "S2": ("S3", "S2"),
    "S1": ("S3", "S2", "S1"),
}


def severity_relevance_sql(
    min_severity: str,
    relevant_only: bool,
    include_strategic: bool,
) -> str | None:
    """深刻さ・関連性・戦略上の重みの 3 独立 facet (2026-10-04) を 1 行 (記事 1 件分の
    ``article_importance_v2``) に対する boolean 式へ合成する。絞り込み無しなら None。

    - ``min_severity``: "" (すべて) / "S3" (重大のみ) / "S2" (注意以上) / "S1" (参考以上)
    - ``relevant_only``: True なら関連性ありだけ (深刻さの設定と独立に効く)
    - ``include_strategic``: ``min_severity`` が設定されているときだけ意味を持つ。
      深刻さ無し (non_cyber/no_axes 等) の記事は既定で除外されるが、これが True なら
      ``strategic_weight='heavy'`` の記事 (政策・地政学で注視国が主体) を合わせて含める
      (``relevant_only`` の対象にもなる)。
    """
    if not min_severity and not relevant_only:
        return None
    if not min_severity:
        return "relevant = 1"
    allowed = SEVERITY_ALLOWED.get(min_severity)
    if allowed is None:
        return None
    ph = ",".join(f"'{v}'" for v in allowed)
    core = f"severity IN ({ph})"
    if include_strategic:
        core = f"(({core}) OR (severity IS NULL AND strategic_weight = 'heavy'))"
    if relevant_only:
        core = f"(({core}) AND relevant = 1)"
    return core


def severity_relevance_exists_for_event(
    item_id_expr: str,
    min_severity: str,
    relevant_only: bool,
    include_strategic: bool,
) -> str | None:
    """事象 (``event_items``) 側の同じ 3 facet。構成記事の集合に対する EXISTS で判定する。

    深刻さの許容集合は常に prefix なので「最良 (最小) の構成記事が該当」=
    「1 件でも該当する構成記事がある」で同値 (``severity_relevance_sql`` のモジュール docstring
    参照)。関連性・戦略上の重みも「1 件でも該当するメンバーがいるか」で判定する
    (設計: 深刻さ=最良メンバーで判定 / 関連性=いずれかのメンバーが関連 /
    戦略上の重み=深刻さ無しで heavy なメンバーがいずれか)。``item_id_expr`` は
    ``event_items`` 側の id 列参照 (例: ``"event_items.id"``)。絞り込み無しなら None。
    """
    if not min_severity and not relevant_only:
        return None

    def _exists(cond: str) -> str:
        return (
            "EXISTS (SELECT 1 FROM event_item_members _sem"
            " JOIN article_importance_v2 _seiv ON _seiv.article_id = _sem.article_id"
            f" WHERE _sem.item_id = {item_id_expr} AND {cond})"
        )

    if not min_severity:
        return _exists("_seiv.relevant = 1")
    allowed = SEVERITY_ALLOWED.get(min_severity)
    if allowed is None:
        return None
    ph = ",".join(f"'{v}'" for v in allowed)
    core = _exists(f"_seiv.severity IN ({ph})")
    if include_strategic:
        strategic = _exists("_seiv.severity IS NULL AND _seiv.strategic_weight = 'heavy'")
        core = f"(({core}) OR ({strategic}))"
    if relevant_only:
        core = f"(({core}) AND ({_exists('_seiv.relevant = 1')}))"
    return core


__all__ = [
    "LEVEL_CASE_EXPR",
    "SEVERITY_ALLOWED",
    "level_scalar_subquery",
    "min_level_subquery_for_event",
    "order_by_level_then_recency",
    "severity_relevance_exists_for_event",
    "severity_relevance_sql",
]
