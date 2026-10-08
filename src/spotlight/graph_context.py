"""Spotlight の「線でたどった関連事象」の節 — GraphRAG の取得 (2026-09-29)。

候補記事が属する事象から、事象どうしの線 (``eventnews.relations`` — 本番の関係表示と同じ導出)
をたどり、**候補の外** の関連事象を見出し・日付・共有した指標・出典の記事番号つきで渡す。

実測 (scripts/graphrag_spotlight_compare.py、凍結 15 窓・Opus・盲検): 線つきが 12:3 で勝ち、
根拠の観点は悪化しなかった (8:5)。1 窓の候補 30 件はほぼ別々の事象で、候補どうしの線は
ほとんど無い — 効くのは候補の外へたどる取得の形。

⭐ 2026-10-08: 取得・render の実体は :mod:`src.graph` (Spotlight 以外の生成経路も使う共通
モジュール) へ移した。このファイルは Spotlight 向けの**互換レイヤ**として残す
(``graph_context_enabled`` / ``build_graph_context`` / ``render_graph_context`` /
``insert_after_candidates`` は呼び手を変えずに使える)。線 1 本の中身を厚くする設計
(相手事象の種別・被害業種/国・時間差・要点 1 文・共有指標の珍しさ・ハブ抑制) は
:mod:`src.graph.render` 側の実装を参照。

既定は off (``SPOTLIGHT_GRAPH_CONTEXT=1`` で on)。ローカルモデルが線を扱えるか確かめてから開く。
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any

from src.graph.render import MAX_LINES, render_relation_section
from src.graph.render import insert_after_candidates as insert_after_candidates

_FLAG_ENV = "SPOTLIGHT_GRAPH_CONTEXT"


def graph_context_enabled() -> bool:
    return os.environ.get(_FLAG_ENV, "0") == "1"


def render_graph_context(
    article_ids: Sequence[str],
    *,
    members: Mapping[str, Sequence[str]],
    relations: Mapping[str, Sequence[Any]],
    first_reported: Mapping[str, datetime],
    headlines: Mapping[str, str],
    window_end: datetime,
    actor_name: Callable[[str], str],
    max_lines: int = MAX_LINES,
) -> str:
    """線の節のテキスト (純粋関数、:func:`src.graph.render.render_relation_section` の薄い層)。

    Spotlight からの既存呼び出し (付帯情報を渡さない形) をそのまま受ける。線の中身を
    厚くした呼び出し (種別・被害業種/国・要点つき) をしたい呼び手は
    :func:`src.graph.context.build_relation_context` を直接使う。
    """
    return render_relation_section(
        article_ids,
        members=members,
        relations=relations,
        first_reported=first_reported,
        headlines=headlines,
        window_end=window_end,
        actor_name=actor_name,
        max_lines=max_lines,
    )


def build_graph_context(repo: Any, article_ids: Sequence[str], window_end: datetime) -> str:
    """DB から線・事象を読んで節を組む (失敗は呼び手が握る)。"""
    from src.graph.context import RELATION_DAYS, build_relation_context

    return build_relation_context(
        repo, article_ids=article_ids, window_end=window_end, relation_days=RELATION_DAYS
    )


__all__ = [
    "MAX_LINES",
    "build_graph_context",
    "graph_context_enabled",
    "insert_after_candidates",
    "render_graph_context",
]
