"""src.tools.probe_calibration のテスト (s24_plan §4.2)。"""

from __future__ import annotations

import math

import pytest

from src.tools.probe_calibration import (
    OrgRow,
    Row,
    confident_band_accuracy,
    ece,
    group_folds,
    operating_high_accept,
    operating_low_flag,
    org_same,
    out_of_fold_high_accept,
    out_of_fold_low_flag,
    out_of_fold_org,
    pick_high_accept_threshold,
    pick_low_flag_threshold,
    pick_org_thresholds,
    platt_apply,
    platt_fit,
    weighted_auc,
)


def _rows(pairs: list[tuple[int, float]], weight: float = 1.0) -> list[Row]:
    return [Row(f"g{i}", label, score, weight) for i, (label, score) in enumerate(pairs)]


class TestAuc:
    def test_perfect_separation_is_one(self) -> None:
        assert weighted_auc(_rows([(1, 0.9), (1, 0.8), (0, 0.2), (0, 0.1)])) == 1.0

    def test_ties_count_half(self) -> None:
        assert weighted_auc(_rows([(1, 0.5), (0, 0.5)])) == 0.5

    def test_none_when_one_class_missing(self) -> None:
        assert weighted_auc(_rows([(1, 0.9), (1, 0.8)])) is None

    def test_weights_shift_the_result(self) -> None:
        rows = [Row("a", 1, 0.9, 1.0), Row("b", 1, 0.1, 3.0), Row("c", 0, 0.5, 1.0)]
        # 重み 3 の正例が負例より低い → AUC は (1*1 + 3*0) / 4
        assert weighted_auc(rows) == pytest.approx(0.25)


class TestEceAndBand:
    def test_ece_zero_when_perfectly_calibrated_bins(self) -> None:
        rows = [Row("a", 1, 1.0), Row("b", 0, 0.0)]
        assert ece(rows) == pytest.approx(0.0)

    def test_ece_detects_overconfidence(self) -> None:
        rows = [Row(str(i), 1 if i < 5 else 0, 0.95) for i in range(10)]
        assert ece(rows) == pytest.approx(0.45)

    def test_confident_band_ignores_middle(self) -> None:
        acc, n = confident_band_accuracy(_rows([(1, 0.95), (0, 0.05), (1, 0.5), (0, 0.9)]))
        assert n == 3
        assert acc == pytest.approx(2 / 3)

    def test_confident_band_empty(self) -> None:
        assert confident_band_accuracy(_rows([(1, 0.5)])) == (None, 0)


class TestPlatt:
    def test_platt_improves_ece_of_overconfident_scores(self) -> None:
        rows = [Row(str(i), 1 if i % 4 else 0, 0.99 if i % 2 == 0 else 0.01) for i in range(80)]
        a, b = platt_fit(rows)
        assert ece(platt_apply(rows, a, b)) < ece(rows)  # type: ignore[operator]
        assert math.isfinite(a) and math.isfinite(b)


class TestLowFlag:
    def test_operating_point(self) -> None:
        rows = _rows([(0, 0.01), (0, 0.05), (0, 0.5), (1, 0.9), (1, 0.06)])
        op = operating_low_flag(rows, 0.1)
        assert op.recall == pytest.approx(2 / 3)
        assert op.false_fire == pytest.approx(0.5)
        assert op.precision == pytest.approx(2 / 3)

    def test_pick_largest_threshold_under_false_fire_cap(self) -> None:
        rows = _rows([(0, 0.01), (0, 0.2), (0, 0.4), (1, 0.3), (1, 0.6), (1, 0.7), (1, 0.8)])
        # Yes 側の最小 0.3 を拾わない T = 0.3 が上限 (誤発火 0)
        assert pick_low_flag_threshold(rows, 0.0) == 0.3
        # 誤発火 25% まで許すと 0.3 を拾う T=0.6 まで上がる
        assert pick_low_flag_threshold(rows, 0.25) == 0.6

    def test_pick_none_without_both_classes(self) -> None:
        assert pick_low_flag_threshold(_rows([(1, 0.5)]), 0.03) is None

    def test_weighted_false_fire_uses_sampling_weights(self) -> None:
        # 重みの小さい Yes 側の誤発火は上限を超えない
        rows = [Row("a", 1, 0.1, 0.01), Row("b", 1, 0.9, 10.0), Row("c", 0, 0.05, 1.0)]
        assert pick_low_flag_threshold(rows, 0.05) == 0.9


