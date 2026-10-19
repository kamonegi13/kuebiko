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
        f
        for f in (normalize_for_match(p) for p in _ELLIPSIS_RE.split(excerpt))
        if len(f) >= _MIN_FRAGMENT_CHARS
    ]
    if not fragments:
        fragments = [normalized]

    return any(all(f in hay for f in fragments) for hay in haystacks)


#: 一致率の計算に使う抜粋 / 本文の文字数の上限 (正規化後。計算量を本文の長さに対し線形に抑える)
MATCH_EXCERPT_CAP = 300
MATCH_BODY_CAP = 100_000
_NGRAM = 3
#: 1 つの n-gram について数える本文内の出現位置の上限 (頻出語での膨張を防ぐ)
_MAX_POSITIONS_PER_NGRAM = 20
#: 「ほぼ一致」とみなす一致率と、同じ箇所とみなす斜めのずれの幅
_NEAR_MATCH_RATIO = 0.9
_SAME_SPOT_DIAGONAL_GAP = 8


def _diagonal_votes(needle: str, hay: str) -> dict[int, int]:
    """needle の n-gram が hay のどこに在るか数え、(本文位置 - 抜粋位置) ごとの票にする。"""
    positions: dict[str, list[int]] = {}
    for i in range(len(needle) - _NGRAM + 1):
        positions.setdefault(needle[i : i + _NGRAM], [])
    if not positions:
        return {}
    for p in range(len(hay) - _NGRAM + 1):
        gram = hay[p : p + _NGRAM]
        bucket = positions.get(gram)
        if bucket is not None and len(bucket) < _MAX_POSITIONS_PER_NGRAM:
            bucket.append(p)
    votes: dict[int, int] = {}
    for i in range(len(needle) - _NGRAM + 1):
        for p in positions[needle[i : i + _NGRAM]]:
            votes[p - i] = votes.get(p - i, 0) + 1
    return votes


def excerpt_match_stats(excerpt: str, *bodies: str) -> tuple[float, bool]:
    """逐語の照合に落ちた抜粋の近さ: (最良一致率 0〜1, 9 割以上一致の箇所が 1 か所だけか)。

    一致率 = 抜粋の連続した 3-gram のうち、同じ位置関係 (斜め) で本文に在る割合の最大値。
    置換には強く、挿入・削除には弱い近似で、計算量は本文の長さに線形。
    ``unique`` は 9 割以上一致する箇所が全本文を通して **ちょうど 1 か所** のとき True。
    抜粋が短すぎる / 本文が空なら (0.0, False)。
    """
    needle = normalize_for_match(excerpt)[:MATCH_EXCERPT_CAP]
    total = len(needle) - _NGRAM + 1
    if total <= 0:
        return 0.0, False
    best = 0
    near_spots = 0
    for body in bodies:
        hay = normalize_for_match(body)[:MATCH_BODY_CAP]
        votes = _diagonal_votes(needle, hay)
        if not votes:
            continue
        best = max(best, max(votes.values()))
        near = sorted(d for d, v in votes.items() if v / total >= _NEAR_MATCH_RATIO)
        prev: int | None = None
        for d in near:
            if prev is None or d - prev > _SAME_SPOT_DIAGONAL_GAP:
                near_spots += 1
            prev = d
    return round(best / total, 4), near_spots == 1
