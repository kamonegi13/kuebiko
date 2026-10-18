"""logprob 判定のしきい値を決める純粋な計算 (s24_plan §4.2、I/O を持たない)。

``scripts/calibrate_probes.py`` が較正集を流して得た (正解, スコア) の行を渡す。ここは
AUC・ECE・Platt・確信帯・しきい値の選び方・5 分割の分割外評価だけ。サンプリング重み
(濃縮抽出を自然比に戻す) は全関数で ``weight`` として扱う。
"""

from __future__ import annotations

import math
import random
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass

DEFAULT_FOLDS = 5
DEFAULT_SEED = 10_115
ECE_BINS = 10
CONFIDENT_LOW = 0.2
CONFIDENT_HIGH = 0.8
_LOGIT_EPS = 1e-6
_PLATT_ITERATIONS = 50
_PLATT_RIDGE = 1e-6


@dataclass(frozen=True)
class Row:
    """1 件の較正行。

    ``label``=1 は『Yes 側 (中身あり / 裏付けあり / 同一)』、score は P(Yes) か logit。
    """

    group: str
    label: int
    score: float
    weight: float = 1.0


@dataclass(frozen=True)
class OrgRow:
    """被害組織名の同一判定 (2 問の AND)。``event`` / ``org`` はそれぞれの logit。"""

    group: str
    label: int
    event: float
    org: float
    weight: float = 1.0


@dataclass(frozen=True)
class Operating:
    """しきい値での動作点。``target`` 側 (薄い・裏付けなし・同一) を拾う前提の回収と誤発火。"""

    threshold: float
    recall: float | None
    false_fire: float | None
    precision: float | None


def _sigmoid(x: float) -> float:
    """オーバーフローしない sigmoid。"""
    if x >= 0:
        return 1 / (1 + math.exp(-x))
    e = math.exp(x)
    return e / (1 + e)


def logit(p: float) -> float:
    clipped = min(max(p, _LOGIT_EPS), 1 - _LOGIT_EPS)
    return math.log(clipped / (1 - clipped))


def _wsum(rows: Sequence[Row], pred: Callable[[Row], bool]) -> float:
    return sum(r.weight for r in rows if pred(r))


def weighted_auc(rows: Sequence[Row]) -> float | None:
    """P(Yes 側のスコア > No 側のスコア) (同点は 0.5、重み付き)。片側が空なら ``None``。"""
    pos = [r for r in rows if r.label == 1]
    neg = [r for r in rows if r.label == 0]
    if not pos or not neg:
        return None
    num = sum(
        p.weight * n.weight * (1.0 if p.score > n.score else 0.5 if p.score == n.score else 0.0)
        for p in pos
        for n in neg
    )
    den = sum(p.weight for p in pos) * sum(n.weight for n in neg)
    return num / den if den else None


def ece(rows: Sequence[Row], bins: int = ECE_BINS) -> float | None:
    """P(Yes) の期待較正誤差 (等幅 ``bins``、重み付き)。``score`` は確率であること。"""
    total = sum(r.weight for r in rows)
    if not rows or total <= 0:
        return None
    err = 0.0
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        inbin = [r for r in rows if (lo <= r.score < hi) or (b == bins - 1 and r.score == 1.0)]
        w = sum(r.weight for r in inbin)
        if w <= 0:
            continue
        conf = sum(r.weight * r.score for r in inbin) / w
        acc = sum(r.weight * r.label for r in inbin) / w
        err += (w / total) * abs(conf - acc)
    return err


def confident_band_accuracy(rows: Sequence[Row]) -> tuple[float | None, int]:
    """P<0.2 または P>0.8 の行だけの正解率 (P>0.5 を Yes と読む)。戻り値は (正解率, 件数)。"""
    band = [r for r in rows if r.score < CONFIDENT_LOW or r.score > CONFIDENT_HIGH]
    w = sum(r.weight for r in band)
    if w <= 0:
        return None, 0
    ok = sum(r.weight for r in band if (r.score > 0.5) == (r.label == 1))
    return ok / w, len(band)


