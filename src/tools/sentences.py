"""文の区切りの SSoT (2026-11-20、11-19 監査 D1 の点検: 区切りが関門ごとに別々だった)。

2 種類あり、用途で使い分ける。ここ以外で文の区切りを定義しない。

- ``SENTENCE_PATTERN`` (**数える・比べる**用): 区切り記号 (。！？.!? と改行) までの塊。
  終端の記号は文に含める (改行は含めない)。繰り返しの関門 (要約・Spotlight)・メタ文の関門が使う。
  英語の ``.`` でも区切るので ``1.5`` の途中でも割れるが、同じ規則で数え比べる限り害は無い。
- ``CUT_ENDS`` (**切る**用): 縮めるときに切ってよい文末。英語の ``.`` は入れない
  (``v2.0``・``1.5`` の途中で切ると読み手に壊れた数字が届く)。``text_budget`` が使う。
"""

from __future__ import annotations

import re

SENTENCE_PATTERN = re.compile(r"[^。！？.!?\n]+(?:[。！？.!?]+|(?=\n)|$)")
CUT_ENDS = ("。", "！", "？", "!", "?")


def last_cut_position(text: str) -> int:
    """``text`` の中で最後に切ってよい文末の位置 (無ければ -1)。"""
    return max(text.rfind(end) for end in CUT_ENDS)


__all__ = ["CUT_ENDS", "SENTENCE_PATTERN", "last_cut_position"]
