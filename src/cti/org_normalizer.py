"""組織名の照合キー (victim_org の表記ゆれを畳む SSoT、2026-10-12)。

同じ組織が「株式会社X」「X」「X Co., Ltd.」「Xグループ」「Xホールディングス」のように
別の文字列で抽出されると、群化・重複判定が entity を共有できず、さらに
``blocked_by_different_victims`` が「被害者が食い違う別事象」と誤って塞ぐ
(実測 10-12: alert の重複で群化から漏れた 32 件のうち 15 件が表記ゆれ)。

⚠ ここが畳むのは**法人格・組織種別の付け外しだけ**。別名 (略称 ↔ 正式名・日本語 ↔ 英語・
旧社名) は辞書が要るので扱わない。過剰に畳むと別組織が同一視される (群化の破壊) ため、
畳む語は法人格と末尾の総称 (Group / Holdings / グループ) に限る。
"""

from __future__ import annotations

import re
import unicodedata

# 法人格 (日本語)。部分文字列として現れても組織名の一部にならない語だけ。
_JP_LEGAL = re.compile(
    "株式会社|\\(株\\)|有限会社|\\(有\\)|合同会社|合資会社|一般社団法人|一般財団法人|"
    "公益社団法人|公益財団法人|学校法人|社会福祉法人|医療法人|特定非営利活動法人|国立大学法人"
)
# 法人格 (英字)。語境界つき — ``corp`` が ``corporate`` を壊さない。
_EN_LEGAL = re.compile(
    r"(?<![a-z0-9\-])(?:inc|incorporated|corp|corporation|co|company|ltd|limited|llc|llp|"
    r"pllc|plc|gmbh|ag|sa|nv|bv|pty|pte|kk|oyj|ab|as|spa|srl)(?![a-z0-9\-])"
)
# 末尾の総称。先頭や途中の "group" は固有名の一部のことがあるので末尾だけ。
_TRAILING_GENERIC = re.compile(r"(?:holdings?|group|hd|ホールディングス|グループ)$")
# 末尾のドメイン表記 ("advantech.com") と括弧書きの略称 ("Foo (DIVD)")。同じ組織の別表記。
_DOMAIN_SUFFIX = re.compile(r"\.(?:com|net|org|io|co|jp|ch|br|de|uk|fr|it|es|nl)(?:\.[a-z]{2})?$")
_PAREN = re.compile(r"\([^)]*\)")
_MIN_KEY_LEN = 2


def _squash(text: str) -> str:
    return "".join(ch for ch in text if ch.isalnum())


def org_match_key(name: str) -> str:
    """表記ゆれを畳んだ照合キー。畳んだ結果が短すぎるときは畳む前の正規化形を返す。"""
    folded = unicodedata.normalize("NFKC", name or "").casefold()
    plain = _squash(folded)
    stripped = _EN_LEGAL.sub(
        " ", _PAREN.sub(" ", _JP_LEGAL.sub(" ", _DOMAIN_SUFFIX.sub("", folded)))
    )
    core = _squash(stripped)
    # 末尾の総称は、残りが十分に長いときだけ外す ("Group" 単独や "Xグループ" の X が 1 字を避ける)
    while True:
        trimmed = _TRAILING_GENERIC.sub("", core)
        if trimmed == core or len(trimmed) < _MIN_KEY_LEN:
            break
        core = trimmed
    return core if len(core) >= _MIN_KEY_LEN else plain
