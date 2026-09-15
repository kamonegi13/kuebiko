"""weekly / monthly 状況総括の「報告の幅」の不変量 (2026-09-15)。

問題 (実測): weekly は期間内に revision を持つ全 Situation を軌跡判定として**全件**載せる
ため、判定 中央 92 件 / プロンプト 40.2k tok を出力上限 6,000 tok (≒4,000 字) に押し込んで
いた = 1 判定あたり 43 字。分析ではなく列挙になる構造的原因。

設計 (docs/synthesis_weekly_width_design.md): 全件を「扱う」が全件を「書かせない」。
- A 本文: salience 上位 N + **PIR 別の最低 1 件保証** (weekly は上位 12 で切ると測定 11 窓
  すべてで PIR が落ちるため。daily は 73 窓中 1 窓だけなので保証を入れない = 非対称)
- B 一覧: 残りを 1 行 (証拠抜粋なし ≒ 40 tok/件)
- C 件数: B の上限を超えた分は件数だけ (no-silent-caps)
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from src.synthesis.grounded.estimate import (
    Estimate,
    EvidenceItem,
    HypothesisScore,
    KeyJudgment,
)
from src.synthesis.grounded.render import build_render_plan


def _judgment(i: int, **over: object) -> KeyJudgment:
    base = KeyJudgment(
        id=f"j{i:03d}",
        claim=f"変化した判定 {i}",
        domain="cyber_incident",
        leading_hypothesis="criminal_financial",
        confidence="moderate",
        confidence_basis="b",
        hypotheses=(
            HypothesisScore(
                hypothesis="criminal_financial", consistent=1, inconsistent=0, verdict="leading"
            ),
        ),
        evidence=(
            EvidenceItem(
                article_id=f"a{i}",
                source_tier="vendor",
                attribution_basis="vendor_confirmed",
                excerpt=f"根拠抜粋 {i}",
                polarity="supports",
            ),
        ),
        delta_type="escalated",
    )
    return replace(base, **over)  # type: ignore[arg-type]


def _estimate(judgments: tuple[KeyJudgment, ...], period_type: str = "weekly") -> Estimate:
    now = datetime(2026, 9, 7, tzinfo=UTC)
    return Estimate(
        period_type=period_type,
        period_start=now,
        period_end=now,
        judgments=judgments,
    )


class TestThreeLayers:
    def test_weekly_splits_into_body_list_and_count(self) -> None:
        est = _estimate(tuple(_judgment(i) for i in range(90)))

        plan = build_render_plan(est=est, period_label="L")

        body, _, rest = plan.prompt.partition("【そのほかの動き")
        # A 本文: 上位 12 件だけが完全な形 (根拠抜粋つき) で載る
        assert body.count("根拠(要点)") == 12
        # B 一覧: 1 行形で 60 件、抜粋は載せない
        listing, _, tail = rest.partition("ほかに変化した判定")
        assert listing.count("【拡大】") == 60
        assert "根拠抜粋" not in listing
        # C 件数: 残り 18 件は件数だけ (90 - 12 - 60)
        assert "18 件" in tail

    def test_monthly_uses_its_own_width(self) -> None:
        est = _estimate(tuple(_judgment(i) for i in range(40)), period_type="monthly")

        plan = build_render_plan(est=est, period_label="L")

        assert plan.prompt.count("根拠(要点)") == 15
        assert "【そのほかの動き" in plan.prompt

    def test_short_weekly_has_no_list_or_count(self) -> None:
        """判定が少ない週は現行どおり (層分けの痕跡を出さない)。"""
        est = _estimate(tuple(_judgment(i) for i in range(5)))

        plan = build_render_plan(est=est, period_label="L")

        assert plan.prompt.count("根拠(要点)") == 5
        assert "【そのほかの動き" not in plan.prompt
        assert "ほかに変化した判定" not in plan.prompt

    def test_daily_keeps_count_only_behaviour(self) -> None:
        """daily は 1 行一覧を持たない (moved 中央 5 件で不要 — 09-15 実測)。"""
        est = _estimate(tuple(_judgment(i) for i in range(20)), period_type="daily")

        plan = build_render_plan(est=est, period_label="L")

        assert plan.prompt.count("根拠(要点)") == 12
        assert "【そのほかの動き" not in plan.prompt
        assert "8 件" in plan.prompt


class TestPirGuarantee:
    def test_pir_only_in_low_salience_judgment_is_pulled_into_the_body(self) -> None:
        """上位 12 に無い PIR は、その PIR を持つ最上位判定を A 層へ引き上げる。"""
        judgments = [_judgment(i, pir_ids=("pir_common",)) for i in range(30)]
        # salience 最下位 (証拠なし = 認識論的重みは同じだが id 順で後ろ) に固有 PIR を置く
        judgments[29] = _judgment(29, pir_ids=("pir_rare",))
        est = _estimate(tuple(judgments))

        plan = build_render_plan(est=est, period_label="L")

        body, _, _ = plan.prompt.partition("【そのほかの動き")
        assert "[j029]" in body  # 保証で引き上げられた
        assert body.count("根拠(要点)") == 13  # 上位 12 + 保証 1

    def test_guarantee_is_bounded(self) -> None:
        """PIR が多数落ちても A 層は上限で止める (幅が無限に伸びない)。"""
        judgments = [_judgment(i, pir_ids=(f"pir_{i}",)) for i in range(30)]
        est = _estimate(tuple(judgments))

        plan = build_render_plan(est=est, period_label="L")

        body, _, _ = plan.prompt.partition("【そのほかの動き")
        assert body.count("根拠(要点)") == 18  # 上位 12 + 保証上限 6

    def test_guarantee_does_not_apply_to_daily(self) -> None:
        judgments = [_judgment(i, pir_ids=(f"pir_{i}",)) for i in range(20)]
        est = _estimate(tuple(judgments), period_type="daily")

        plan = build_render_plan(est=est, period_label="L")

        assert plan.prompt.count("根拠(要点)") == 12

    def test_pir_rollup_still_covers_everything(self) -> None:
        """A/B/C のどこに落ちても PIR 対応は全判定から作る (関心領域は消えない)。"""
        judgments = [_judgment(i, pir_ids=("pir_common",)) for i in range(90)]
        judgments[89] = _judgment(89, pir_ids=("pir_tail",))
        est = _estimate(tuple(judgments))

        plan = build_render_plan(est=est, period_label="L")

        _, _, rollup = plan.prompt.partition("【PIR 対応")
        assert "pir_tail" in rollup
