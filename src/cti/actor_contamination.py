"""アクター単位の汚染の見張り — 言及が地政学の記事に偏るアクター (2026-09-27)。

主題判定の平均精度 (Opus 盲検 92%) は、特定のアクターに集中した誤りを隠す。誤りはランダムでなく
**別名が一般語と衝突するアクター** に集中する (APT37 は主題 26 記事中 17 記事が無人機の記事)。
言及がサイバー以外の記事に偏るアクターを週次の確認候補に出す。自動では直さない
(影響工作のアクターは本来地政学の記事に多い — 確認して辞書を直すか、除外に足す)。
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

#: 地政学の記事に付いた言及の割合がこれ以上で候補
SKEW_MIN_SHARE = 0.5
#: 標本の下限 (少数の言及では判定しない)
SKEW_MIN_MENTIONS = 8
_MAX_LINES = 6
_NON_CYBER_CATEGORIES = frozenset({"geopolitical"})


@dataclass(frozen=True)
class ActorSkew:
    actor: str
    total: int
    geo: int
    examples: tuple[str, ...]

    @property
    def share(self) -> float:
        return self.geo / self.total if self.total else 0.0


def find_skewed_actors(
    rows: Iterable[tuple[str, str, str]], *, exclude: frozenset[str]
) -> list[ActorSkew]:
    """(actor, category, title) の行から、地政学の記事に偏るアクターを返す (割合の大きい順)。"""
    total: dict[str, int] = defaultdict(int)
    geo: dict[str, list[str]] = defaultdict(list)
    for actor, category, title in rows:
        if actor in exclude:
            continue
        total[actor] += 1
        if category in _NON_CYBER_CATEGORIES:
            geo[actor].append(title)
    out = [
        ActorSkew(a, n, len(geo[a]), tuple(geo[a][:2]))
        for a, n in total.items()
        if n >= SKEW_MIN_MENTIONS and len(geo[a]) / n >= SKEW_MIN_SHARE
    ]
    return sorted(out, key=lambda s: (-s.share, -s.total))


def skew_lines(skews: list[ActorSkew]) -> tuple[list[str], bool]:
    """週次監査の行と、警告するか。"""
    if not skews:
        return ["アクターの偏り: なし"], False
    lines = [
        f"⚠️ アクターの偏り: {s.actor} の言及 {s.total} 件中 {s.geo} 件 ({s.share:.0%})"
        " が地政学の記事"
        f" — 別名の衝突を確認 (例: {s.examples[0][:40] if s.examples else '-'})"
        for s in skews[:_MAX_LINES]
    ]
    return lines, True


def contamination_exclusions() -> frozenset[str]:
    """本来地政学の記事に多いアクター (ハクティビスト系・影響工作) — 候補から除く。"""
    from src.cti.actor_normalizer import load_actor_aliases

    return frozenset(
        a.id for a in load_actor_aliases().actors if a.family == "hacktivist" or a.kind != "group"
    )