def platt_fit(rows: Sequence[Row]) -> tuple[float, float]:
    """logit(P) に対するロジスティック回帰 (Platt)。

    戻り値 (a, b): 較正後 P = sigmoid(a*logit+b)。
    """
    a, b = 1.0, 0.0
    xs = [logit(r.score) for r in rows]
    for _ in range(_PLATT_ITERATIONS):
        g_a = g_b = h_aa = h_ab = h_bb = 0.0
        for x, r in zip(xs, rows, strict=True):
            p = _sigmoid(a * x + b)
            w = r.weight
            err = p - r.label
            g_a += w * err * x
            g_b += w * err
            s = w * p * (1 - p)
            h_aa += s * x * x
            h_ab += s * x
            h_bb += s
        h_aa += _PLATT_RIDGE
        h_bb += _PLATT_RIDGE
        det = h_aa * h_bb - h_ab * h_ab
        if abs(det) < 1e-12:
            break
        da = (h_bb * g_a - h_ab * g_b) / det
        db = (h_aa * g_b - h_ab * g_a) / det
        a, b = a - da, b - db
        if abs(da) + abs(db) < 1e-9:
            break
    return a, b


def platt_apply(rows: Sequence[Row], a: float, b: float) -> list[Row]:
    return [Row(r.group, r.label, _sigmoid(a * logit(r.score) + b), r.weight) for r in rows]


# ---------------------------------------------------------------------------
# 動作点とその選び方
# ---------------------------------------------------------------------------


def operating_low_flag(rows: Sequence[Row], threshold: float) -> Operating:
    """『score < T なら No 側 (薄い・裏付けなし) と拾う』ときの動作点。"""
    tgt_total = _wsum(rows, lambda r: r.label == 0)
    oth_total = _wsum(rows, lambda r: r.label == 1)
    caught = _wsum(rows, lambda r: r.label == 0 and r.score < threshold)
    wrong = _wsum(rows, lambda r: r.label == 1 and r.score < threshold)
    fired = caught + wrong
    return Operating(
        threshold,
        caught / tgt_total if tgt_total else None,
        wrong / oth_total if oth_total else None,
        caught / fired if fired else None,
    )


def operating_high_accept(rows: Sequence[Row], threshold: float) -> Operating:
    """『score >= T なら Yes 側 (同一・証拠) と拾う』ときの動作点 (target = label 1)。"""
    tgt_total = _wsum(rows, lambda r: r.label == 1)
    oth_total = _wsum(rows, lambda r: r.label == 0)
    caught = _wsum(rows, lambda r: r.label == 1 and r.score >= threshold)
    wrong = _wsum(rows, lambda r: r.label == 0 and r.score >= threshold)
    fired = caught + wrong
    return Operating(
        threshold,
        caught / tgt_total if tgt_total else None,
        wrong / oth_total if oth_total else None,
        caught / fired if fired else None,
    )


def pick_low_flag_threshold(rows: Sequence[Row], max_false_fire: float) -> float | None:
    """誤発火 (Yes 側を誤って拾う割合) ≤ ``max_false_fire`` を満たす**最大**の T (回収が最大)。

    候補は観測された score。『score < T』なので T は Yes 側の score そのものを選ぶ
    (その行は拾わない)。Yes 側が無ければ ``None``。上限を満たす T が最小の score 以下しか
    無いときも、最小の score を返す。
    """
    yes = sorted({r.score for r in rows if r.label == 1})
    if not yes or not any(r.label == 0 for r in rows):
        return None
    best = min(r.score for r in rows)
    for t in sorted({r.score for r in rows}):
        op = operating_low_flag(rows, t)
        if op.false_fire is not None and op.false_fire <= max_false_fire:
            best = t
    return best


def pick_high_accept_threshold(rows: Sequence[Row], min_precision: float) -> float | None:
    """適合率 ≥ ``min_precision`` を満たす**最小**の T (回収が最大)。無ければ ``None``。"""
    if not any(r.label == 1 for r in rows):
        return None
    for t in sorted({r.score for r in rows}):
        op = operating_high_accept(rows, t)
        if op.precision is not None and op.precision >= min_precision and op.recall:
            return t
    return None


def org_same(row: OrgRow, event_t: float, org_t: float) -> bool:
    return row.event >= event_t and row.org >= org_t


def pick_org_thresholds(
    rows: Sequence[OrgRow], max_false_same: float = 0.0
) -> tuple[float, float] | None:
    """誤同一視の割合 ≤ ``max_false_same`` の中で回収が最大の (event_t, org_t)。

    誤同一視 = 別組織を同一と判定すること。

    同点は閾値の和が大きい方 (より保守的) を選ぶ。正例 (同一) が無ければ ``None``。
    """
    pos = [r for r in rows if r.label == 1]
    neg = [r for r in rows if r.label == 0]
    if not pos or not neg:
        return None
    neg_total = sum(r.weight for r in neg)
    pos_total = sum(r.weight for r in pos)
    best: tuple[float, float, float] | None = None  # (recall, sum, ...) の比較用
    result: tuple[float, float] | None = None
    for ev in sorted({r.event for r in pos}):
        for og in sorted({r.org for r in pos}):
            false_same = sum(r.weight for r in neg if org_same(r, ev, og)) / neg_total
            if false_same > max_false_same:
                continue
            recall = sum(r.weight for r in pos if org_same(r, ev, og)) / pos_total
            key = (recall, ev + og, 0.0)
            if best is None or key > best:
                best, result = key, (ev, og)
    return result