class TestHighAccept:
    def test_pick_smallest_threshold_meeting_precision(self) -> None:
        rows = _rows([(0, 0.2), (0, 0.55), (1, 0.5), (1, 0.7), (1, 0.9), (0, 0.1)])
        t = pick_high_accept_threshold(rows, 0.9)
        assert t == 0.7
        assert t is not None
        op = operating_high_accept(rows, t)
        assert op.precision == 1.0
        assert op.recall == pytest.approx(2 / 3)

    def test_none_when_precision_unreachable(self) -> None:
        assert pick_high_accept_threshold(_rows([(0, 0.9), (1, 0.1)]), 0.9) is None


class TestOrg:
    def _rows(self) -> list[OrgRow]:
        return [
            OrgRow("p1", 1, 15.0, 2.0),
            OrgRow("p2", 1, 13.0, -1.0),
            OrgRow("p3", 1, 5.0, 3.0),  # event が低い同一 (取りこぼし)
            OrgRow("n1", 0, 14.0, -8.0),
            OrgRow("n2", 0, 2.0, 4.0),
        ]

    def test_zero_false_same_with_max_recall(self) -> None:
        picked = pick_org_thresholds(self._rows(), 0.0)
        assert picked is not None
        ev, og = picked
        rows = self._rows()
        assert not any(org_same(r, ev, og) for r in rows if r.label == 0)
        assert sum(org_same(r, ev, og) for r in rows if r.label == 1) == 3

    def test_none_without_positives(self) -> None:
        assert pick_org_thresholds([OrgRow("n", 0, 1.0, 1.0)], 0.0) is None


class TestOutOfFold:
    def test_folds_keep_a_group_together(self) -> None:
        folds = group_folds(["a", "a", "b", "c", "d", "e", "f"], folds=3)
        assert set(folds) == {"a", "b", "c", "d", "e", "f"}
        assert set(folds.values()) <= {0, 1, 2}

    def test_low_flag_oof_does_not_see_its_own_fold(self) -> None:
        rows = [Row(f"g{i}", 0, 0.01 + i * 0.001) for i in range(30)]
        rows += [Row(f"h{i}", 1, 0.5 + i * 0.01) for i in range(60)]
        op, ts = out_of_fold_low_flag(rows, 0.0)
        assert op is not None
        assert len(ts) == 5
        assert op.recall == 1.0
        assert (
            op.false_fire is not None and op.false_fire <= 0.1
        )  # 分割外の Yes 側が学習側の最小より低いと数件拾う

    def test_high_accept_oof(self) -> None:
        rows = [Row(f"g{i}", 0, 0.1 + i * 0.001) for i in range(40)]
        rows += [Row(f"h{i}", 1, 0.8 + i * 0.001) for i in range(40)]
        op, ts = out_of_fold_high_accept(rows, 0.95)
        assert op is not None and len(ts) == 5
        assert op.precision is not None and op.precision >= 0.95

    def test_org_oof(self) -> None:
        rows = [OrgRow(f"p{i}", 1, 14.0 + i * 0.01, 1.0) for i in range(25)]
        rows += [OrgRow(f"n{i}", 0, 3.0, -9.0) for i in range(25)]
        op, ts = out_of_fold_org(rows)
        assert op is not None and len(ts) == 5
        assert op.false_fire == 0.0
        assert op.recall is not None and op.recall >= 0.9
