"""判定の手順 (prereg §3.1・§3.2)・多数決・合否の指標 (§6) の純関数と、教師の手順の駆動。

純関数部分は DB にも Opus にも触れない (単体テストの対象)。
"""

from __future__ import annotations

import math
import random
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from scripts.s26_imp.common import SEED_TEACHER, batch_items
from scripts.s26_imp.opus import JudgeFn, judge_items

Judgment = Mapping[str, Any]

SECOND_PASS_HIGH_CRITERIA = frozenset({2, 3, 5, 6, 8})
MEDIUM_CUES = ("単一組織", "分析", "国家系", "主張")
CONFIRMING_SOURCES = frozenset({"被害者", "当局", "独立研究者"})
MIN_CONFIRMED_ORGS = 2
GOLD_MIN_HIGH_IN_FRAME = 20
THRESHOLDS_A = {"A1": 0.95, "A2": 0.85, "A3": 0.90}
THRESHOLD_B1 = 0.90
THRESHOLD_B2_SHARE = 0.02
_HIGH_TOKEN = re.compile(r"(?<![\w-])high-(\d+)(?!\d)")


def is_high(label: str | None) -> bool:
    return label == "high"


def high_criteria_numbers(criterion: str) -> set[int]:
    """criterion に書かれた high-N の N の集合。"""
    return {int(m) for m in _HIGH_TOKEN.findall(criterion or "")}


def flags_consistency(j: Judgment, *, null_count_as_lt2: bool = True) -> tuple[str, ...]:
    """§3.2 の一貫性の検査 (決定論)。当たった検査の名前 ("i" / "ii")。

    ``null_count_as_lt2``: (ii) で ``confirmed_victim_orgs`` が null を「2 未満」に数えるか。
    2 回目の対象の選択 (§3.1) は実行どおり True のまま、§6-B の B2 の数え方は False
    (null = 数え切れない規模を含む「不明」で、矛盾に数えない。prereg §12、11-02 の結果の後の変更)。
    """
    out: list[str] = []
    orgs = j.get("confirmed_victim_orgs")
    by = j.get("confirmed_by")
    imp = j.get("importance")
    if (
        orgs is not None
        and orgs >= MIN_CONFIRMED_ORGS
        and by in CONFIRMING_SOURCES
        and not is_high(imp)
    ):
        out.append("i")
    if (
        is_high(imp)
        and 2 in high_criteria_numbers(str(j.get("criterion", "")))
        and (
            (orgs is None and null_count_as_lt2)
            or (orgs is not None and orgs < MIN_CONFIRMED_ORGS)
            or by == "主張のみ"
        )
    ):
        out.append("ii")
    return tuple(out)


def needs_second_pass(first: Judgment) -> bool:
    """§3.1: 2 回目を当てる行か。"""
    criterion = str(first.get("criterion", ""))
    if high_criteria_numbers(criterion) & SECOND_PASS_HIGH_CRITERIA:
        return True
    if first.get("importance") == "medium" and any(c in criterion for c in MEDIUM_CUES):
        return True
    return bool(flags_consistency(first))


def high_split(a: Judgment, b: Judgment) -> bool:
    return is_high(a.get("importance")) != is_high(b.get("importance"))


def needs_third(judgments: Sequence[Judgment], *, any_disagreement: bool) -> bool:
    """2 回の判定の後に 3 回目を当てるか。gold は high/非 high の割れ、教師は 3 値の割れ。"""
    if len(judgments) != 2:
        return False
    a, b = judgments
    if high_split(a, b):
        return True
    return any_disagreement and a.get("importance") != b.get("importance")


@dataclass(frozen=True)
class Vote:
    label: str
    boundary: bool


def majority_vote(judgments: Sequence[Judgment]) -> Vote:
    """最終のラベル (§3.1)。1 回 = その値、同じ値が 2 つ以上 = その値、1:1:1 = medium + boundary。

    2 回で食い違ったまま (3 回目が無い) の非 high 同士は medium。
    """
    labels = [str(j["importance"]) for j in judgments]
    if len(labels) == 1:
        return Vote(labels[0], False)
    top, n = Counter(labels).most_common(1)[0]
    if n >= 2:
        return Vote(top, False)
    return Vote("medium", len(labels) >= 3 or high_split(judgments[0], judgments[1]))


def gold_vote(judgments: Sequence[Judgment]) -> Vote:
    """gold:imp_precheck_s26 の正解。2 回で high/非 high が割れた記事は境界 (3 回目の多数決でラベルは付く)。"""
    boundary = len(judgments) >= 2 and high_split(judgments[0], judgments[1])
    return Vote(majority_vote(judgments).label, boundary)


