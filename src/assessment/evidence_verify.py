"""証拠の引用実在検査 (2026-08-22)。

synthesis の証拠は「どの記事か」(article_id) と「その記事の何が根拠か」(excerpt) の
2 要素。**どちらも実在検査が無く**、実測で記事参照の 3.14% が実在しない記事を指し、
引用の 59.9% が本文 (原文 + 日本語訳) に存在しない要約・言い換えだった。

プロンプトは以前から「要約でなく該当箇所」と明示しており、**指示では止まっていない**
(deterministic_gates_over_instructions 2026-08-19 と同型)。したがって書込 seam に
決定論の関門を置く — 本モジュールはその判定器 (純関数、DB 非依存)。

CTI として「証拠が無い」より「たどれない証拠を示す」ほうが悪い: 証拠件数と出典 tier が
裏取り済みに見えるうえ、証拠状態カウントは台帳の一級データとして下流に流れる。
"""

from __future__ import annotations

import re
import unicodedata

# これ未満の引用は偶然一致しうるため証拠として検証しない (落とす)
_MIN_EXCERPT_CHARS = 12
# 省略記号で繋いだ引用は各断片がすべて本文に在ることを要求する
_ELLIPSIS_RE = re.compile(r"\.{3}|…|…")
# 断片として意味を持つ最小長 (これ未満の断片は照合対象から外す)
_MIN_FRAGMENT_CHARS = 8
# 空白 + 引用符/ダッシュ/句読点の字体差。LLM は原文を写す際にこれらを揺らす
# (実測: 非逐語と判定された引用の 2.7% は 'became aware of…' のような引用符差のみ)。
# 逐語判定では落として比較する — 12 字以上の文字列が偶然一致することはまず無く、
# 言い換えは記号を落としても一致しない (実測で残り 57.2% は本文に真に不在)。
_NOISE_RE = re.compile(
    r"[\s\u2018\u2019\u201c\u201d'\"`\u2010-\u2015\-\u2026.,:;!?()\[\]{}\u3001\u3002\u300c\u300d\u300e\u300f]+"
)


def normalize_for_match(text: str) -> str:
    """字体差を吸収する。全角/半角 (NFKC)・大小・空白・引用符/ダッシュ/句読点を落とす。

    「原文をそのまま写したか」を見るのが目的であり、表記の揺れで正当な引用を
    落とすのは台帳の精度を下げる (偽陰性も損失)。
    """
    return _NOISE_RE.sub("", unicodedata.normalize("NFKC", text or "").casefold())


def excerpt_is_supported(excerpt: str, *bodies: str) -> bool:
    """``excerpt`` が ``bodies`` のいずれかに逐語で含まれるか。

    ``bodies`` には原文本文と日本語訳を並べて渡す (訳文から引用される場合を救う)。
    省略記号を含む引用は断片に分け、**すべての断片**が同一の haystack に在ることを
    要求する (断片を別々の本文から拾う「継ぎ接ぎ」を許さない)。

    判定できない場合 (引用が短すぎる / 本文が空) は ``False`` — 検証不能を
    「支持された」と読まないための保守則。呼出側は本文の有無を別途区別すること。
    """
    normalized = normalize_for_match(excerpt)
    if len(normalized) < _MIN_EXCERPT_CHARS:
        return False

    haystacks = [normalize_for_match(b) for b in bodies if b]
    if not any(haystacks):
        return False

    fragments = [
        f for f in (normalize_for_match(p) for p in _ELLIPSIS_RE.split(excerpt))
        if len(f) >= _MIN_FRAGMENT_CHARS
    ]
    if not fragments:
        fragments = [normalized]

    return any(all(f in hay for f in fragments) for hay in haystacks)
