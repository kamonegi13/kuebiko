"""攻撃帰属集計の subject-gate SQL フラグメント (2026-07-27, 案A / §5)。

docs/body_extraction_and_entity_integrity_redesign.md §5。

導入時 (2026-07-27) は 地図 flow / 情勢ボード攻撃元 / 概況 actor-nation が
``article_entities`` の **言及 (mention)** actor を数えており、記事の主題でないアクターの
混入 (実測 90 日窓で 24%) を本 gate の後付けで抑えていた。**2026-08-20/21 にこの 3 消費者は
母集団自体を subject_actor_ids 起点に反転済み** (75c7dee / b44a2e6 — gate 後付けは呼び出し側
ごとに漏れるため)。現在 gate を使うのは **観測・言及の意味論が正しい消費者**のみ:
標的面の攻撃元内訳 (_victim_cyber_face) / 脅威スナップショットの regex 経路
(threat_operations) / STIX export (article_ops) / PIR 上位アクター (pir/evaluator)。

subject-gate = 評価済み行 (subject_actor_source 非 NULL) は **主題メンバーシップ** に絞り、
legacy 行 (主題層 07-17 稼働前) は mention に fallback する。membership は
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


def subject_membership_clause(subject_ids_col: str, param_count: int) -> str:
    """OR 連結の literal-id membership SQL 断片を返す (INSTR position ベース、両端カンマ挟み)。

    ``param_count`` 個の ``INSTR(',' || COALESCE({subject_ids_col},'') || ',',
    ',' || ? || ',') > 0`` を OR で連結し ``(...)`` で括って返す (呼出側は params に候補 id を
    宣言順で渡す契約)。:func:`subject_gate_clause` と同じ position ベース判定 — LIKE は
    id 中の ``_`` がワイルドカードになる罠、psycopg が ``%,`` を placeholder と誤解する罠の
    両方があるため使わない (INSTR は translate_sql が PG で STRPOS に変換する)。

    ``param_count=0`` は呼出側の空 guard 漏れ (候補なしで OR 句を作ろうとしている) なので
    ValueError で早期検出する — 呼出側は空リストなら本関数を呼ばずに別処理へ分岐する契約。
    """
    if param_count <= 0:
        raise ValueError("param_count must be >= 1 (caller must guard an empty candidate list)")
    membership = " OR ".join(
        f"INSTR(',' || COALESCE({subject_ids_col},'') || ',', ',' || ? || ',') > 0"
        for _ in range(param_count)
    )
    return f"({membership})"


def split_subject_ids(subject_ids_csv: str | None) -> frozenset[str]:
    """``subject_actor_ids`` (comma 連結 canonical id 列) を id 集合に分解する SSoT。

    writer (persistence/reprocess) は ``",".join(canonical_ids)`` で空白なしに書く。
    読み手が site ごとに split を再実装して strip 有無が揺れていたため一本化 (2026-08-20)。
    """
    return frozenset(s for s in (subject_ids_csv or "").split(",") if s)


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
    return mention_id in split_subject_ids(subject_ids_csv)
