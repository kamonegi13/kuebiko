"""集約シグナルを ACH へ供給する seam — 蓄積から答えを組み立てるための材料。

利用者指摘 (2026-09-16):「一記事で答えてもいい。ただしそれだけではダメ」。
PIR の答えは**蓄積から組み立てられ**、単一の記事はそれを更新する。既存の A 型 (事前配置)
はこれを 2 本腕の証拠規則で実現している — 直接 (R1/R3、1 本で決定的) と間接 (R2、蓄積して
初めて意味を持つ)。

しかし**集約の側が判定に届いていなかった**: `measure_composition` (段B-2b) は構成比を
算出できるのに、ACH のプロンプトへ渡る経路が無く、判定は「証拠記事を数本読んだ印象」に
なっていた。ここでその経路を作る。

不変条件:
1. **LLM に数えさせない** — 数値はコードが算出し、プロンプトは「そのまま使え」と言う
   (数えさせると収集量が判定を駆動する = CLAUDE.md §7 の禁止)
2. 観測バイアス仮説を**判定するための素性**を必ず併記する (コホート・網の規模)
3. コホートが除去できるのは我々の網の成長だけ、という限界を明記する
4. 母集団が薄いときは薄いと言う (黙って構成比を語らない)
"""

from __future__ import annotations

from src.assessment.aggregate_signal import format_aggregate_signal
from src.assessment.composition import AxisRow, CompositionMeasurement, measure_composition


def _rows(*specs: tuple[str, str, str]) -> list[AxisRow]:
    return [AxisRow(feed_title=f, window=w, value=v) for f, w, v in specs]


def _healthy() -> CompositionMeasurement:
    rows = _rows(
        *[("s1", "p1", "espionage")] * 40,
        *[("s1", "p1", "financial")] * 60,
        *[("s2", "p2", "espionage")] * 60,
        *[("s2", "p2", "financial")] * 40,
        *[("s1", "p2", "espionage")] * 1,
        *[("s2", "p1", "financial")] * 1,
        *[("new_feed", "p2", "financial")] * 30,
    )
    return measure_composition(rows)


class TestLlmIsToldNotToCount:
    def test_block_forbids_the_model_from_counting(self) -> None:
        text = format_aggregate_signal(_healthy(), p1_label="前期", p2_label="直近")

        assert "数えない" in text

    def test_block_is_labelled_as_deterministic(self) -> None:
        """「コードが算出した事実」と明示する (LLM の推測と混ぜない)。"""
        text = format_aggregate_signal(_healthy(), p1_label="前期", p2_label="直近")

        assert "決定論" in text or "コード" in text


class TestObservationBiasMaterials:
    def test_cohort_size_and_share_are_stated(self) -> None:
        text = format_aggregate_signal(_healthy(), p1_label="前期", p2_label="直近")

        assert "コホート" in text
        assert "%" in text

    def test_net_growth_is_stated(self) -> None:
        """網が増減したことを数字で出す — 出さないと観測仮説を検討できない。"""
        text = format_aggregate_signal(_healthy(), p1_label="前期", p2_label="直近")

        assert "媒体" in text

    def test_limit_of_the_cohort_is_stated(self) -> None:
        """除去できるのは我々の網の成長だけ、という限界を毎回書く。"""
        text = format_aggregate_signal(_healthy(), p1_label="前期", p2_label="直近")

        assert "報道量" in text


class TestThinPopulation:
    def test_thin_population_is_declared(self) -> None:
        rows = _rows(("s", "p1", "a"), ("s", "p2", "a"))

        text = format_aggregate_signal(
            measure_composition(rows, thin_threshold=30), p1_label="前期", p2_label="直近"
        )

        assert "薄" in text

    def test_thin_block_warns_against_asserting_a_trend(self) -> None:
        rows = _rows(("s", "p1", "a"), ("s", "p2", "a"))

        text = format_aggregate_signal(
            measure_composition(rows, thin_threshold=30), p1_label="前期", p2_label="直近"
        )

        assert "構成比" in text


