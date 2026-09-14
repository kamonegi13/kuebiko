"""状況総括 render の CoT 欄 (analysis_notes) と schema 全必須化の不変量。

背景: ``_WireSections`` は全欄に既定値があり required がゼロだった = Ollama の制約
デコードで**省略が文法上許され後発の欄から静かに落ちる**条件そのもの (eventnews /
judgment で実測済み)。CoT 欄を足すついでに同じ手当を当てる。

CoT 欄は **既定 OFF** (``SYNTHESIS_COT_NOTES=1`` で有効)。本番の narrative は CoT を
学習していないモデルが担当しているため、教師収穫と、CoT を学習した生徒の配備まで
本番の出力形を変えない。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest

from src.synthesis.grounded.estimate import Estimate, EvidenceItem, HypothesisScore, KeyJudgment
from src.synthesis.grounded.render import (
    _WireSections,
    _WireSectionsCoT,
    build_render_plan,
    render_sections,
)

_OK_HEADLINE = (
    "ランサムウェアの標的が重要インフラ部門へ拡大している動きを新たに確認した"
    "(金銭目的の犯罪である可能性が高い)。"
    "多部門への展開速度から、国内関連組織でも同種の侵入試行への警戒を高めるべき局面にある。"
)


def _estimate() -> Estimate:
    judgment = KeyJudgment(
        id="j1",
        claim="悪用が拡大した",
        domain="cyber",
        leading_hypothesis="criminal_financial",
        confidence="moderate",
        confidence_basis="vendor_confirmed",
        hypotheses=(
            HypothesisScore(
                hypothesis="criminal_financial", consistent=2, inconsistent=0, verdict="leading"
            ),
        ),
        evidence=(
            EvidenceItem(
                article_id="a1",
                source_tier="vendor",
                attribution_basis="vendor_confirmed",
                excerpt="抜粋",
                polarity="supports",
            ),
        ),
        delta_type="escalated",
        delta_note="被害の拡大を観測",
    )
    return Estimate(
        period_type="daily",
        period_start=datetime(2026, 9, 1, tzinfo=UTC),
        period_end=datetime(2026, 9, 2, tzinfo=UTC),
        judgments=(judgment,),
    )


class RecordingLLM:
    """呼出時の schema を捕獲する fake (どちらの schema が渡ったかが本テストの主眼)。"""

    def __init__(self) -> None:
        self.model = "fake"
        self.schema: type | None = None
        self.prompt = ""

    async def generate_structured(self, prompt: str, schema: type, **kw: Any) -> Any:
        self.prompt = prompt
        self.schema = schema
        return schema(headline=_OK_HEADLINE)


class TestSchemaRequired:
    def test_sections_schema_requires_every_property(self) -> None:
        schema = _WireSections.model_json_schema()
        assert sorted(schema["required"]) == sorted(schema["properties"])

    def test_cot_schema_requires_every_property(self) -> None:
        schema = _WireSectionsCoT.model_json_schema()
        assert sorted(schema["required"]) == sorted(schema["properties"])

    def test_analysis_notes_is_the_first_property(self) -> None:
        """制約デコードは properties 順に生成する = 思考は本文より**前**でなければ意味がない。"""
        assert list(_WireSectionsCoT.model_json_schema()["properties"])[0] == "analysis_notes"

    def test_cot_schema_carries_the_same_sections(self) -> None:
        base = set(_WireSections.model_json_schema()["properties"])
        cot = set(_WireSectionsCoT.model_json_schema()["properties"])
        assert cot - base == {"analysis_notes"}
        assert base - cot == set()


class TestPromptPlan:
    def test_prompt_omits_cot_instruction_by_default(self) -> None:
        plan = build_render_plan(est=_estimate(), period_label="L")
        assert "analysis_notes" not in plan.prompt
        assert plan.head is not None
        assert plan.mode == "moved"

    def test_prompt_includes_cot_instruction_when_enabled(self) -> None:
        plan = build_render_plan(est=_estimate(), period_label="L", cot_notes=True)
        assert "analysis_notes" in plan.prompt
        assert "出典の上流依存" in plan.prompt


class TestRenderSections:
    @pytest.mark.asyncio
    async def test_uses_plain_schema_by_default(self) -> None:
        llm = RecordingLLM()
        out = await render_sections(llm=llm, est=_estimate(), period_label="L")  # type: ignore[arg-type]
        assert llm.schema is _WireSections
        assert out.headline == _OK_HEADLINE

    @pytest.mark.asyncio
    async def test_uses_cot_schema_when_env_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SYNTHESIS_COT_NOTES", "1")
        llm = RecordingLLM()
        out = await render_sections(llm=llm, est=_estimate(), period_label="L")  # type: ignore[arg-type]
        assert llm.schema is _WireSectionsCoT
        # 下流 (射影・保存) は CoT 欄を知らない = 返り値は常に _WireSections
        assert type(out) is _WireSections
        assert out.headline == _OK_HEADLINE


class TestMovedWidthGuard:
    """報告の幅 (本文に載せる「変化した判定」の件数) の有界化。

    台帳の更新上限 (`_MAX_UPDATES_BY_PERIOD`) が報告の幅を兼ねていたため、台帳を
    26B 実測に合わせて広げると報告まで一緒に広がってしまう。両者は別の要求なので
    render 側で別に有界化する。**落とした件数は必ずプロンプトに書く** (no silent caps)。

    実測 (2026-09-15、過去 73 窓): daily の moved は中央 5 件・上限 12 が効く窓は
    1 件のみ = 通常は非発動の安全網。weekly/monthly は幅の設計が別途必要 (中央 92 件)
    なので**対象外** — ここで一律 12 に切ると週次の報告が壊れる。
    """

    def _estimate_with_moved(self, n: int, period_type: str = "daily") -> Estimate:
        """n 件の moved 判定を持つ Estimate。末尾ほど salience が低くなるよう証拠を減らす。"""
        base = _estimate().judgments[0]
        judgments = tuple(
            replace(
                base,
                id=f"j{i}",
                claim=f"変化した判定 {i}",
                delta_type="escalated",
                # 末尾 (= 落とされる側) に固有の PIR を付け、ロールアップ側の残存を検証する
                pir_ids=("pir_tail",) if i >= 12 else ("pir_head",),
                evidence=base.evidence if i < 12 else (),
            )
            for i in range(n)
        )
        return replace(_estimate(), judgments=judgments, period_type=period_type)

    def test_daily_moved_is_capped_and_the_omission_is_stated(self) -> None:
        plan = build_render_plan(est=self._estimate_with_moved(15), period_label="L")
        assert plan.prompt.count("【拡大】") == 12
        assert "3 件" in plan.prompt  # 落とした件数を本文に書く

    def test_daily_under_the_cap_is_unchanged(self) -> None:
        plan = build_render_plan(est=self._estimate_with_moved(5), period_label="L")
        assert plan.prompt.count("【拡大】") == 5
        assert "本文に載せない" not in plan.prompt

    def test_weekly_is_not_capped(self) -> None:
        """週次は判定 中央 92 件を軌跡として載せる設計。ここで切ると報告が壊れる。"""
        plan = build_render_plan(est=self._estimate_with_moved(15, "weekly"), period_label="L")
        assert plan.prompt.count("【拡大】") == 15

    def test_pir_rollup_keeps_the_omitted_judgments(self) -> None:
        """幅を切っても PIR 対応は**全判定**から作る (関心領域そのものは消えない)。

        これが成り立つので daily に PIR 別の最低保証を入れていない (実測でも 73 窓中
        1 窓しか取りこぼしが起きなかった)。
        """
        plan = build_render_plan(est=self._estimate_with_moved(15), period_label="L")
        body, _, pir_section = plan.prompt.partition("【PIR 対応")
        assert "pir_tail" not in body  # 本文の変化セクションからは落ちている
        assert "pir_tail" in pir_section  # PIR ロールアップには残る

    def test_headline_judgment_is_never_cut_from_the_moved_section(self) -> None:
        """指名判定 (接地ゲート通過の最上位) が噂クラスの上位に押し出されても本文に残る。"""
        base = _estimate().judgments[0]
        rumors = tuple(
            replace(
                base,
                id=f"r{i}",
                claim=f"未実証の主張 {i}",
                delta_type="hypothesis_flip",  # delta 3.0 で salience が高い
                leading_hypothesis="unverified_or_false",  # 噂クラス = headline 不可
                japan_related=True,
                confidence="high",
                domain="cyber_incident",
                pir_ids=("pir_china_apt",),  # 最優先 PIR boost (salience 7.7 対 3.2)
            )
            for i in range(12)
        )
        grounded = replace(
            base, id="g", claim="接地された変化", delta_type="strengthened", confidence="moderate"
        )
        est = replace(_estimate(), judgments=(*rumors, grounded))
        plan = build_render_plan(est=est, period_label="L")
        assert plan.head is not None and plan.head.id == "g"
        body, _, _ = plan.prompt.partition("【継続中の判定】")
        assert "[g]" in body
        assert plan.prompt.count("【") >= 12  # 幅は cap のまま (入替であって追加ではない)
