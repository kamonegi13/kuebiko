"""焦点の遡及再現 (``scripts/focus_replay.py``) の純粋な部品 (2026-10-24 F0)。

設計書の付録の手順: 統合先は統合の時点より前に入っていたメンバーだけ、吸収側は全メンバーで
焦点を作り、照合の 4 値と正解ラベルを突き合わせる。DB を読む部分は script 側、数える部分はここ。
"""

from __future__ import annotations

import statistics
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from src.eventnews.identity.focus import (
    CLASS_CONFLICT,
    CLASS_LINEAGE,
    CLASS_SUPPORT,
    CLASS_WEAK,
    Focus,
)

CLASSES: tuple[str, ...] = (CLASS_SUPPORT, CLASS_WEAK, CLASS_CONFLICT, CLASS_LINEAGE)
#: F2 で結ばない値 (F1b: lineage は止めず、関係の候補へ回す)
STOPPING_CLASSES: frozenset[str] = frozenset({CLASS_CONFLICT})
#: 結ぶ判断では止めず、関係 (系統) の候補として渡す値
RELATION_CANDIDATE_CLASSES: frozenset[str] = frozenset({CLASS_LINEAGE})
LABEL_SAME = "same"


def members_before(rows: Sequence[tuple[str, str]], cutoff_iso: str) -> list[tuple[str, str]]:
    """(article_id, joined_at) のうち、``cutoff_iso`` より前に入っていた行 (ISO 文字列の辞書順)。"""
    return [(aid, joined) for aid, joined in rows if joined < cutoff_iso]


def final_label(row: Mapping[str, Any]) -> str:
    """正解ラベル。Opus 1 回目を採り、2 回目があって食い違うときは行の ``label`` に戻す。

    暫定の Sonnet 判定 (``label``) より Opus を優先する。Opus が無い行は ``label`` のまま。
    """
    first = row.get("opus1")
    second = row.get("opus2")
    label1 = str(first["label"]) if isinstance(first, Mapping) and first.get("label") else ""
    label2 = str(second["label"]) if isinstance(second, Mapping) and second.get("label") else ""
    if label1 and (not label2 or label1 == label2):
        return label1
    return str(row.get("label", ""))


@dataclass(frozen=True)
class LabelTally:
    """正解つきの組での、照合の結果の集計。"""

    errors: int
    errors_stopped: int
    correct: int
    correct_stopped: int
    by_class: Mapping[str, Mapping[str, int]]

    def as_lines(self) -> list[str]:
        lines = [
            f"誤り (same 以外) {self.errors} 組のうち止まる (conflict) {self.errors_stopped}",
            f"正しい統合 (same) {self.correct} 組のうち止まる {self.correct_stopped}",
        ]
        for klass in CLASSES:
            counts = self.by_class.get(klass, {})
            lines.append(
                f"  {klass:9s} same={counts.get(LABEL_SAME, 0)} other={counts.get('other', 0)}"
            )
        return lines


def tally_labeled(results: Sequence[tuple[str, str]]) -> LabelTally:
    """(照合の 4 値, ラベル) の列を数える。ラベルは same かそれ以外 (誤り) に畳む。"""
    by_class: dict[str, Counter[str]] = {}
    errors = errors_stopped = correct = correct_stopped = 0
    for klass, label in results:
        key = LABEL_SAME if label == LABEL_SAME else "other"
        by_class.setdefault(klass, Counter())[key] += 1
        stopped = klass in STOPPING_CLASSES
        if key == LABEL_SAME:
            correct += 1
            correct_stopped += int(stopped)
        else:
            errors += 1
            errors_stopped += int(stopped)
    return LabelTally(
        errors, errors_stopped, correct, correct_stopped, {k: dict(v) for k, v in by_class.items()}
    )


def class_shares(classes: Sequence[str]) -> dict[str, tuple[int, float]]:
    """照合の 4 値ごとの (件数, 割合)。"""
    counts = Counter(classes)
    total = len(classes)
    return {k: (counts.get(k, 0), counts.get(k, 0) / total if total else 0.0) for k in CLASSES}


def size_stats(values: Sequence[int]) -> tuple[float, float, int]:
    """(中央値, 90 パーセンタイル, 最大)。空は (0, 0, 0)。"""
    if not values:
        return 0.0, 0.0, 0
    ordered = sorted(values)
    p90 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.9))]
    return float(statistics.median(ordered)), float(p90), ordered[-1]


def focus_sizes(focuses: Sequence[Focus]) -> list[int]:
    return [len(f.names) for f in focuses]
