"""運用者定義キーワードの照合ヘルパ (語境界つき) の SSoT。

監査 2026-07-05 P3 で PIR keyword に導入した語境界照合 (`OT`→not/protocol、
`ICS`→statistics 型の偽陽性防止) を、match_lists (有機的結合監査 M4: 同じ誤爆を
運用者定義キーワードで再現していた) と共有するために切り出したもの。

規約: ASCII keyword は語境界 regex、非 ASCII (日本語) は substring。
境界は keyword の端が英数字の側にのみ課す (`ED-` のような記号終わり略語は
右側に英数字が続くのが正常 [ed-26-01] なので右境界を課さない)。
"""

from __future__ import annotations

import re
from functools import lru_cache


@lru_cache(maxsize=1024)
def keyword_pattern(kw_lower: str) -> re.Pattern[str] | None:
    """ASCII keyword は語境界 regex を返す。非 ASCII (日本語) は None = substring 照合。"""
    if not kw_lower.isascii():
        return None
    prefix = r"(?<![a-z0-9])" if kw_lower[0].isalnum() else ""
    suffix = r"(?![a-z0-9])" if kw_lower[-1].isalnum() else ""
    return re.compile(prefix + re.escape(kw_lower) + suffix)


def keyword_in_text(kw: str, text_lower: str) -> bool:
    """keyword 1 語のマッチ判定 (ASCII=語境界 / 日本語=substring)。text は小文字化済み前提。"""
    kw_lower = kw.lower()
    pattern = keyword_pattern(kw_lower)
    if pattern is None:
        return kw_lower in text_lower
    # 語境界パターンは「その語そのもの」の前後に制約を足しただけなので、語が 1 度も
    # 現れない text は regex でも必ず不一致になる。先に C 実装の部分一致で切ると
    # 大半の候補をここで落とせる (PIR 一覧は 21 PIR × 数千記事の全文を走査するため
    # 効く)。判定結果は変わらない。
    if kw_lower not in text_lower:
        return False
    return pattern.search(text_lower) is not None