def representative(judgments: Sequence[Judgment], label: str) -> Judgment:
    """最終ラベルと同じ importance の判定のうち最後のもの (§3.2 を多数決の後に当てる材料)。"""
    same = [j for j in judgments if j.get("importance") == label]
    return same[-1] if same else judgments[-1]


def shuffled(items: Sequence[dict[str, Any]], seed: int) -> list[dict[str, Any]]:
    out = list(items)
    random.Random(seed).shuffle(out)
    return out


@dataclass(frozen=True)
class TeacherRow:
    id: str
    judgments: tuple[dict[str, Any], ...]
    label: str
    boundary: bool
    flags_first: tuple[str, ...]
    flags_final: tuple[str, ...]


@dataclass
class TeacherResult:
    rows: dict[str, TeacherRow] = field(default_factory=dict)
    failed: list[str] = field(default_factory=list)
    pass_sizes: dict[int, int] = field(default_factory=dict)
    pass_groups: dict[int, int] = field(default_factory=dict)


async def _run_pass(
    items: Sequence[dict[str, Any]],
    pass_no: int,
    seed: int,
    judge: JudgeFn,
    result: TeacherResult,
    schema_attr: str = "items",
) -> dict[str, dict[str, Any]]:
    groups = batch_items(shuffled(items, seed))
    result.pass_sizes[pass_no] = len(items)
    result.pass_groups[pass_no] = len(groups)
    got, failed = await judge_items(groups, pass_no=pass_no, judge=judge, verdict_attr=schema_attr)
    result.failed.extend(failed)
    return got


async def run_teacher_protocol(
    items: Sequence[dict[str, Any]],
    judge: JudgeFn,
    *,
    seeds: Sequence[int] = SEED_TEACHER,
) -> TeacherResult:
    """教師の手順: 1 回目 (4 件ずつ) → 境の型・検査に当たる行だけ 2 回目 → 食い違いは 3 回目 (§3, §3.1)。"""
    result = TeacherResult()
    by_id = {it["id"]: it for it in items}
    first = await _run_pass(items, 1, seeds[0], judge, result)
    judgments: dict[str, list[dict[str, Any]]] = {i: [j] for i, j in first.items()}
    flags_first = {i: flags_consistency(j) for i, j in first.items()}

    second_ids = [i for i, j in first.items() if needs_second_pass(j)]
    if second_ids:
        second = await _run_pass([by_id[i] for i in second_ids], 2, seeds[1], judge, result)
        for i, j in second.items():
            judgments[i].append(j)

    third_ids = [
        i for i in second_ids if i in judgments and needs_third(judgments[i], any_disagreement=True)
    ]
    if third_ids:
        third = await _run_pass([by_id[i] for i in third_ids], 3, seeds[2], judge, result)
        for i, j in third.items():
            judgments[i].append(j)

    failed_set = set(result.failed)
    for i, js in judgments.items():
        if i in failed_set:
            continue
        v = majority_vote(js)
        rep = representative(js, v.label)
        result.rows[i] = TeacherRow(
            id=i,
            judgments=tuple(dict(j) for j in js),
            label=v.label,
            boundary=v.boundary,
            flags_first=flags_first[i],
            flags_final=flags_consistency(rep, null_count_as_lt2=False),
        )
    result.failed = sorted(failed_set | (set(by_id) - set(result.rows)))
    return result


def agreement(rows: Sequence[TeacherRow]) -> tuple[int, int]:
    """2 回目のある行の、1 回目と 2 回目の high/非 high の一致 (一致数, 行数)。"""
    pairs = [r for r in rows if len(r.judgments) >= 2]
    same = sum(not high_split(r.judgments[0], r.judgments[1]) for r in pairs)
    return same, len(pairs)


def _ratio(num: int, den: int) -> float | None:
    return num / den if den else None


@dataclass(frozen=True)
class PrecheckItem:
    """§6-A の入力 1 件。gold と教師の結果を並べたもの。"""

    id: str
    gold_label: str
    gold_boundary: bool
    roundup: bool
    teacher: TeacherRow


