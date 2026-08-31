"""生成本文が原文の表現をどれだけそのまま含むかを測り、超えたら止める関門。

事実に著作権は無いが**表現**にはある。要約を名乗る以上、原文の文をなぞった行は
出せない。

実測 (2026-08-26、公開 53 件): 20 字以上の連続一致が本文に占める割合は
**原文が日本語のみ 42.4% (中央値・最大 61.6%) / 混在 17.1% / 翻訳のみ 0.0%
(最大 9.8%)** と 20 倍以上違った。翻訳が表現を壊すためで、**日本語ソースには
壊す層が無い**。最悪の 1 件は要約ではなく語尾だけ変えた転記だった:

    原文 「…閲覧可能な状態にあったことが判明しました。」
    生成 「…閲覧可能な状態にあったことが判明したと報じられている。」

閾値は上の実測から置く (翻訳のみの最大 9.8% と混在の最大 26.7% は通し、
日本語のみの 3 件 38-62% を止める)。指示では止まらないので決定論で測る。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from src.assessment.evidence_verify import normalize_for_match
from src.eventnews.models import FactItem

# これ以上の連続一致を「表現の一致」と数える。固有名詞や定型句は 20 字に届かない。
MIN_RUN = 20
# 本文全体でこの割合を超えたら不可。
ARTICLE_MAX_RATIO = 0.30
# 1 行がほぼ丸ごと一致していれば、本文全体が低くても不可 (転記した 1 文を隠さない)。
LINE_MAX_RATIO = 0.80
# 短い行は固有名詞や数値の並びで一致しやすいので、行単位の判定からは外す。
LINE_MIN_CHARS = 50
# 二分探索の上限 (これ以上長い一致は測っても判定が変わらない)。
_MAX_RUN = 400


def _longest_run_at(text: str, start: int, body: str) -> int:
    """``text[start:]`` の先頭が ``body`` に含まれる最長の長さ。"""
    low, high = 0, min(len(text) - start, _MAX_RUN)
    while low < high:
        mid = (low + high + 1) // 2
        if text[start : start + mid] in body:
            low = mid
        else:
            high = mid - 1
    return low


def overlap_ratio(text: str, body: str) -> float:
    """``text`` のうち ``body`` と ``MIN_RUN`` 字以上連続一致する部分の割合。

    字体差を吸収してから測る (``normalize_for_match``) — 全角/半角や引用符の
    違いで一致を見逃すと、関門が素通りする。
    """
    needle = normalize_for_match(text)
    haystack = normalize_for_match(body)
    if not needle or not haystack:
        return 0.0
    covered = 0
    index = 0
    while index < len(needle):
        run = _longest_run_at(needle, index, haystack)
        if run >= MIN_RUN:
            covered += run
            index += run
        else:
            index += 1
    return covered / len(needle)


def _body_for(fact: FactItem, bodies: Mapping[int, str]) -> str:
    return bodies.get(fact.source_index, "")


def article_ratio(facts: Sequence[FactItem], bodies: Mapping[int, str]) -> float:
    """本文全体の一致割合 (行を連結した上での比率)。"""
    total = matched = 0
    for fact in facts:
        body = _body_for(fact, bodies)
        if not body:
            continue
        normalized = normalize_for_match(fact.text)
        total += len(normalized)
        matched += round(overlap_ratio(fact.text, body) * len(normalized))
    if not total:
        return 0.0
    return matched / total


def transcribed_lines(facts: Sequence[FactItem], bodies: Mapping[int, str]) -> tuple[int, ...]:
    """ほぼ丸ごと原文と一致している行の位置 (0 始まり)。**落とす行はこれだけ**。

    ⚠ 全体比 (``article_ratio``) を落とす条件に使わない。実測 (2026-08-26) で、
    書き直し後に残る一致は表現ではなく事実の羅列だった::

        「costexplorerを確認できた1059キーの範囲では2026年7月に50アカウントが
          1000ドル超9アカウントが1万ドル超を利用し…合計42万631ドル」

    数値とその関係だけで構成されており、避けるには事実を歪めるしかない。句読点を
    落として正規化するため、数値の列挙が長い「一致」として現れる —
    **全体比は事実密度を測ってしまう**。表現の借用を測れるのは行単位の方で、
    書き直しにより丸写しの行は 0 になった (61.6% の記事で 6 行 → 0 行)。
    """
    found: list[int] = []
    for index, fact in enumerate(facts):
        body = _body_for(fact, bodies)
        if not body or len(normalize_for_match(fact.text)) < LINE_MIN_CHARS:
            continue
        if overlap_ratio(fact.text, body) >= LINE_MAX_RATIO:
            found.append(index)
    return tuple(found)


def needs_rewrite(facts: Sequence[FactItem], bodies: Mapping[int, str]) -> bool:
    """書き直しをさせるか。全体比が高い場合も含めて広めに拾う。"""
    if not facts:
        return False
    return article_ratio(facts, bodies) > ARTICLE_MAX_RATIO or bool(
        transcribed_lines(facts, bodies)
    )