class TestShiftRendering:
    def test_shifts_are_shown_as_percentage_points(self) -> None:
        text = format_aggregate_signal(_healthy(), p1_label="前期", p2_label="直近")

        assert "pt" in text

    def test_counts_are_shown_alongside_shares_for_provenance(self) -> None:
        """比だけだと N がわからず、薄い軸の揺れを本物と読む。件数も併記する。"""
        text = format_aggregate_signal(_healthy(), p1_label="前期", p2_label="直近")

        assert "件" in text

    def test_window_labels_appear(self) -> None:
        text = format_aggregate_signal(_healthy(), p1_label="6月", p2_label="8月")

        assert "6月" in text
        assert "8月" in text


class TestEmptyInput:
    def test_no_data_yields_empty_block_not_a_misleading_one(self) -> None:
        """データが無いときに空の表を出さない (空表は「変化なし」と誤読される)。"""
        assert format_aggregate_signal(measure_composition([]), p1_label="a", p2_label="b") == ""


class TestWindowSplit:
    """2 窓の切り方 — 現在窓 vs その直前の同じ長さの窓。"""

    def test_windows_are_equal_length_and_adjacent(self) -> None:
        from datetime import UTC, datetime

        from src.assessment.aggregate_signal import split_windows

        now = datetime(2026, 9, 16, tzinfo=UTC)
        w = split_windows(now=now, window_days=45)

        assert (w.p2_end - w.p2_start).days == 45
        assert (w.p1_end - w.p1_start).days == 45
        assert w.p1_end == w.p2_start

    def test_labels_are_dates_not_relative_words(self) -> None:
        """「前期」では後から再現できない — 実日付を出す。"""
        from datetime import UTC, datetime

        from src.assessment.aggregate_signal import split_windows

        w = split_windows(now=datetime(2026, 9, 16, tzinfo=UTC), window_days=45)

        assert "2026-" in w.p1_label
        assert "2026-" in w.p2_label


class TestBoundaryStaysIntact:
    def test_backbone_still_cannot_reach_composition(self) -> None:
        """集約 seam を足しても、背骨からは届かないこと。"""
        import ast
        from pathlib import Path

        backbone = ("src/pir", "src/tools", "src/cti", "src/taxonomy")
        offenders = []
        for py in Path("src").rglob("*.py"):
            if not str(py).startswith(backbone):
                continue
            tree = ast.parse(py.read_text(encoding="utf-8"))
            mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
            if any(m.startswith("src.assessment.aggregate_signal") for m in mods):
                offenders.append(str(py))

        assert offenders == []


class TestRowCap:
    def test_cap_covers_a_45_day_window(self) -> None:
        """実測 8,815 件/45日窓。下回ると窓の古い側が落ちて実効期間が縮む。"""
        from src.assessment.aggregate_signal import _ROW_CAP

        assert _ROW_CAP >= 8815


class TestBothEvaluationPathsReceiveIt:
    """⚠ 実際に起きた: 増分にだけ配線し**初回評価に渡し忘れた**。

    初回は最初の答えを決める場面で、蓄積が届かないと記事数本の印象で確度が決まる。
    実測では趨勢の問いが初回で「悪化・確度 high」になった (集約は +3.5pt で
    観測バイアスも未棄却だった)。
    """

    def test_first_evaluation_accepts_an_aggregate_signal(self) -> None:
        import inspect

        from src.synthesis.grounded.passes import ground_and_score

        assert "aggregate_signal" in inspect.signature(ground_and_score).parameters

    def test_incremental_evaluation_accepts_an_aggregate_signal(self) -> None:
        import inspect

        from src.synthesis.grounded.incremental import incremental_ground_and_score

        assert "aggregate_signal" in inspect.signature(incremental_ground_and_score).parameters

    def test_both_prompts_render_the_block(self) -> None:
        """片方のプロンプトだけに入れると、その経路だけ蓄積が消える。"""
        from pathlib import Path

        for f in (
            "prompts/synthesis/ground_ach.j2",
            "prompts/synthesis/ground_ach_skeleton.j2",
            "prompts/synthesis/ground_incremental.j2",
            "prompts/synthesis/ground_incremental_skeleton.j2",
        ):
            assert "aggregate_signal" in Path(f).read_text(encoding="utf-8"), f


