"""パターン版の描画・旗 (SYNTHESIS_PATTERNS)・現行との同一性。LLM は fake。"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from src.storage.repo_judgment_attrs import JudgmentAttrs
from src.synthesis.grounded import pattern_render
from src.synthesis.grounded.estimate import Estimate
from src.synthesis.grounded.pattern_render import PatternItem, WirePatterns
from src.synthesis.grounded.render import _WireSections, render_sections
from tests.unit.n20_t5_fixtures import BASE_TS
from tests.unit.synthesis_pattern_fixtures import BREACH, JP, attrs, j

LABEL = "2026-11-17 (daily)"


def _est() -> Estimate:
    js = (
        j("s-1", "アルファ証券で顧客情報が漏えいした。"),
        j("s-2", "ベータ銀行で委託先のシステム経由の漏えいがあった。"),
        j("s-3", "ガンマ商事で侵害が確認された。"),
        j("s-4", "継続中の評価の文。", delta="no_change"),
    )
    return Estimate("daily", BASE_TS, BASE_TS + timedelta(days=1), js, model="fake")


def _attrs() -> dict[str, JudgmentAttrs]:
    return {
        "s-1": attrs(countries=JP, kinds=BREACH, s3=True),
        "s-2": attrs(countries=JP, kinds=BREACH),
        "s-3": attrs(countries=JP, kinds=BREACH),
    }


GOOD = WirePatterns(
    headline="アルファ証券を含む日本の侵害が 3 件動いた。",
    patterns=[
        PatternItem(pattern_id="P1", text="アルファ証券は S3 で、ガンマ商事は確認段階にとどまる。")
    ],
    watch="漏えいの続報を注視する。",
)


class _LLM:
    model = "fake"

    def __init__(self, out: WirePatterns | None = GOOD, *, fail: bool = False) -> None:
        self.schemas: list[type] = []
        self.out = out
        self.fail = fail

    async def generate_structured(self, prompt: str, schema: type, **_kw: Any) -> Any:
        self.schemas.append(schema)
        if schema is WirePatterns:
            if self.fail:
                raise RuntimeError("boom")
            return self.out
        return _WireSections(headline="旧の見出し", weight_section="旧")


async def _render(llm: _LLM, a: dict[str, JudgmentAttrs] | None = None) -> _WireSections:
    return await render_sections(
        llm=llm,  # type: ignore[arg-type]
        est=_est(),
        period_label=LABEL,
        attrs=_attrs() if a is None else a,
    )


def test_prompt_has_patterns_marks_and_json_contract() -> None:
    from src.synthesis.grounded.patterns import build_patterns

    plan = build_patterns(_est().judgments, _attrs())
    prompt = pattern_render.build_prompt(plan, LABEL)
    assert "パターン P1: 日本の侵害・情報漏えい (3 件、深刻度 S3 1 件)" in prompt
    assert "提供者・委託先経由" not in prompt
    assert "[新規・中確度] ベータ銀行で委託先のシステム経由の漏えいがあった" in prompt
    assert "見出しで必ず触れる事案" in prompt and "[C" not in prompt
    assert '"patterns": [{"pattern_id"' in prompt


@pytest.mark.asyncio
async def test_flag_off_does_not_touch_patterns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SYNTHESIS_PATTERNS", raising=False)
    llm = _LLM()
    out = await _render(llm)
    assert llm.schemas == [_WireSections]
    assert out.weight_section == "旧"


@pytest.mark.asyncio
async def test_on_without_attrs_uses_current_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SYNTHESIS_PATTERNS", "1")
    llm = _LLM()
    out = await render_sections(llm=llm, est=_est(), period_label=LABEL)  # type: ignore[arg-type]
    assert llm.schemas == [_WireSections] and out.weight_section == "旧"


@pytest.mark.asyncio
async def test_on_renders_patterns_sections(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SYNTHESIS_PATTERNS", "1")
    llm = _LLM()
    out = await _render(llm)
    assert llm.schemas == [WirePatterns]
    assert "アルファ証券を含む日本の侵害が 3 件動いた" in out.headline
    assert "【日本の侵害・情報漏えい】アルファ証券は S3" in out.weight_section
    assert "- ベータ銀行で委託先のシステム経由の漏えいがあった (新規・中確度)" in out.weight_section
    assert "継続中の評価の文" in out.cog_section
    assert "漏えいの続報を注視する" in out.spillover_section
    assert out.pir_section == "SIR に紐づく判定はない。"
    assert out.chain_section == "判定間の関係はこの総括では扱っていない。"


@pytest.mark.asyncio
async def test_on_falls_back_to_deterministic_text_when_gate_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SYNTHESIS_PATTERNS", "1")
    bad = WirePatterns(headline="確実に拡大", patterns=[PatternItem(pattern_id="P9", text="x")])
    out = await _render(_LLM(bad))
    assert out.headline.startswith(
        "日本の侵害・情報漏えい: アルファ証券で顧客情報が漏えいした。注目:"
    )
    assert "日本の侵害・情報漏えい: 3 件" in out.weight_section


@pytest.mark.asyncio
async def test_on_quiet_day_skips_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SYNTHESIS_PATTERNS", "1")
    llm = _LLM()
    out = await _render(llm, {})  # 属性なし = パターンが立たない
    assert llm.schemas == []
    assert out.headline.startswith("確度をもって報告できる横断の動きはない。")
    assert "そのほかの動き" in out.weight_section


@pytest.mark.asyncio
async def test_on_llm_failure_falls_back_to_current(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SYNTHESIS_PATTERNS", "1")
    llm = _LLM(fail=True)
    out = await _render(llm)
    assert llm.schemas == [WirePatterns, _WireSections]
    assert out.weight_section == "旧"


@pytest.mark.asyncio
async def test_shadow_delivers_current_and_logs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SYNTHESIS_PATTERNS", "shadow")
    seen: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(pattern_render._log, "info", lambda e, **kw: seen.append((e, kw)))
    llm = _LLM()
    out = await _render(llm)
    assert llm.schemas == [_WireSections, WirePatterns]
    assert out.weight_section == "旧"
    event, kw = seen[-1]
    assert event == "synthesis_pattern_render_shadow"
    assert kw["patterns"] == 1 and kw["llm_called"] is True


@pytest.mark.asyncio
async def test_shadow_failure_never_affects_delivery(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SYNTHESIS_PATTERNS", "shadow")
    out = await _render(_LLM(fail=True))
    assert out.weight_section == "旧"


@pytest.mark.asyncio
async def test_generation_input_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SYNTHESIS_PATTERNS", "1")
    kinds: list[str] = []
    monkeypatch.setattr(
        pattern_render, "record_generation_input", lambda **kw: kinds.append(kw["product_kind"])
    )
    await _render(_LLM())
    assert kinds == ["synthesis_pattern_render"]


@pytest.mark.asyncio
async def test_notable_singleton_is_prompted_and_shown_after_patterns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace

    monkeypatch.setenv("SYNTHESIS_PATTERNS", "1")
    est = _est()
    est = replace(
        est, judgments=(*est.judgments, j("s-5", "ゼータ取引所で大規模な不正流出が発生した。"))
    )
    a = {**_attrs(), "s-5": attrs(kinds=frozenset({"exploitation"}), s3=True)}
    out_llm = WirePatterns(
        headline="日本の侵害が 3 件動いた。ゼータ取引所の不正流出にも注意する。",
        patterns=[
            PatternItem(pattern_id="P1", text="アルファ証券は S3 で、ガンマ商事は確認段階だった。"),
            PatternItem(pattern_id="S1", text="ゼータ取引所で不正流出が発生した。"),
        ],
    )
    out = await render_sections(
        llm=_LLM(out_llm),  # type: ignore[arg-type]
        est=est,
        period_label=LABEL,
        attrs=a,
    )
    assert "【注目の単独事案】ゼータ取引所で不正流出が発生した。" in out.weight_section
    assert out.weight_section.index("【日本の侵害") < out.weight_section.index("【注目の単独事案】")
    from src.synthesis.grounded.patterns import build_patterns

    prompt = pattern_render.build_prompt(build_patterns(est.judgments, a), LABEL)
    assert "注目の単独事案 S1" in prompt


@pytest.mark.asyncio
async def test_marks_and_all_standing_lines_are_rendered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace

    monkeypatch.setenv("SYNTHESIS_PATTERNS", "1")
    est = _est()
    est = replace(
        est,
        judgments=(
            *est.judgments,
            *(
                j(f"s-{n}", f"継続{n}の長い台帳の文。詳細。", delta="no_change")
                for n in (7, 8, 9, 10, 11)
            ),
        ),
    )
    tech = frozenset({"T1566"})
    a = {
        "s-1": attrs(countries=JP, kinds=BREACH, s3=True, techniques=tech),
        "s-2": attrs(countries=JP, kinds=BREACH, techniques=tech),
        "s-3": attrs(countries=JP, kinds=BREACH),
        "s-7": attrs(last="2026-11-10T00:00:00+00:00"),
    }
    llm_out = WirePatterns(
        headline="アルファ証券を含む日本の侵害が動いた。",
        patterns=[
            PatternItem(pattern_id="P1", text="アルファ証券とベータ銀行で侵害が確認された。")
        ],
    )
    out = await render_sections(
        llm=_LLM(llm_out),  # type: ignore[arg-type]
        est=est,
        period_label=LABEL,
        attrs=a,
    )
    assert "共通の手口: T1566" in out.weight_section
    assert out.chain_section == "判定間の関係はこの総括では扱っていない。"  # 共通の実体なし
    assert out.cog_section.count("\n") == 5  # 継続 6 件 (s-4 + s-7〜11) を全て 1 行ずつ
    assert "継続7の長い台帳の文 (中確度・最終 2026-11-10)" in out.cog_section
    assert "詳細" not in out.cog_section


@pytest.mark.asyncio
async def test_shared_entities_become_deterministic_relations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SYNTHESIS_PATTERNS", "1")
    a = {
        "s-1": attrs(
            countries=JP, kinds=BREACH, links=frozenset({"cve:CVE-2026-0001", "actor:G1"})
        ),
        "s-2": attrs(
            countries=JP, kinds=BREACH, links=frozenset({"cve:CVE-2026-0001", "actor:G1"})
        ),
        "s-3": attrs(countries=JP, kinds=BREACH, links=frozenset({"cve:CVE-2026-0001"})),
    }
    out = await _render(_LLM(GOOD), a)
    lines = out.chain_section.splitlines()
    assert lines[0] == "- アルファ証券 と ベータ銀行: 共通 = CVE-2026-0001、G1"
    assert len(lines) == 3 and all("共通 = CVE-2026-0001" in x for x in lines)  # 上限 3 件


@pytest.mark.asyncio
async def test_notable_shown_with_s_id_and_model_answer_by_c_id_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """3 回目の不具合: 注目の事案を C 番号で見せ S 番号を求めたため、C で返されて落ちていた。"""
    from dataclasses import replace

    monkeypatch.setenv("SYNTHESIS_PATTERNS", "1")
    est = _est()
    est = replace(
        est, judgments=(*est.judgments, j("s-5", "ゼータ取引所で大規模な不正流出が発生した。"))
    )
    a = {**_attrs(), "s-5": attrs(kinds=frozenset({"exploitation"}), s3=True)}
    from src.synthesis.grounded.patterns import build_patterns

    plan = build_patterns(est.judgments, a)
    prompt = pattern_render.build_prompt(plan, LABEL)
    assert "[S1・" in prompt and "[C" not in prompt
    notable_c_id = plan.notable[0].case.case_id
    out_llm = WirePatterns(
        headline="ゼータ取引所の不正流出と、日本の侵害が動いた。",
        patterns=[
            PatternItem(pattern_id="P1", text="アルファ証券とガンマ商事で侵害が確認された。"),
            PatternItem(pattern_id=notable_c_id, text="ゼータ取引所で不正流出が発生した。"),
        ],
    )
    out = await render_sections(
        llm=_LLM(out_llm),  # type: ignore[arg-type]
        est=est,
        period_label=LABEL,
        attrs=a,
    )
    assert "【注目の単独事案】ゼータ取引所で不正流出が発生した。" in out.weight_section
