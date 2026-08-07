"""攻撃帰属集計の subject-gate SQL フラグメント (2026-07-27, 案A / §5)。

docs/body_extraction_and_entity_integrity_redesign.md §5。

地図 flow / 情勢ボード攻撃元 / 概況 actor-nation は ``article_entities`` の **言及 (mention)**
actor を数えており、記事の主題でないアクター (ランサム列挙記事の被害列挙、報道機関の言及等)
が攻撃帰属に混入していた (実測 90 日窓で 24%)。

subject-gate = 評価済み行 (subject_actor_source 非 NULL) は **主題メンバーシップ** に絞り、
legacy 行 (主題層 07-17 稼働前 = 窓の 40%) は mention に fallback する。membership は
**position ベース (INSTR、translate_sql が PG で STRPOS に変換)** — LIKE は id 中の `_` が
ワイルドカードになり、psycopg が `%,` を placeholder と誤解する二重の罠がある (shadow 試行)。

前提: mention actor value が canonical に正規化済み (scripts/backfill_actor_canonical.py) で
あること。subject_actor_ids も canonical のため literal メンバーシップで一致する。
"""

from __future__ import annotations


def subject_gate_clause(*, mention_col: str, subject_ids_col: str, subject_source_col: str) -> str:
    """subject-gate の SQL boolean フラグメントを返す (パラメータなし、列名のみ)。

    - ``mention_col``: 言及 actor id の列 (例 ``ae.value``)
    - ``subject_ids_col``: comma 連結の主題 id 列 (例 ``a.subject_actor_ids``)
    - ``subject_source_col``: 主題判定の source 列 (例 ``a.subject_actor_source``、NULL=legacy)

    INSTR(haystack, needle) は 1-based 位置 / 不在 0。両端をカンマで挟んで完全 id 一致に
    する (``,the_gentlemen,`` が ``,the_gentlemen,akira_ransom,`` に含まれるか)。
    """
    membership = (
        f"INSTR(',' || COALESCE({subject_ids_col},'') || ',', ',' || {mention_col} || ',') > 0"
    )
    return f"({subject_source_col} IS NULL OR {membership})"


def passes_subject_gate(
    *, mention_id: str, subject_ids_csv: str | None, subject_source: str | None
) -> bool:
    """:func:`subject_gate_clause` の Python 版 (text-regex 抽出経路用、SQL と同一意味論)。

    SQL 版は ``article_entities`` を JOIN する消費者向けだが、脅威スナップショット
    (``threat_operations``) は title+summary の正規表現抽出で mention actor を得るため
    SQL gate を適用できない。同じ規則を Python で再現する:

    - ``subject_source`` が NULL/空 (主題層 07-17 稼働前 = legacy) → mention をそのまま通す
    - それ以外 (評価済み) → ``mention_id`` が ``subject_ids_csv`` (カンマ連結) のメンバーのみ
      通す。``subject_ids_csv`` が空文字 ('' = 評価済み・主題なし) なら全 mention を落とす。
    """
    if not subject_source:
        return True
    ids = {s for s in (subject_ids_csv or "").split(",") if s}
    return mention_id in ids
