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


class TestImportanceDrivenWidth:
    """A 層は固定 N でなく**重要度基準** — 重要な事象が多い期間は本文も長くなる。"""

    def test_body_grows_with_the_number_of_important_judgments(self) -> None:
        """全件が同格に重要なら、上限まで全部 A 層に入る (下限 12 で止まらない)。"""
        est = _estimate(tuple(_judgment(i) for i in range(90)))

        plan = build_render_plan(est=est, period_label="L")

        assert plan.body_count == 40  # 上限 (安全弁)
        assert plan.prompt.count("根拠(要点)") == 40

    def test_body_shrinks_to_the_floor_when_only_a_few_matter(self) -> None:
        """重要度が突出した 1 件 + 些末な多数 → A 層は下限まで縮む (薄く広げない)。"""
        judgments = [
            _judgment(0, delta_type="hypothesis_flip", confidence="high", japan_related=True),
            *[_judgment(i, delta_type="claim_revised", confidence="low") for i in range(1, 60)],
        ]
        est = _estimate(tuple(judgments))

        plan = build_render_plan(est=est, period_label="L")

        assert plan.body_count == 12  # 下限
        assert plan.listed_count == 48

    def test_weekly_splits_into_body_list_and_count(self) -> None:
        judgments = [
            _judgment(0, delta_type="hypothesis_flip", confidence="high", japan_related=True),
            *[_judgment(i, delta_type="claim_revised", confidence="low") for i in range(1, 90)],
        ]
        est = _estimate(tuple(judgments))

        plan = build_render_plan(est=est, period_label="L")

        body, _, rest = plan.prompt.partition("【そのほかの動き")
        assert body.count("根拠(要点)") == 12  # A 層 (下限)
        listing, _, tail = rest.partition("ほかに変化した判定")
        assert listing.count("【更新】") == 60  # B 層 (上限)
        assert "根拠抜粋" not in listing
        assert "18 件" in tail  # C 層 = 90 - 12 - 60

    def test_monthly_uses_its_own_floor(self) -> None:
        judgments = [
            _judgment(0, delta_type="hypothesis_flip", confidence="high", japan_related=True),
            *[_judgment(i, delta_type="claim_revised", confidence="low") for i in range(1, 40)],
        ]
        est = _estimate(tuple(judgments), period_type="monthly")

        plan = build_render_plan(est=est, period_label="L")

        assert plan.body_count == 15  # monthly の下限
        assert "【そのほかの動き" in plan.prompt

    def test_output_budget_scales_with_the_period(self) -> None:
        """文数指示は period 別 (判定が多い期間ほど総括も長い)。"""
        daily = build_render_plan(est=_estimate((_judgment(1),), "daily"), period_label="L")
        weekly = build_render_plan(est=_estimate((_judgment(1),), "weekly"), period_label="L")

        assert "2-5 文" in daily.prompt
        assert "3-8 文" in weekly.prompt

    def test_weight_section_is_tied_to_the_judgment_count(self) -> None:
        """「各セクション N 文」が「各判定 1-2 文」を握りつぶしていた矛盾の解消 (実測: weekly は
        判定 92 件に対し weight_section が 9 文しか書かれていなかった)。"""
        est = _estimate(tuple(_judgment(i) for i in range(30)))

        plan = build_render_plan(est=est, period_label="L")

        assert "30 件すべて" in plan.prompt
        assert "1 件あたり 1-2 文" in plan.prompt

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
        """A 層に無い PIR は、その PIR を持つ最上位判定を引き上げる。"""
        judgments = [
            _judgment(0, delta_type="hypothesis_flip", confidence="high", japan_related=True),
            *[
                _judgment(i, delta_type="claim_revised", confidence="low", pir_ids=("pir_common",))
                for i in range(1, 30)
            ],
        ]
        judgments[29] = _judgment(
            29, delta_type="claim_revised", confidence="low", pir_ids=("pir_rare",)
        )
        est = _estimate(tuple(judgments))

        plan = build_render_plan(est=est, period_label="L")

        body, _, _ = plan.prompt.partition("【そのほかの動き")
        assert "[j029]" in body  # 保証で引き上げられた
        assert plan.body_count == 13  # A 層 12 (下限) + 保証 1

    def test_guarantee_is_bounded(self) -> None:
        """PIR が多数落ちても保証で足す件数は上限で止める (幅が無限に伸びない)。"""
        judgments = [
            _judgment(0, delta_type="hypothesis_flip", confidence="high", japan_related=True),
            *[
                _judgment(i, delta_type="claim_revised", confidence="low", pir_ids=(f"pir_{i}",))
                for i in range(1, 30)
            ],
        ]
        est = _estimate(tuple(judgments))

        plan = build_render_plan(est=est, period_label="L")

        assert plan.body_count == 18  # A 層 12 (下限) + 保証上限 6

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


