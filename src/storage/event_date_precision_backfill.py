"""既存行の event_date_precision を機械で埋める (2026-10-18)。

元の抽出文字列 (YYYY-MM など) は DB に残っていないため、event_date と basis から確実に言える
範囲だけを埋める。確実に言えない行は ``unknown`` (推測で day にしない)。

- basis=relative            -> relative
- 日が 01 以外               -> day  (月のみを 1 日に寄せた値ではありえない)
- 日が 01 (basis が relative 以外) -> unknown (本当の 1 日か寄せられた値か区別できない)
"""

from __future__ import annotations

from typing import Final

_DAY_OF = "substr(event_date, 9, 2)"

#: (precision, WHERE 句)。上から順に適用し、event_date_precision IS NULL の行だけを埋める。
BACKFILL_RULES: Final[tuple[tuple[str, str], ...]] = (
    ("relative", "event_date_basis = 'relative'"),
    ("day", f"{_DAY_OF} <> '01' AND length(event_date) = 10"),
    ("unknown", "event_date IS NOT NULL"),
)


def build_backfill_statements() -> tuple[str, ...]:
    """規則ごとの UPDATE 文 (パラメータなし)。適用順に並ぶ。"""
    return tuple(
        f"UPDATE articles SET event_date_precision = '{precision}' "
        f"WHERE event_date IS NOT NULL AND event_date <> '' "
        f"AND event_date_precision IS NULL AND {cond}"
        for precision, cond in BACKFILL_RULES
    )
