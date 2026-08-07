"""段C: salience 決定論ランキングと delta render 射影のテスト。

headline 占有問題 (kinetic 戦況が CTI ブリーフ筆頭を取る) の構造的解決を回帰固定する。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from src.assessment.salience import pick_headline, rank_judgments, salience
from src.synthesis.grounded.estimate import Estimate, KeyJudgment
from src.synthesis.grounded.render import _pir_rollup, _WireSections, render_sections


def _j(**kw: Any) -> KeyJudgment:
    base: dict[str, Any] = {
        "id": "j1",
        "claim": "claim",
        "domain": "cyber_incident",
        "leading_hypothesis": "organized_state_op",
        "confidence": "moderate",
        "confidence_basis": "",
        "hypotheses": (),
        "evidence": (),
    }
    base.update(kw)
    return KeyJudgment(**base)


class TestSalience:
    def test_cyber_opened_outranks_military_escalated(self) -> None:
        # 実測病理の構造解決: キーウ攻撃 (military) が headline を占有しない
        cyber = _j(id="a", domain="cyber_incident", delta_type="opened")
        kinetic = _j(id="b", domain="military", delta_type="escalated")
        assert salience(cyber) > salience(kinetic)

    def test_japan_related_boosts(self) -> None:
        plain = _j(id="a")
        jp = _j(id="b", japan_related=True)
        assert salience(jp) > salience(plain)

    def test_pir_priority_max_not_count(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # L2 (2026-07-05): 件数でなく **最重要 PIR の優先度** で評価する。
        import src.assessment.salience as sal

        monkeypatch.setattr(sal, "_pir_priority_map", lambda: {"top": 1.0, "low": 0.1})
        none = _j(id="a")
        low = _j(id="b", pir_ids=("low",))
        top = _j(id="c", pir_ids=("top",))
        top_plus_junk = _j(id="d", pir_ids=("top", "low"))
        # 優先度が効く: top > low > none
        assert salience(top) > salience(low) > salience(none)
        # MAX なので promiscuous PIR を足しても最大優先度は変わらない (件数インフレ排除)
        assert salience(top_plus_junk) == salience(top)

    def test_moved_high_pir_outranks_moved_no_pir(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # L3: 同じ delta なら高優先 PIR にマッチした判定が headline を取る (乗算的増幅)
        import src.assessment.salience as sal

        monkeypatch.setattr(sal, "_pir_priority_map", lambda: {"china": 1.0})
        moved_top = _j(id="a", domain="cyber_incident", delta_type="opened", pir_ids=("china",))
        moved_no_pir = _j(id="b", domain="cyber_incident", delta_type="opened")
        head = pick_headline((moved_no_pir, moved_top))
        assert head is not None and head.id == "a"

    def test_standing_high_pir_still_loses_to_big_moved(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # L3: 高優先 PIR standing は押し上げられるが genuine な大 delta には負ける (陳腐化防止)
        import src.assessment.salience as sal

        monkeypatch.setattr(sal, "_pir_priority_map", lambda: {"china": 1.0})
        standing_top = _j(id="a", domain="geopolitical", delta_type="no_change", pir_ids=("china",))
        moved_cyber = _j(id="b", domain="cyber_incident", delta_type="opened")
        head = pick_headline((standing_top, moved_cyber))
        assert head is not None and head.id == "b"

    def test_headline_prefers_moved_over_standing(self) -> None:
        standing_high = _j(
            id="a", domain="cyber_incident", confidence="high", delta_type="no_change"
        )
        moved_low = _j(id="b", domain="cyber_incident", confidence="low", delta_type="opened")
        head = pick_headline((standing_high, moved_low))
        assert head is not None
        assert head.id == "b"  # 変化した判定を headline に据える

    def test_rank_is_deterministic_on_tie(self) -> None:
        a = _j(id="a")
        b = _j(id="b")
        assert [x.id for x in rank_judgments((b, a))] == ["a", "b"]


class TestHeadlineTwoStage:
    """pick_headline の 2 段選定 (2026-07-12): 接地された変化 > standing。

    旧単一ランキングの「変化優先は重みから創発する」仮定が実測で崩れた回帰を固定する
    (日本×最上位PIR×高確度 standing が 拡大・高確度 moved を連日抑え、変化がある日に
    quiet headline を出した)。
    """

    def test_grounded_moved_beats_top_pir_japan_standing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 実測で敗北していた構図: standing = 日本×最優先PIR×高確度 (relevance 最大級)
        import src.assessment.salience as sal

        monkeypatch.setattr(sal, "_pir_priority_map", lambda: {"pir_top": 1.0})
        standing = _j(
            id="a",
            confidence="high",
            delta_type="no_change",
            japan_related=True,
            pir_ids=("pir_top",),
        )
        moved = _j(id="b", confidence="high", delta_type="escalated")
        head = pick_headline((standing, moved))
        assert head is not None
        assert head.id == "b"  # 接地された変化が pool 分離で必ず筆頭

    def test_rumor_class_flip_falls_back_to_standing_quiet(self) -> None:
        # 2026-07-05 の門を保存: 噂クラスの変化しかない日は standing (quiet) が正
        standing = _j(id="a", confidence="high", delta_type="no_change")
        rumor = _j(
            id="b",
            confidence="low",
            delta_type="hypothesis_flip",
            leading_hypothesis="unverified_or_false",
        )
        head = pick_headline((standing, rumor))
        assert head is not None
        assert head.id == "a"

    def test_refuted_moved_does_not_get_pool_priority(self) -> None:
        # 反証済みの変化は grounded pool に入らない = pool 分離の優先は受けない。
        # fallback では従来どおり epistemic 減衰つきで競合する (ハード除外はしない)。
        standing = _j(id="a", confidence="high", delta_type="no_change", japan_related=True)
        refuted = _j(id="b", confidence="high", delta_type="escalated", adversarial_refuted=True)
        head = pick_headline((standing, refuted))
        assert head is not None
        assert head.id == "a"  # 減衰 (x0.6) により日本関連 standing が勝つ

    def test_moved_pool_is_ordered_by_salience(self) -> None:
        # pool 内の順位は salience 乗算のまま (認識論的重みが噂でない低確度を下げる)
        flip_low = _j(id="a", confidence="low", delta_type="hypothesis_flip")
        escalated_high = _j(id="b", confidence="high", delta_type="escalated")
        head = pick_headline((flip_low, escalated_high))
        assert head is not None
        assert head.id == "b"


class TestPirRollup:
    def test_groups_by_pir_and_orders_by_count(self) -> None:
        j1 = _j(id="a", claim="c1", pir_ids=("pir_x",))
        j2 = _j(id="b", claim="c2", pir_ids=("pir_x", "pir_y"))
        rollup = _pir_rollup((j1, j2))
        assert rollup[0]["pir_title"] in ("pir_x",)  # 件数順 (title 解決不可なら id)
        assert len(rollup[0]["entries"]) == 2

    def test_empty_without_pir_links(self) -> None:
        assert _pir_rollup((_j(id="a"),)) == []


# floor (70 字) を満たす contract 準拠の headline (変化+要点+含意+確度)
_OK_HEADLINE = (
    "ランサムウェアの標的が重要インフラ部門へ拡大している動きを新たに確認した"
    "(金銭目的の犯罪である可能性が高い)。"
    "多部門への展開速度から、国内関連組織でも同種の侵入試行への警戒を高めるべき局面にある。"
)


class FakeLLM:
    def __init__(self, headline: str = _OK_HEADLINE) -> None:
        self.model = "fake"
        self.last_prompt = ""
        self._headline = headline

    async def generate_structured(self, prompt: str, schema: type, **kw: Any) -> Any:
        self.last_prompt = prompt
        return _WireSections(headline=self._headline)


class TestRenderSections:
    @pytest.mark.asyncio
    async def test_prompt_groups_moved_and_standing_and_names_headline(self) -> None:
        est = Estimate(
            period_type="daily",
            period_start=datetime.now(UTC),
            period_end=datetime.now(UTC),
            judgments=(
                _j(
                    id="s-moved",
                    claim="悪用が拡大した",
                    delta_type="escalated",
                    delta_note="被害・標的の拡大を観測",
                    fired_indicators=("新たな被害公表",),
                    # PIR rollup のテンプレート経路も通す (dict.items 衝突の回帰)
                    pir_ids=("pir_x",),
                ),
                _j(id="s-standing", claim="継続中の情勢", delta_type="no_change"),
            ),
        )
        llm = FakeLLM()
        out = await render_sections(llm=llm, est=est, period_label="L")  # type: ignore[arg-type]
        assert out.headline == _OK_HEADLINE
        # headline 対象はコード指名 (moved の salience 最上位)
        assert "headline 対象判定" in llm.last_prompt
        assert "[s-moved]" in llm.last_prompt
        assert "【拡大】" in llm.last_prompt
        assert "【継続】継続中の情勢" in llm.last_prompt
        assert "発火した監視指標: 新たな被害公表" in llm.last_prompt

    @pytest.mark.asyncio
    async def test_legacy_estimate_without_delta_renders_as_standing(self) -> None:
        # SYNTHESIS_STATE=0/shadow の旧経路 (delta 無し) でも同一テンプレートで壊れない
        est = Estimate(
            period_type="daily",
            period_start=datetime.now(UTC),
            period_end=datetime.now(UTC),
            judgments=(_j(id="j1", claim="旧経路の判定"),),
        )
        llm = FakeLLM()
        out = await render_sections(llm=llm, est=est, period_label="L")  # type: ignore[arg-type]
        assert out.headline == _OK_HEADLINE
        assert "(本期間に変化した判定なし)" in llm.last_prompt


def _daily(*judgments: KeyJudgment) -> Estimate:
    return Estimate(
        period_type="daily",
        period_start=datetime.now(UTC),
        period_end=datetime.now(UTC),
        judgments=judgments,
    )


class TestQuietRepeatSuppression:
    """静穏日の同一 standing 再掲は「前日から継続 + 次いで注視」へ置換 (2026-08-07)。"""

    @pytest.mark.asyncio
    async def test_repeat_quiet_headline_is_replaced_with_continuation(self) -> None:
        est = _daily(
            _j(
                id="s-top",
                claim="中国系APTのスパイ活動が継続",
                delta_type="no_change",
                confidence="high",
            ),
            _j(id="s-next", claim="Mirai系ボットネットの拡散", delta_type="no_change"),
        )
        llm = FakeLLM()
        out = await render_sections(
            llm=llm,  # type: ignore[arg-type]
            est=est,
            period_label="L",
            prev_headline_judgment_id="s-top",
        )
        assert "前日から継続" in out.headline
        assert "中国系APTのスパイ活動が継続" in out.headline
        # salience 次点が「次いで注視」で新規性を供給する
        assert "次いで注視" in out.headline
        assert "Mirai系ボットネットの拡散" in out.headline

    @pytest.mark.asyncio
    async def test_first_quiet_day_is_not_suppressed(self) -> None:
        # 前日の headline が別判定 (または初日) なら通常の quiet render のまま
        est = _daily(_j(id="s-top", claim="継続中の情勢", delta_type="no_change"))
        llm = FakeLLM()
        out = await render_sections(
            llm=llm,  # type: ignore[arg-type]
            est=est,
            period_label="L",
            prev_headline_judgment_id="different-id",
        )
        assert out.headline == _OK_HEADLINE

    @pytest.mark.asyncio
    async def test_moved_day_repeat_is_not_suppressed(self) -> None:
        # 実変化 (moved) の連日報告は情報価値がある — 同一判定でも置換しない
        est = _daily(_j(id="m1", claim="悪用が拡大", delta_type="escalated"))
        llm = FakeLLM()
        out = await render_sections(
            llm=llm,  # type: ignore[arg-type]
            est=est,
            period_label="L",
            prev_headline_judgment_id="m1",
        )
        assert out.headline == _OK_HEADLINE

    @pytest.mark.asyncio
    async def test_repeat_without_next_candidate_still_marks_continuation(self) -> None:
        est = _daily(_j(id="s-top", claim="唯一の継続判定", delta_type="no_change"))
        llm = FakeLLM()
        out = await render_sections(
            llm=llm,  # type: ignore[arg-type]
            est=est,
            period_label="L",
            prev_headline_judgment_id="s-top",
        )
        assert "前日から継続" in out.headline
        assert "次いで注視" not in out.headline


class TestHeadlineContract:
    """headline の書式モードはコードが決定論指名する (moved/quiet/plain)。"""

    @pytest.mark.asyncio
    async def test_moved_mode_demands_change_language(self) -> None:
        llm = FakeLLM()
        await render_sections(
            llm=llm,  # type: ignore[arg-type]
            est=_daily(_j(id="a", delta_type="escalated")),
            period_label="L",
        )
        assert "変化種別が分かる自然な日本語" in llm.last_prompt

    @pytest.mark.asyncio
    async def test_quiet_mode_when_nothing_moved(self) -> None:
        # 静穏日: 「変化なし」の正直報告を headline にも構造適用 (変化を創作させない)
        llm = FakeLLM()
        await render_sections(
            llm=llm,  # type: ignore[arg-type]
            est=_daily(_j(id="a", delta_type="no_change")),
            period_label="L",
        )
        assert "変化を創作しない" in llm.last_prompt

    @pytest.mark.asyncio
    async def test_plain_mode_for_legacy_estimate_without_delta(self) -> None:
        # rollback 経路 (delta 未追跡): 「変化なし」と偽らず、変化に言及しない書式
        llm = FakeLLM()
        await render_sections(
            llm=llm,  # type: ignore[arg-type]
            est=_daily(_j(id="a")),
            period_label="L",
        )
        assert "変化情報が供給されていない" in llm.last_prompt


def test_template_renders_without_headline_mode_variable() -> None:
    """prompts はホットマウント (編集即反映)、コードは cold deploy。

    旧コード (headline_mode 未供給) + 新テンプレートの組合せが StrictUndefined で
    落ちないこと (moved 既定に fallback) を固定する。
    """
    from src.synthesis.grounded.passes import _render

    prompt = _render(
        "synthesis_render.j2",
        period_label="L",
        headline_id="a",
        headline_view=None,
        moved=[],
        standing=[],
        pir_rollup=[],
        relation_lines=[],
    )
    assert "変化種別が分かる自然な日本語" in prompt


class TestHeadlineGuard:
    """floor 未満 (claim の言い換え程度) の headline は台帳 field から決定論合成する。"""

    @pytest.mark.asyncio
    async def test_short_headline_replaced_with_composition(self) -> None:
        est = _daily(
            _j(
                id="a",
                claim="The Gentlemen による重要インフラへの標的拡大",
                delta_type="escalated",
                delta_note="製造・エネルギー部門で新規被害",
                confidence="high",
                implication="国内関連組織でも侵入試行への警戒が必要",
            )
        )
        out = await render_sections(
            llm=FakeLLM(headline="標的が拡大した。"),  # type: ignore[arg-type]
            est=est,
            period_label="L",
        )
        assert "【拡大】" in out.headline
        assert "The Gentlemen による重要インフラへの標的拡大" in out.headline
        assert "高確度" in out.headline
        assert "国内関連組織でも侵入試行への警戒が必要" in out.headline

    @pytest.mark.asyncio
    async def test_adequate_headline_passes_through(self) -> None:
        out = await render_sections(
            llm=FakeLLM(),  # type: ignore[arg-type]
            est=_daily(_j(id="a", delta_type="opened")),
            period_label="L",
        )
        assert out.headline == _OK_HEADLINE

    @pytest.mark.asyncio
    async def test_quiet_fallback_reports_no_change_honestly(self) -> None:
        est = _daily(_j(id="a", claim="継続中の最重要判定", delta_type="no_change"))
        out = await render_sections(
            llm=FakeLLM(headline="短い。"),  # type: ignore[arg-type]
            est=est,
            period_label="L",
        )
        assert out.headline.startswith("本期間、確度をもって報告できる大きな変化はない")
        assert "継続中の最重要判定" in out.headline


class TestEpistemicWeight:
    """認識論的重み (2026-07-05): salience = 関連性 × どれだけ知っているか。

    本番実測 (2026-07-05 朝刊): ソース本文がボット対策画面のみで leading=
    unverified_or_false の JadePuffer 判定が、実証拠 7 件 + 実含意を持つ同 delta の
    Kairos 判定 ($1M 支払い) を差し置いて headline を占有した。ACH の誠実な較正結果が
    配置決定に流れ込む経路がなかった病巣の回帰固定。
    """

    def test_rumor_class_opened_loses_to_grounded_opened(self) -> None:
        # JadePuffer シナリオ: 同 delta/domain/conf でも噂クラスは接地された判定に負ける
        rumor = _j(
            id="jade",
            claim="JadePuffer が AI エージェントを利用",
            delta_type="opened",
            confidence="moderate",
            leading_hypothesis="unverified_or_false",
        )
        grounded = _j(
            id="kairos",
            claim="米政府機関が Kairos に $1M 支払い",
            delta_type="opened",
            confidence="moderate",
            leading_hypothesis="criminal_financial",
        )
        head = pick_headline((rumor, grounded))
        assert head is not None and head.id == "kairos"

    def test_all_rumor_moved_day_falls_to_solid_standing(self) -> None:
        # 変化が噂だけの日は、接地された継続判定が headline に立つ (→ render は quiet mode)
        rumor = _j(id="r", delta_type="opened", leading_hypothesis="unverified_or_false")
        standing = _j(
            id="s",
            delta_type="no_change",
            confidence="high",
            leading_hypothesis="criminal_financial",
        )
        head = pick_headline((rumor, standing))
        assert head is not None and head.id == "s"

    def test_massive_relevance_rumor_can_still_surface(self) -> None:
        # ハード除外ではない: 日本関連 + 高 delta の未実証情報は接地 standing を上回りうる
        jp_rumor = _j(
            id="jp",
            delta_type="opened",
            japan_related=True,
            pir_ids=("p1", "p2", "p3", "p4"),
            leading_hypothesis="unverified_or_false",
        )
        weak_standing = _j(
            id="s",
            delta_type="no_change",
            confidence="moderate",
            leading_hypothesis="opportunistic_commodity",
            domain="geopolitical",
        )
        head = pick_headline((jp_rumor, weak_standing))
        assert head is not None and head.id == "jp"

    def test_refuted_low_confidence_sinks(self) -> None:
        refuted = _j(
            id="a",
            delta_type="opened",
            confidence="low",
            adversarial_refuted=True,
            leading_hypothesis="unverified_or_false",
            domain="geopolitical",
        )
        solid = _j(
            id="b",
            delta_type="no_change",
            confidence="high",
            leading_hypothesis="criminal_financial",
        )
        assert salience(refuted) < salience(solid)

    def test_propaganda_and_reporting_artifact_also_rumor_class(self) -> None:
        base = _j(id="x", delta_type="opened")
        for hyp in ("propaganda_or_overstated", "reporting_artifact"):
            rumor = _j(id="y", delta_type="opened", leading_hypothesis=hyp)
            assert salience(rumor) < salience(base)

    def test_grounded_moved_still_beats_solid_standing(self) -> None:
        # 「変化優先」は重みから創発する: 接地された moved は standing high に勝つ
        moved = _j(id="m", delta_type="strengthened", confidence="moderate")
        standing = _j(id="s", delta_type="no_change", confidence="high")
        head = pick_headline((moved, standing))
        assert head is not None and head.id == "m"


@pytest.mark.asyncio
async def test_headline_contract_prohibits_process_and_audit_language() -> None:
    """2026-07-05 実測病理の回帰固定: headline は世界について書く。

    「新たに追跡を開始したが」(道具の運用) と「ソース本文がアクセス制限画面のみ〜
    実証できない」(判定過程) が headline に出た — contract に禁止を明記する。
    """
    llm = FakeLLM()
    await render_sections(
        llm=llm,  # type: ignore[arg-type]
        est=_daily(_j(id="a", delta_type="opened")),
        period_label="L",
    )
    assert "道具側の運用" in llm.last_prompt
    assert "ソース品質" in llm.last_prompt
    # 旧例示句 (プロセス言語) が contract から排除されていること
    assert "新たに追跡を開始した" not in llm.last_prompt
