"""GraphRAG 共通取得 — 事象どうしの線をたどって候補の外の関連事象を集める (2026-10-08)。

Spotlight 専用だった ``src/spotlight/graph_context.py`` を一般化したもの。状況総括・
事象ニュース・PIR 別の要点など、どの生成経路からも「記事 id 群 (または事象 id 群) →
線の節テキスト」を取得できる。

- :mod:`src.graph.render` — 純粋関数 (線の節テキストを組む)。DB に触らない
- :mod:`src.graph.context` — DB から線・事象の付帯情報を読んで render に渡す
"""

from __future__ import annotations

from src.graph.context import build_relation_context
from src.graph.render import (
    MAX_LINES,
    EventContext,
    PriorityHint,
    render_relation_section,
)

__all__ = [
    "MAX_LINES",
    "EventContext",
    "PriorityHint",
    "build_relation_context",
    "render_relation_section",
]
