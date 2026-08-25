"""生成された facts が原文のどこまで届いたかを測る (観測層)。

**関門ではない**。足りない事実を自動で書き足すことはしない (それは創作になる) —
早期打ち切りを**観測できるようにして**、プロンプト側で直せるようにするための計測。

2026-08-26 の実測: 単独報 2 件が原文の 27% / 57% 地点で止まり、後半にあった
被害規模 (「2,000 件超流出」「1,000 万件」) を落とした上、それを unknowns に
「不明」と書いていた (読み手へ出典と矛盾したことを伝えるので、単なる省略より悪い)。
複数報道は 73% / 89% まで届いていた — 出典間の相違を書かせる仕組みが全出典の
走査を強制するためで、単独報にはその力が働かない、という構造差だった。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from src.assessment.evidence_verify import normalize_for_match
from src.eventnews.models import FactItem

# 照合断片の長さ。短すぎると助詞だけで誤一致し、長すぎると言い換えで当たらない。
_NGRAM = 8
# 断片を取る間隔 (1 fact あたり数十回の find で足りる)。
_STRIDE = 3
# これ未満の原文は「最後まで読めた」扱いにする (測る意味がない)。
MIN_BODY_CHARS = 1200
# 最も深く読めた出典がここに届かなければ警告する。
WARN_BELOW = 0.6


def _deepest_hit(fact_text: str, body: str) -> float | None:
    """``fact_text`` の断片が ``body`` のどこまで現れるかを 0.0-1.0 で返す。

    言い換え・要約で一致断片が 1 つも無ければ ``None`` (**検証不能を
    「届いていない」と読まない**)。
    """
    needle = normalize_for_match(fact_text)
    if len(needle) < _NGRAM or not body:
        return None
    deepest = -1
    for start in range(0, len(needle) - _NGRAM + 1, _STRIDE):
        found = body.find(needle[start : start + _NGRAM])
        if found > deepest:
            deepest = found
    if deepest < 0:
        return None
    return deepest / len(body)


def deepest_coverage(
    facts: Sequence[FactItem],
    member_bodies: Mapping[int, str],
) -> float | None:
    """facts が到達した最深位置 (出典をまたいだ最大値)。

    ``member_bodies`` は ``source_index`` (1 始まり) → 原文本文。測定対象は
    ``MIN_BODY_CHARS`` 以上の原文のみ。一致が 1 件も取れなければ ``None``。

    出典ごとではなく**全体の最大**を見る: 複数報道では後発記事が先発の焼き直しで
    浅くしか使われないことが正当にあり、出典ごとに閾値を課すと警告が濁る。
    見たいのは「どの出典も深く読めていない」状態。
    """
    bodies = {
        index: normalized
        for index, text in member_bodies.items()
        if len(normalized := normalize_for_match(text)) >= MIN_BODY_CHARS
    }
    if not bodies:
        return None
    hits = [
        hit
        for fact in facts
        if (body := bodies.get(fact.source_index)) is not None
        and (hit := _deepest_hit(fact.text, body)) is not None
    ]
    if not hits:
        return None
    return max(hits)
