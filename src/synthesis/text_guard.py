"""生成文の反復暴走を畳む関門 (2026-09-25)。

2026-09-25 の夕ブリーフで、状況総括の「比重」節に「新規開設 (detect-new) / 」が 286 回連続した
(n17c、出力 6,493 トークン)。節の文字数上限 (``render._SECTION_MAX_CHARS``) は膨張を止めるが、
**上限の内側で同じ句を繰り返す暴走は止められない** (切れた位置で「新規設」と途切れていた)。
暴走は decoding 側の現象で指示では止まらない (2026-09-20) ため、書込の seam で決定論に畳む。

同じ断片 (4-80 字) が **連続で 4 回以上**並んだら 1 回に畳む。正常な文章で同じ句が 4 回連続する
ことは無く (列挙でも区切りの中身が変わる)、誤って畳む危険は小さい。
"""

from __future__ import annotations

import re

#: 連続の閾値 (この回数以上並んだら暴走とみなす)。3 回までは列挙の偶然一致を許す
MIN_REPEATS = 4
_RUN = re.compile(r"(.{4,80}?)\1{" + str(MIN_REPEATS - 1) + r",}", re.DOTALL)


def collapse_repetition(text: str) -> tuple[str, int]:
    """連続反復を 1 回に畳む。返り値 = (畳んだ文, 畳んだ箇所の数)。"""
    if not text:
        return text, 0
    count = 0

    def _one(m: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return m.group(1)

    return _RUN.sub(_one, text), count
