"""E1・E2 の集計の純関数 (DB にも Opus にも触れない)。

- 盲検の札 (X / Y) を腕 (A = 線なし / B = 線つき) へ戻す
- E1: 提示順ごとの勝ち数・主張の裏付け率・課題ごとの判定 (prereg graphrag_e1_rescore.md §6)
- E2: 型ごとの精度・Wilson 区間・用途の判定 (prereg graphrag_e2_edge_precision.md §6)
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from scripts.replay._stats import Interval, wilson_interval

Arm = Literal["A", "B"]
Label = Literal["X", "Y"]
Order = Literal["AB", "BA"]
ORDERS: tuple[Order, Order] = ("AB", "BA")
TIE = "同等"

#: E1 の線 (prereg §6)
E1_MIN_WINS = 10
E1_N_WINDOWS = 15
E1_NONINF_MARGIN = -0.03

#: E2 の線 (prereg §6)
E2_WRITER_PRECISION = 0.90
E2_WRITER_WILSON_LOW = 0.80
E2_SCREEN_PRECISION = 0.85


def arm_of_label(label: Label, order: Order) -> Arm:
    """提示順 AB = X が A (線なし)・Y が B (線つき)。BA はその逆。"""
    if order == "AB":
        return "A" if label == "X" else "B"
    return "B" if label == "X" else "A"


def label_of_arm(arm: Arm, order: Order) -> Label:
    labels: tuple[Label, Label] = ("X", "Y")
    for lab in labels:
        if arm_of_label(lab, order) == arm:
            return lab
    raise AssertionError("unreachable")


def unblind(choice: str, order: Order) -> str:
    """判定の札 (X / Y / 同等) を腕 (A / B / 同等) へ。"""
    if choice == TIE:
        return TIE
    if choice not in ("X", "Y"):
        raise ValueError(f"unknown choice: {choice!r}")
    return arm_of_label(choice, order)  # type: ignore[arg-type]


def outputs_in_order(a_text: str, b_text: str, order: Order) -> tuple[str, str]:
    """(X, Y) の本文。"""
    return (a_text, b_text) if order == "AB" else (b_text, a_text)


# ---------------------------------------------------------------- E1


@dataclass(frozen=True)
class PairwiseRow:
    task: str
    key: str
    order: Order
    winner: str  # A / B / 同等 (札を戻した後)
    len_a: int
    len_b: int


@dataclass(frozen=True)
class ClaimTally:
    task: str
    key: str
    arm: Arm
    total: int
    supported: int  # その腕が受け取った入力で裏付けられる主張
    misattributed: int  # 線の節にだけある内容を記事の番号に帰属させた主張 (型①)


@dataclass(frozen=True)
class E1TaskResult:
    task: str
    wins_b: dict[str, int]  # 順 → 線つきの勝ち数
    wins_a: dict[str, int]
    ties: dict[str, int]
    n: dict[str, int]
    consistent_windows: int  # 両方の順で同じ腕が勝った窓
    position_first_wins: int  # 先に出した方 (X) が勝った回数 (両順の計)
    support_a: float | None
    support_b: float | None
    support_diff: float | None
    misattributed_b: int
    mean_len_diff: float  # 線つき − 線なしの字数 (窓の平均)
    verdict: str  # 維持 / 評価の産物 / 未確定


def _as_arm(value: str) -> Arm:
    if value == "A":
        return "A"
    if value == "B":
        return "B"
    raise ValueError(f"not an arm: {value!r}")


def tally_pairwise(rows: Iterable[PairwiseRow]) -> dict[str, dict[str, dict[str, int]]]:
    """task → order → {A, B, 同等} の数。"""
    out: dict[str, dict[str, dict[str, int]]] = {}
    for r in rows:
        cell = out.setdefault(r.task, {}).setdefault(r.order, {"A": 0, "B": 0, TIE: 0})
        cell[r.winner] += 1
    return out


def support_rate(tallies: Iterable[ClaimTally], arm: Arm) -> float | None:
    rows = [t for t in tallies if t.arm == arm]
    total = sum(t.total for t in rows)
    return None if total == 0 else sum(t.supported for t in rows) / total


def e1_verdict(
    wins_b_by_order: Mapping[str, int],
    n_by_order: Mapping[str, int],
    support_diff: float | None,
) -> str:
    """prereg §6: 両順で線つきの勝ち ≥10/15 かつ裏付け率の差 ≥ -3pt → 維持。

    どちらかの順で勝ち ≤9 (= 順で逆転を含む) → 評価の産物。
    窓が欠けた・主張の照合が無い → 未確定。
    """
    if any(n_by_order.get(o, 0) != E1_N_WINDOWS for o in ORDERS) or support_diff is None:
        return "未確定"
    if all(wins_b_by_order.get(o, 0) >= E1_MIN_WINS for o in ORDERS):
        return "維持" if support_diff >= E1_NONINF_MARGIN - 1e-12 else "裏付けで不合格"
    return "評価の産物"


def e1_task_result(
    task: str, rows: Sequence[PairwiseRow], tallies: Sequence[ClaimTally]
) -> E1TaskResult:
    rows = [r for r in rows if r.task == task]
    tallies = [t for t in tallies if t.task == task]
    cells = tally_pairwise(rows).get(task, {})
    wins_b: dict[str, int] = {str(o): cells.get(o, {}).get("B", 0) for o in ORDERS}
    wins_a: dict[str, int] = {str(o): cells.get(o, {}).get("A", 0) for o in ORDERS}
    ties: dict[str, int] = {str(o): cells.get(o, {}).get(TIE, 0) for o in ORDERS}
    n: dict[str, int] = {str(o): sum(cells.get(o, {}).values()) for o in ORDERS}
    by_key: dict[str, dict[str, str]] = {}
    for r in rows:
        by_key.setdefault(r.key, {})[r.order] = r.winner
    consistent = sum(
        1 for v in by_key.values() if len(v) == 2 and v["AB"] == v["BA"] and v["AB"] != TIE
    )
    first = sum(
        1
        for r in rows
        if r.winner in ("A", "B") and label_of_arm(_as_arm(r.winner), r.order) == "X"
    )
    sa, sb = support_rate(tallies, "A"), support_rate(tallies, "B")
    diff = None if sa is None or sb is None else sb - sa
    lens = {r.key: r.len_b - r.len_a for r in rows}
    mean_len = sum(lens.values()) / len(lens) if lens else 0.0
    return E1TaskResult(
        task=task,
        wins_b=wins_b,
        wins_a=wins_a,
        ties=ties,
        n=n,
        consistent_windows=consistent,
        position_first_wins=first,
        support_a=sa,
        support_b=sb,
        support_diff=diff,
        misattributed_b=sum(t.misattributed for t in tallies if t.arm == "B"),
        mean_len_diff=mean_len,
        verdict=e1_verdict(wins_b, n, diff),
    )


def win_rate_by_length(rows: Sequence[PairwiseRow]) -> dict[str, tuple[int, int]]:
    """長さの差の符号 (線つきが長い / 短い・同じ) ごとの (線つきの勝ち, 判定数)。記述のみ。"""
    out: dict[str, list[int]] = {"b_longer": [0, 0], "b_not_longer": [0, 0]}
    for r in rows:
        k = "b_longer" if r.len_b > r.len_a else "b_not_longer"
        out[k][1] += 1
        out[k][0] += int(r.winner == "B")
    return {k: (v[0], v[1]) for k, v in out.items()}


# ---------------------------------------------------------------- E2

EdgeType = Literal["same_actor", "incident", "lineage"]
EDGE_TYPES: tuple[EdgeType, ...] = ("same_actor", "incident", "lineage")
RELATIONS = ("同じ事象", "系統", "同じアクターの別の活動", "無関係")


@dataclass(frozen=True)
class EdgeJudgement:
    pair_key: str
    types: frozenset[str]  # その組が属する型 (複数ありうる)
    relation: str
    same_attacker: str  # はい / いいえ / 不明
    tag_a: str  # 正しい / 誤り / タグ無し / 判断できない
    tag_b: str


def edge_correct(edge_type: str, j: EdgeJudgement, *, lenient: bool = False) -> bool:
    """prereg §6 の正誤の定義。"""
    if edge_type == "same_actor":
        return j.same_attacker == "はい"
    if edge_type == "incident":
        return j.relation in ("同じ事象", "系統")
    if edge_type == "lineage":
        return j.relation in (("系統", "同じ事象") if lenient else ("系統",))
    raise ValueError(f"unknown edge type: {edge_type}")


def subject_error(j: EdgeJudgement) -> bool:
    return "誤り" in (j.tag_a, j.tag_b)


@dataclass(frozen=True)
class E2TypeResult:
    edge_type: str
    n: int
    correct: int
    precision: Interval
    lenient_correct: int
    wrong: int
    wrong_with_subject_error: int
    unknown_attacker: int
    use: str  # 書く材料 / 画面のみ / 不可 / 標本なし


def e2_use(n: int, correct: int) -> tuple[Interval, str]:
    iv = wilson_interval(correct, n)
    if n == 0:
        return iv, "標本なし"
    if iv.point >= E2_WRITER_PRECISION and iv.low >= E2_WRITER_WILSON_LOW:
        return iv, "書く材料"
    if iv.point >= E2_SCREEN_PRECISION:
        return iv, "画面のみ"
    return iv, "不可"


def e2_type_result(edge_type: str, judgements: Sequence[EdgeJudgement]) -> E2TypeResult:
    rows = [j for j in judgements if edge_type in j.types]
    correct = sum(edge_correct(edge_type, j) for j in rows)
    lenient = sum(edge_correct(edge_type, j, lenient=True) for j in rows)
    wrong = [j for j in rows if not edge_correct(edge_type, j)]
    iv, use = e2_use(len(rows), correct)
    return E2TypeResult(
        edge_type=edge_type,
        n=len(rows),
        correct=correct,
        precision=iv,
        lenient_correct=lenient,
        wrong=len(wrong),
        wrong_with_subject_error=sum(subject_error(j) for j in wrong),
        unknown_attacker=sum(j.same_attacker == "不明" for j in rows),
        use=use,
    )
