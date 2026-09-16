"""段B-2b: 安定フィードコホート上の構成比 — 型 E (趨勢) / H (閾値) の土台。

**なぜ構成比か** (docs/pir_brief_design.md §6c): 収集網は実測で 8 週に 114→245 フィードへ
倍増しており、生の件数は「活動が増えた」と「網が広がった」を分離できない。同一コホートに
限れば網の成長は除去でき、観測バイアス仮説が**判定可能**になる。

**なぜ件数でなく比か**: 実測では同一コホートでも絶対数が 4,465→2,634 に落ちており、
件数で見ると「全部減った」になる。構成比にして初めて軸の移動が見える。

このファイルが固定する不変条件:
1. コホート = **両窓に居る**フィードだけ (片方にしか居ないものは網の変化そのもの)
2. 出力の主役は share であって count ではない
3. コホートの代表性 (article_share) を必ず併せて返す — 低ければ測定自体が弱い
4. 母集団が薄い軸は `is_thin` で明示する (型の適用前に母集団を数える)
5. 空窓・ゼロ除算で落ちない
"""

from __future__ import annotations

from src.assessment.composition import AxisRow, measure_composition


def _rows(*specs: tuple[str, str, str]) -> list[AxisRow]:
    """(feed, window, value) の並びを行に変換。"""
    return [AxisRow(feed_title=f, window=w, value=v) for f, w, v in specs]


class TestCohortSelection:
    def test_cohort_keeps_only_feeds_present_in_both_windows(self) -> None:
        """片窓にしか居ないフィードは網の変化そのもの — 比較から外す。"""
        rows = _rows(
            ("stable", "p1", "espionage"),
            ("stable", "p2", "espionage"),
            ("added_later", "p2", "espionage"),
            ("gone", "p1", "financial"),
        )

        m = measure_composition(rows)

        assert m.cohort.feeds == 1

    def test_article_share_reports_cohort_representativeness(self) -> None:
        """コホートが全体の何割を出しているか — 低ければ測定は弱い。"""
        rows = _rows(
            ("stable", "p1", "a"),
            ("stable", "p2", "a"),
            ("added_later", "p2", "a"),
        )

        m = measure_composition(rows)

        assert m.cohort.article_share == 2 / 3

    def test_new_feeds_do_not_shift_the_measured_composition(self) -> None:
        """網の成長で構成が動いて見えないこと (この型の存在理由)。"""
        stable = _rows(
            ("s", "p1", "espionage"),
            ("s", "p2", "espionage"),
        )
        flooded = [*stable, *_rows(*[("new", "p2", "financial")] * 50)]

        assert measure_composition(stable).shift_for("espionage") == measure_composition(
            flooded
        ).shift_for("espionage")


class TestShareNotCount:
    def test_share_rises_even_when_count_falls(self) -> None:
        """実測と同じ状況 — 絶対数は落ちたが構成比は上がった。件数で見ると誤読する。"""
        rows = _rows(
            *[("s", "p1", "focus")] * 2,
            *[("s", "p1", "other")] * 6,
            *[("s", "p2", "focus")] * 1,
            *[("s", "p2", "other")] * 1,
        )

        shift = measure_composition(rows).shift_for("focus")

        assert shift is not None
        assert shift.p2_count < shift.p1_count
        assert shift.p2_share > shift.p1_share
        assert shift.shift_points > 0

    def test_shares_sum_to_one_per_window(self) -> None:
        rows = _rows(
            ("s", "p1", "a"),
            ("s", "p1", "b"),
            ("s", "p2", "a"),
            ("s", "p2", "b"),
            ("s", "p2", "c"),
        )

        m = measure_composition(rows)

        assert round(sum(s.p1_share for s in m.shifts), 6) == 1.0
        assert round(sum(s.p2_share for s in m.shifts), 6) == 1.0

    def test_shifts_are_ordered_by_magnitude(self) -> None:
        """読み手が最初に見るべき動きを先頭に置く。"""
        rows = _rows(
            ("s", "p1", "big"),
            ("s", "p1", "big"),
            ("s", "p1", "small"),
            ("s", "p1", "small"),
            ("s", "p2", "small"),
            ("s", "p2", "small"),
            ("s", "p2", "small"),
            ("s", "p2", "big"),
        )

        m = measure_composition(rows)

        assert abs(m.shifts[0].shift_points) >= abs(m.shifts[-1].shift_points)


class TestThinPopulation:
    def test_thin_axis_is_flagged(self) -> None:
        """prepositioning は実測 N=62→55。薄い軸で構成比を語らせない。"""
        rows = _rows(("s", "p1", "rare"), ("s", "p2", "rare"))

        m = measure_composition(rows, thin_threshold=10)

        assert m.is_thin is True

    def test_sufficient_population_is_not_thin(self) -> None:
        rows = _rows(*[("s", "p1", "common")] * 20, *[("s", "p2", "common")] * 20)

        assert measure_composition(rows, thin_threshold=10).is_thin is False


class TestDegenerateInputs:
    def test_empty_input_does_not_raise(self) -> None:
        m = measure_composition([])

        assert m.shifts == ()
        assert m.cohort.feeds == 0
        assert m.cohort.article_share == 0.0
        assert m.is_thin is True

    def test_window_with_no_cohort_articles_does_not_divide_by_zero(self) -> None:
        """p2 にコホート記事が無い — share は 0 に倒す (NaN や例外にしない)。"""
        rows = _rows(("s", "p1", "a"), ("s", "p2", ""), ("other", "p2", "a"))

        m = measure_composition(rows)

        assert all(s.p2_share == 0.0 for s in m.shifts)

    def test_blank_values_are_excluded(self) -> None:
        """未判定 (空文字) を 1 つの軸値として数えない — 構成比が歪む。"""
        rows = _rows(("s", "p1", "a"), ("s", "p1", ""), ("s", "p2", "a"), ("s", "p2", ""))

        m = measure_composition(rows)

        assert [s.value for s in m.shifts] == ["a"]


class TestObservationBiasMaterials:
    """観測バイアス仮説を **判定する**ための材料が揃っていること。"""

    def test_measurement_exposes_feed_counts_per_window(self) -> None:
        rows = _rows(
            ("s", "p1", "a"),
            ("s", "p2", "a"),
            ("only_p1", "p1", "a"),
            ("only_p2", "p2", "a"),
        )

        m = measure_composition(rows)

        assert m.cohort.p1_feeds_total == 2
        assert m.cohort.p2_feeds_total == 2
        assert m.cohort.feeds == 1

    def test_net_growth_is_derivable(self) -> None:
        """「網が広がった」が数字で言えること — 言えないと仮説を棄却も採択もできない。"""
        rows = _rows(
            ("s", "p1", "a"),
            ("s", "p2", "a"),
            ("new1", "p2", "a"),
            ("new2", "p2", "a"),
        )

        m = measure_composition(rows)

        assert m.cohort.p2_feeds_total - m.cohort.p1_feeds_total == 2
