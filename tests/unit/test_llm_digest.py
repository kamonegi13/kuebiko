"""src.digest.llm_digest の出力予算スケーリング (2026-07-07)。

件数駆動 recap: 選定件数に応じ digest 出力予算を拡張し各記事の厚みを保つ。
"""

from __future__ import annotations

from src.digest.llm_digest import (
    _DIGEST_MAX_TOKENS_CEILING,
    _DIGEST_TOKENS_PER_ITEM,
    DIGEST_MAX_TOKENS,
    _digest_max_tokens,
)


def test_floor_for_few_items() -> None:
    # 少数件は従来同等の floor (最低保証)
    assert _digest_max_tokens(1) == DIGEST_MAX_TOKENS
    assert _digest_max_tokens(5) == DIGEST_MAX_TOKENS


def test_scales_with_item_count() -> None:
    # floor を超える件数では per-item ぶん拡張し厚みを保つ
    n = (DIGEST_MAX_TOKENS // _DIGEST_TOKENS_PER_ITEM) + 3
    assert _digest_max_tokens(n) == n * _DIGEST_TOKENS_PER_ITEM


def test_capped_at_ceiling() -> None:
    # 青天井にはせず ceiling で頭打ち (生成時間/可読性)
    assert _digest_max_tokens(1000) == _DIGEST_MAX_TOKENS_CEILING
