"""DETECT-REJUDGE の R18.2 (閉包の段) の純粋部分。

仕様は docs/replay_prereg/detect_decompose_rejudge.md §7。

R18.1 (前回) は最大成分が上限 0.30 を超えた。R18.2 は「ハブの値を鍵にしない」段を弱い方から順に
試し、最初に最大成分 < 0.30 を満たした段を使う。段は prereg で固定 (結果を見る前)。
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

R18_2_VERSION = "R18.2"
MAX_COMPONENT_SHARE = 0.30  # 最大成分の占有率はこれ未満 (prereg §7.2)
FOCUS_CVE_MAX = 3  # 1 記事が持つ値がこれを超える欄は鍵にしない (前回と同じ)
CANDIDATE_CONTAMINATION_STOP = 0.50  # 候補の汚染割合がこれを超えたら GPU 枠を使わない (§7.2)

_ID_TOKEN = re.compile(r"[A-Za-z0-9_\-]+:[^\s\"'\\<>\[\],]+")
_TRAILING = ".;)"


@dataclass(frozen=True)
class ClosureStep:
    """閉包の 1 段。``value_freq_cap`` は DB 全体で値を持つ記事数の上限 (None = 上限なし)。"""

    name: str
    value_freq_cap: int | None
    cos_threshold: float
    per_article_cap: int = FOCUS_CVE_MAX


#: prereg §7.2 の表 (弱い順)。変えるときは prereg の変更履歴に書く
LADDER: tuple[ClosureStep, ...] = (
    ClosureStep("L0", None, 0.80),
    ClosureStep("L1", 50, 0.80),
    ClosureStep("L2", 20, 0.80),
    ClosureStep("L3", 20, 0.85),
    ClosureStep("L4", 20, 0.90),
)


def id_tokens(text: str) -> set[str]:
    """テキストから記事 id らしい語 (DB で実在を確かめる前の候補。末尾の句読点は両方残す)。"""
    out: set[str] = set()
    for m in _ID_TOKEN.finditer(text):
        tok = m.group(0)
        out.add(tok)
        stripped = tok.rstrip(_TRAILING)
        if stripped != tok:
            out.add(stripped)
    return out


def value_frequencies(values_of: Mapping[str, Iterable[str]]) -> dict[str, int]:
    """値 → それを持つ記事の数 (記事ごとに 1 回)。"""
    freq: dict[str, int] = defaultdict(int)
    for vals in values_of.values():
        for v in set(vals):
            freq[v] += 1
    return dict(freq)


def keys_for_step(
    values_of: Mapping[str, frozenset[str]],
    freq: Mapping[str, int],
    step: ClosureStep,
    *,
    per_article: bool = True,
) -> dict[str, frozenset[str]]:
    """段の規則で鍵にする値だけを残す (記事ごとの上限 → 値の頻度の上限)。空は落とす。

    ``per_article=False`` は事象の根 (記事ごとの上限を当てない、前回と同じ)。
    """
    out: dict[str, frozenset[str]] = {}
    for a, vals in values_of.items():
        if per_article and len(vals) > step.per_article_cap:
            continue
        kept = frozenset(
            v for v in vals if step.value_freq_cap is None or freq.get(v, 0) <= step.value_freq_cap
        )
        if kept:
            out[a] = kept
    return out


def choose_step(
    shares: Sequence[tuple[ClosureStep, float]], max_share: float = MAX_COMPONENT_SHARE
) -> ClosureStep | None:
    """段の順に、最初に最大成分の占有率 < max_share の段。無ければ None (判定不能)。"""
    for step, share in shares:
        if share < max_share:
            return step
    return None


def contaminated_articles(
    component_of: Mapping[str, str], train_ids: Iterable[str], targets: Iterable[str]
) -> set[str]:
    """targets のうち、学習・調整側の記事と同じ成分に入る記事 (成分は割らない)。"""
    train_components = {component_of.get(a, a) for a in train_ids}
    return {a for a in targets if component_of.get(a, a) in train_components}


def unit_contaminated(
    article_ids: Iterable[str], contaminated: set[str], universe: set[str]
) -> bool:
    """単位の記事のどれかが汚染記事、または凍結の母集合の外 (保守側) なら True。"""
    return any(a in contaminated or a not in universe for a in article_ids)


def share_of(part: Iterable[str], whole: Sequence[str]) -> float | None:
    """whole のうち part に入る割合 (whole が空なら None)。"""
    if not whole:
        return None
    s = set(part)
    return sum(1 for a in whole if a in s) / len(whole)