class TestAggregateConfidenceCap:
    """集約を**確度の上限**として構造的に効かせる (段B-3h)。

    ⚠ 実測 2026-09-16: 集約を散文でプロンプトに載せても ACH の集計に席が無く、
    観測系仮説が 0/0 (未評価) のまま `threshold_crossed` / 確度 high が出た。
    中国の集約はむしろ逆 (事前配置 19.2%→10.1%) を示していた。
    「散文指示は無効・構造のみ有効」(2026-08-27) を自分で踏んだ。

    → leading は変えない (ACH の証拠駆動判定を尊重)。**変化の主張をする leading の
    確度に上限をかける**。既存の `final_confidence` と同じ方向中立の思想。
    """

    def _measurement(self, *specs: tuple[str, str, str], thin: int = 30) -> CompositionMeasurement:
        return measure_composition(_rows(*specs), thin_threshold=thin)

    def _moved(self, value: str, *, p1: int, p2: int, other: int = 100) -> CompositionMeasurement:
        rows = _rows(
            *[("s", "p1", value)] * p1,
            *[("s", "p1", "other")] * other,
            *[("s", "p2", value)] * p2,
            *[("s", "p2", "other")] * other,
        )
        return measure_composition(rows)

    def test_no_cap_when_leading_asserts_no_change(self) -> None:
        """「平時の変動内」「横ばい」は集約の支持を要しない (fail-closed の既定)。"""
        from src.assessment.aggregate_signal import aggregate_confidence_cap

        m = self._moved("disruption", p1=50, p2=10)

        assert aggregate_confidence_cap(m, leading="threshold_within_normal", threat=None) is None
        assert aggregate_confidence_cap(m, leading="trend_flat", threat="disruption") is None

    def test_worsening_is_capped_when_the_share_fell(self) -> None:
        """集約が逆を示すのに「悪化」と言うなら確度を落とす。"""
        from src.assessment.aggregate_signal import aggregate_confidence_cap

        m = self._moved("disruption", p1=50, p2=10)

        cap = aggregate_confidence_cap(m, leading="trend_worsening", threat="disruption")

        assert cap is not None
        assert cap[0] == "low"

    def test_improving_is_capped_when_the_share_rose(self) -> None:
        """方向中立 — 穏当な側の主張も同じ規律で抑える。"""
        from src.assessment.aggregate_signal import aggregate_confidence_cap

        m = self._moved("disruption", p1=10, p2=50)

        cap = aggregate_confidence_cap(m, leading="trend_improving", threat="disruption")

        assert cap is not None
        assert cap[0] == "low"

    def test_worsening_is_not_capped_when_the_share_rose(self) -> None:
        """集約が支持しているなら抑えない。"""
        from src.assessment.aggregate_signal import aggregate_confidence_cap

        m = self._moved("disruption", p1=10, p2=50)

        assert aggregate_confidence_cap(m, leading="trend_worsening", threat="disruption") is None

    def test_thin_population_caps_any_change_claim(self) -> None:
        from src.assessment.aggregate_signal import aggregate_confidence_cap

        m = self._measurement(("s", "p1", "a"), ("s", "p2", "a"))

        cap = aggregate_confidence_cap(m, leading="threshold_crossed", threat=None)

        assert cap is not None
        assert cap[0] == "low"
        assert "薄" in cap[1]

    def test_threshold_uses_escalatory_intents_when_no_threat_slot(self) -> None:
        """閾値の問いは主体の活動の**性格**で見る (事前配置・破壊の比率)。"""
        from src.assessment.aggregate_signal import aggregate_confidence_cap

        m = self._moved("prepositioning", p1=50, p2=10)

        cap = aggregate_confidence_cap(m, leading="threshold_crossed", threat=None)

        assert cap is not None
        assert cap[0] == "low"

    def test_reason_names_the_numbers_for_audit(self) -> None:
        """後から「なぜ抑えたか」を追えること。"""
        from src.assessment.aggregate_signal import aggregate_confidence_cap

        m = self._moved("disruption", p1=50, p2=10)
        cap = aggregate_confidence_cap(m, leading="trend_worsening", threat="disruption")

        assert cap is not None
        assert "pt" in cap[1]

    def test_no_measurement_means_no_cap(self) -> None:
        """集約が出せないことを根拠に確度を下げない (無知は証拠ではない)。"""
        from src.assessment.aggregate_signal import aggregate_confidence_cap

        assert aggregate_confidence_cap(None, leading="trend_worsening", threat="x") is None
