"""事象ニュース draft の尾部欄 (相違点・注意点・未解明点) の全空関門 (2026-09-13)。

背景: レシピ是正した SFT モデルは 3 欄がすべて空の item を 3/39 出す (教師 0/39・N1 0/39)。
CTI の読み物として「注意点が丸ごと無い」は「短い」より悪い。欄ごとの下限は付けない
(教師でも個別の欄が空なのは普通: caveats 0 が 2/39、discrepancies 0 が 5/39)。
spotlight の tail_gate と同じ思想: 指示でなく関門、同一プロンプトで再サンプルし最良候補を採る。
旗 ``EVENTNEWS_TAIL_GATE=0`` で無効。
"""

from __future__ import annotations

import os

from src.eventnews.models import EventNewsDraft

GATE_FLAG_ENV = "EVENTNEWS_TAIL_GATE"
TAIL_FIELDS: tuple[str, ...] = ("discrepancies", "caveats", "unknowns")
MAX_RESAMPLES = 2


def gate_enabled() -> bool:
    """関門の有効判定 (既定 ON)。``EVENTNEWS_TAIL_GATE=0`` で無効。"""
    return os.environ.get(GATE_FLAG_ENV, "1").strip() not in ("0", "false", "False")


def tail_score(draft: EventNewsDraft) -> int:
    """尾部 3 欄の件数の和 (最良候補の選択にのみ使う)。"""
    return sum(len(getattr(draft, f)) for f in TAIL_FIELDS)


def tail_all_empty(draft: EventNewsDraft) -> bool:
    return tail_score(draft) == 0