class TestStandingWidth:
    """継続中の判定 (standing) も moved と同じ重要度基準で絞る (2026-09-15)。

    weekly の軌跡射影は no_change の判定をそのまま載せ、実測で 22 件 = 3.2k tok を
    占めていた。moved を重要度基準にした以上、ここだけ無上限なのは一貫しない。
    """

    def _with_standing(self, n: int, period_type: str = "weekly") -> Estimate:
        moved = _judgment(0, delta_type="escalated", confidence="high", japan_related=True)
        standing = tuple(
            _judgment(i, delta_type="no_change", confidence="low") for i in range(1, n + 1)
        )
        return _estimate((moved, *standing), period_type)

    def test_weekly_standing_is_bounded_and_the_omission_is_stated(self) -> None:
        plan = build_render_plan(est=self._with_standing(30), period_label="L")

        section, _, tail = plan.prompt.partition("【判定間の関係】")
        assert section.count("【継続】") == 12  # 上限
        assert "18 件" in section  # 落とした件数を明記 (30 - 12)

    def test_standing_keeps_the_floor_when_few_are_important(self) -> None:
        """重要度が割れていても下限 3 件は残す (継続の視界をゼロにしない)。"""
        moved = _judgment(0, delta_type="escalated", confidence="high", japan_related=True)
        standing = (
            _judgment(1, delta_type="no_change", confidence="high", japan_related=True),
            *[_judgment(i, delta_type="no_change", confidence="low") for i in range(2, 20)],
        )
        plan = build_render_plan(est=_estimate((moved, *standing)), period_label="L")

        section, _, _ = plan.prompt.partition("【判定間の関係】")
        assert section.count("【継続】") == 3

    def test_daily_standing_is_unchanged(self) -> None:
        """daily は上流 (_FALLBACK_STANDING=3) で既に 3 件。render 側の上限は非発動。"""
        plan = build_render_plan(est=self._with_standing(3, "daily"), period_label="L")

        section, _, _ = plan.prompt.partition("【判定間の関係】")
        assert section.count("【継続】") == 3
        assert "ほかに継続中の判定" not in plan.prompt


class TestStandingSeedsAreNeverCut:
    """常設情報要求 (kind='standing') は幅の上限から除外する。

    「国家 N は日本の重要インフラへの事前配置を進めているか」のような常設の問いは、台帳でも
    dormant/close の対象外 (静穏期間こそ問いが生きる = 「静か≠安全」)。静かな週ほど salience が
    下がるため、幅の上限に任せると**最も見えているべき週に消える**。
    """

    def test_quiet_standing_seed_survives_the_standing_cap(self) -> None:
        from src.assessment.standing import STANDING_SEEDS

        seed_id = STANDING_SEEDS[0].situation_id
        moved = _judgment(0, delta_type="escalated", confidence="high", japan_related=True)
        loud = [
            _judgment(i, delta_type="no_change", confidence="high", japan_related=True)
            for i in range(1, 20)
        ]
        quiet_seed = replace(_judgment(99, delta_type="no_change", confidence="low"), id=seed_id)

        plan = build_render_plan(est=_estimate((moved, *loud, quiet_seed)), period_label="L")

        section, _, _ = plan.prompt.partition("【判定間の関係】")
        assert f"[{seed_id}]" in section

    def test_quiet_standing_seed_survives_the_moved_cap(self) -> None:
        from src.assessment.standing import STANDING_SEEDS

        seed_id = STANDING_SEEDS[0].situation_id
        loud = [
            _judgment(i, delta_type="hypothesis_flip", confidence="high", japan_related=True)
            for i in range(60)
        ]
        quiet_seed = replace(
            _judgment(99, delta_type="claim_revised", confidence="low"), id=seed_id
        )

        plan = build_render_plan(est=_estimate((*loud, quiet_seed)), period_label="L")

        body, _, _ = plan.prompt.partition("【そのほかの動き")
        assert f"[{seed_id}]" in body