# ---------------------------------------------------------------------------
# 分割外評価
# ---------------------------------------------------------------------------


def group_folds(
    groups: Sequence[str], folds: int = DEFAULT_FOLDS, seed: int = DEFAULT_SEED
) -> dict[str, int]:
    """グループ (記事 id・key・組) 単位で ``folds`` 分割。固定 seed の無作為割当。"""
    uniq = sorted(set(groups))
    rng = random.Random(seed)
    rng.shuffle(uniq)
    return {g: i % folds for i, g in enumerate(uniq)}


def out_of_fold_low_flag(
    rows: Sequence[Row], max_false_fire: float, folds: int = DEFAULT_FOLDS, seed: int = DEFAULT_SEED
) -> tuple[Operating | None, list[float]]:
    """他の分割で T を決め残りで当てる。戻り値は (分割外を束ねた動作点, 各分割の T)。"""
    fold_of = group_folds([r.group for r in rows], folds, seed)
    by_fold: dict[int, list[Row]] = defaultdict(list)
    for r in rows:
        by_fold[fold_of[r.group]].append(r)
    flagged: list[Row] = []
    thresholds: list[float] = []
    for k in range(folds):
        train = [r for f, rs in by_fold.items() if f != k for r in rs]
        t = pick_low_flag_threshold(train, max_false_fire)
        if t is None:
            continue
        thresholds.append(t)
        flagged.extend(
            Row(r.group, r.label, r.score - t, r.weight) for r in by_fold.get(k, [])
        )  # score<0 が拾われる
    return (operating_low_flag(flagged, 0.0) if flagged else None), thresholds


def out_of_fold_high_accept(
    rows: Sequence[Row], min_precision: float, folds: int = DEFAULT_FOLDS, seed: int = DEFAULT_SEED
) -> tuple[Operating | None, list[float]]:
    fold_of = group_folds([r.group for r in rows], folds, seed)
    by_fold: dict[int, list[Row]] = defaultdict(list)
    for r in rows:
        by_fold[fold_of[r.group]].append(r)
    shifted: list[Row] = []
    thresholds: list[float] = []
    for k in range(folds):
        train = [r for f, rs in by_fold.items() if f != k for r in rs]
        t = pick_high_accept_threshold(train, min_precision)
        if t is None:
            continue
        thresholds.append(t)
        shifted.extend(
            Row(r.group, r.label, r.score - t, r.weight) for r in by_fold.get(k, [])
        )  # score>=0 が拾われる
    return (operating_high_accept(shifted, 0.0) if shifted else None), thresholds


def out_of_fold_org(
    rows: Sequence[OrgRow],
    max_false_same: float = 0.0,
    folds: int = DEFAULT_FOLDS,
    seed: int = DEFAULT_SEED,
) -> tuple[Operating | None, list[tuple[float, float]]]:
    fold_of = group_folds([r.group for r in rows], folds, seed)
    by_fold: dict[int, list[OrgRow]] = defaultdict(list)
    for r in rows:
        by_fold[fold_of[r.group]].append(r)
    all_tests: list[tuple[OrgRow, bool]] = []
    picked: list[tuple[float, float]] = []
    for k in range(folds):
        train = [r for f, rs in by_fold.items() if f != k for r in rs]
        t = pick_org_thresholds(train, max_false_same)
        if t is None:
            continue
        picked.append(t)
        all_tests.extend((r, org_same(r, *t)) for r in by_fold.get(k, []))
    if not all_tests:
        return None, picked
    pos = sum(r.weight for r, _ in all_tests if r.label == 1)
    neg = sum(r.weight for r, _ in all_tests if r.label == 0)
    caught = sum(r.weight for r, said in all_tests if r.label == 1 and said)
    wrong = sum(r.weight for r, said in all_tests if r.label == 0 and said)
    fired = caught + wrong
    op = Operating(
        0.0,
        caught / pos if pos else None,
        wrong / neg if neg else None,
        caught / fired if fired else None,
    )
    return op, picked