def precheck_metrics(items: Sequence[PrecheckItem]) -> dict[str, Any]:
    """§6-A: まとめ記事・境界を除いた枠で A1/A2/A3。枠の中の正解 high が 20 件未満は判定不能。"""
    frame = [i for i in items if not i.roundup and not i.gold_boundary]
    gold_high = [i for i in frame if is_high(i.gold_label)]
    teacher_high = [i for i in frame if is_high(i.teacher.label)]
    tp = sum(is_high(i.teacher.label) for i in gold_high)
    same, n_second = agreement([i.teacher for i in frame])
    values = {
        "A1": _ratio(tp, len(gold_high)),
        "A2": _ratio(tp, len(teacher_high)),
        "A3": _ratio(same, n_second),
    }
    undetermined = len(gold_high) < GOLD_MIN_HIGH_IN_FRAME or any(
        v is None for v in values.values()
    )
    passed = {
        k: (values[k] is not None and values[k] >= t)  # type: ignore[operator]
        for k, t in THRESHOLDS_A.items()
    }
    verdict = "UNDETERMINED" if undetermined else ("PASS" if all(passed.values()) else "FAIL")
    all_gold_high = [i for i in items if is_high(i.gold_label)]
    all_teacher_high = [i for i in items if is_high(i.teacher.label)]
    all_tp = sum(is_high(i.teacher.label) for i in all_gold_high)
    all_same, all_second = agreement([i.teacher for i in items])
    return {
        "verdict": verdict,
        "thresholds": dict(THRESHOLDS_A),
        "frame": {
            "n_all": len(items),
            "n_frame": len(frame),
            "excluded_roundup": sum(i.roundup for i in items),
            "excluded_boundary": sum(i.gold_boundary for i in items),
            "gold_high": len(gold_high),
            "teacher_high": len(teacher_high),
            "true_positive": tp,
            "second_pass_rows": n_second,
            "second_pass_same": same,
        },
        "values": values,
        "passed": passed,
        "unframed_record_only": {
            "A1": _ratio(all_tp, len(all_gold_high)),
            "A2": _ratio(all_tp, len(all_teacher_high)),
            "A3": _ratio(all_same, all_second),
        },
        "confusion_frame": _matrix([(i.gold_label, i.teacher.label) for i in frame]),
    }


def _matrix(pairs: Sequence[tuple[str, str]]) -> dict[str, dict[str, int]]:
    out: dict[str, Counter[str]] = {}
    for a, b in pairs:
        out.setdefault(a, Counter())[b] += 1
    return {a: dict(c) for a, c in sorted(out.items())}


@dataclass(frozen=True)
class RelabelRow:
    """§6-B の入力 1 行。"""

    key: str
    old: str
    category: str | None
    roundup: bool
    teacher: TeacherRow


def relabel_gates(
    rows: Sequence[RelabelRow], failed: Sequence[str], population: int
) -> dict[str, Any]:
    """§6-B: B1 (2 回目の一致 ≥ 0.90、まとめ記事・境界を除く)・B2 (検査に当たる行 ≤ 2%)・B3 (失敗 0・地政学 high 0)。"""
    frame = [r.teacher for r in rows if not r.roundup and not r.teacher.boundary]
    same, n_second = agreement(frame)
    b1 = _ratio(same, n_second)
    limit_b2 = math.floor(population * THRESHOLD_B2_SHARE)
    n_flagged = sum(bool(r.teacher.flags_final) for r in rows)
    geo_high = sum(is_high(r.teacher.label) and r.category == "geopolitical" for r in rows)
    passed = {
        "B1": b1 is not None and b1 >= THRESHOLD_B1,
        "B2": n_flagged <= limit_b2,
        "B3": len(failed) == 0 and geo_high == 0,
    }
    return {
        "verdict": "PASS" if all(passed.values()) else "FAIL",
        "passed": passed,
        "B1": {"value": b1, "threshold": THRESHOLD_B1, "second_pass_rows": n_second, "same": same},
        "B2": {"flagged_after_vote": n_flagged, "limit": limit_b2, "population": population},
        "B3": {"failed_rows": len(failed), "geopolitical_high": geo_high},
        "record_only": {
            "matrix_old_new": _matrix([(r.old, r.teacher.label) for r in rows]),
            "second_pass_rows_all": sum(len(r.teacher.judgments) >= 2 for r in rows),
            "boundary_rows": sum(r.teacher.boundary for r in rows),
            "roundup_rows": sum(r.roundup for r in rows),
            "roundup_rows_high": sum(r.roundup and is_high(r.teacher.label) for r in rows),
            "flagged_first_pass": sum(bool(r.teacher.flags_first) for r in rows),
            "criterion_high_counts": dict(
                Counter(
                    str(r.teacher.judgments[-1].get("criterion", ""))[:40]
                    for r in rows
                    if is_high(r.teacher.label)
                ).most_common(20)
            ),
        },
    }
