"""状況総括の長さの関門 (2026-11-20、docs/synthesis_length_gate.md §2.2-2.4)。"""

from __future__ import annotations

import pytest

from src.synthesis.grounded.length_gate import (
    fit_judgment_lines,
    length_gate_mode,
    should_drop_last_item,
)
from src.tools.llm_truncation import TruncationProbe

FULL = [
    f"【新規】判定{i}の世界の文。見立ては「x」で、その可能性がある。" + "含意" * 40
    for i in range(6)
]
COMPACT = [f"【新規】判定{i}の世界の文。見立ては「x」で、その可能性がある。" for i in range(6)]
TAIL = "そのほかの動き (根拠は本文で扱わない判定): 新規 3 件。"


def test_mode_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SYNTHESIS_LENGTH_GATE", raising=False)
    assert length_gate_mode() == "off"
    monkeypatch.setenv("SYNTHESIS_LENGTH_GATE", "shadow")
    assert length_gate_mode() == "shadow"
    monkeypatch.setenv("SYNTHESIS_LENGTH_GATE", "1")
    assert length_gate_mode() == "on"


def test_fits_without_change() -> None:
    out = fit_judgment_lines(FULL, COMPACT, cap=10_000, tail=TAIL)
    assert out.text == "\n".join([*FULL, TAIL])
    assert (out.compacted, out.trimmed) == (0, 0)


def test_compacts_from_the_tail_and_keeps_every_judgment_and_the_tail() -> None:
    cap = len("\n".join([*FULL[:3], *COMPACT[3:], TAIL]))

    out = fit_judgment_lines(FULL, COMPACT, cap=cap, tail=TAIL)

    assert len(out.text) <= cap
    assert out.compacted == 3
    assert out.trimmed == 0
    lines = out.text.split("\n")
    assert lines[:3] == FULL[:3]  # 上位は全文のまま
    assert lines[3:6] == COMPACT[3:]  # 末尾から簡潔に
    assert lines[-1] == TAIL  # 一覧の段落は残る


def test_overflow_after_full_compaction_trims_lines_but_keeps_tail() -> None:
    cap = len("\n".join([*COMPACT[:2], TAIL])) + 30

    out = fit_judgment_lines(FULL, COMPACT, cap=cap, tail=TAIL)

    assert len(out.text) <= cap
    assert out.compacted == 6
    assert out.trimmed > 0
    assert out.text.endswith(TAIL)
    assert "省略" in out.text


def test_no_tail() -> None:
    cap = len("\n".join([*FULL[:5], COMPACT[5]]))
    out = fit_judgment_lines(FULL, COMPACT, cap=cap)
    assert out.text.split("\n")[-1] == COMPACT[5]
    assert out.compacted == 1


@pytest.mark.parametrize(
    ("mode", "observed", "cap", "watch_empty", "expected"),
    [
        # 0 / shadow: 従来の推測 (watch が空なら捨てる)
        ("off", True, False, True, True),
        ("off", True, True, False, False),
        ("shadow", True, False, True, True),
        # 1: 信号があれば信号に従う
        ("on", True, False, True, False),  # watch が空でも上限でなければ捨てない
        ("on", True, True, False, True),  # watch を書いていても上限なら捨てる
        # 1 でも信号の無いクライアントは推測に倒す
        ("on", False, False, True, True),
        ("on", False, False, False, False),
    ],
)
def test_should_drop_last_item(
    mode: str, observed: bool, cap: bool, watch_empty: bool, expected: bool
) -> None:
    probe = TruncationProbe(hit_cap=cap, observed=observed)
    assert (
        should_drop_last_item(
            has_items=True,
            watch_empty=watch_empty,
            probe=probe,
            mode=mode,  # type: ignore[arg-type]
        )
        is expected
    )


def test_never_drops_without_items() -> None:
    probe = TruncationProbe(hit_cap=True, observed=True)
    assert not should_drop_last_item(has_items=False, watch_empty=True, probe=probe, mode="on")


# ---- slot render への組み込み ----

from dataclasses import replace  # noqa: E402
from typing import Any  # noqa: E402

from src.synthesis.grounded import render as render_mod  # noqa: E402
from src.synthesis.grounded import slot_render  # noqa: E402
from src.synthesis.grounded.estimate import Estimate  # noqa: E402
from src.synthesis.grounded.render import build_render_plan  # noqa: E402
from src.synthesis.grounded.slot_render import ClauseItem, WireJudgmentClauses  # noqa: E402
from src.tools.llm_truncation import note_hit_cap, note_observed  # noqa: E402
from tests.unit.n20_t5_fixtures import estimate, judgment  # noqa: E402

LABEL = "2026-11-23 (weekly)"
LONG_WORLD = "世界側で侵入が広がっている。" + "標的と手段の詳細が続く" * 12


def _many(n: int) -> Estimate:
    base = estimate()
    judgments = tuple(
        replace(judgment(f"s-{i:03d}", delta="opened"), implication="含意の文が続く" * 20)
        for i in range(n)
    )
    return replace(base, judgments=judgments)


def _clauses_for(est: Estimate) -> WireJudgmentClauses:
    return WireJudgmentClauses(
        headline_clause="見出し",
        items=[
            ClauseItem(judgment_id=j.id, world_clause=LONG_WORLD, implication_clause="含意" * 30)
            for j in est.judgments
        ],
        watch_clause="注視",
    )


def test_fit_keeps_every_judgment_within_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(render_mod, "_MOVED_SECTION_MAX", {"daily": 40})
    monkeypatch.setattr(render_mod, "_MOVED_SECTION_MIN", {"daily": 40})
    est = _many(30)
    plan = build_render_plan(est=est, period_label=LABEL, template=slot_render.TEMPLATE_V9)
    clauses = _clauses_for(est)

    loose, _ = slot_render.assemble_sections(clauses, est=est, plan=plan)
    fitted, stats = slot_render.assemble_sections(clauses, est=est, plan=plan, fit=True)

    assert len(loose.weight_section) > render_mod._SECTION_MAX_CHARS  # 前提: 超える
    assert len(fitted.weight_section) <= render_mod._SECTION_MAX_CHARS
    assert stats.compacted > 0 and stats.overflow_trimmed == 0
    # どの判定も消えない (確度の定型文が全件に残る)
    assert fitted.weight_section.count("その可能性がある") == len(plan.moved_ids)
    assert "対立仮説" in fitted.weight_section.split("\n")[0]  # 上位は全文のまま
    assert "対立仮説" not in fitted.weight_section.split("\n")[-1]  # 末尾は簡潔


def test_fit_off_is_unchanged() -> None:
    est = estimate()
    plan = build_render_plan(est=est, period_label=LABEL, template=slot_render.TEMPLATE_V9)
    clauses = _clauses_for(est)
    a, _ = slot_render.assemble_sections(clauses, est=est, plan=plan)
    b, stats = slot_render.assemble_sections(clauses, est=est, plan=plan, fit=True)
    assert a == b and stats.compacted == 0


class _CapLLM:
    """最後の項目を書き切って watch_clause も書いたが、上限に当たった応答。"""

    model = "fake"

    def __init__(self, *, hit_cap: bool, watch: str) -> None:
        self.hit_cap = hit_cap
        self.watch = watch

    async def generate_structured(self, prompt: str, schema: type, **_kw: Any) -> Any:
        note_observed()
        if self.hit_cap:
            note_hit_cap()
        return WireJudgmentClauses(
            headline_clause="見出し",
            items=[
                ClauseItem(judgment_id="s-aaa", world_clause="攻撃が拡大した"),
                ClauseItem(judgment_id="s-bbb", world_clause="被害が広がった"),
            ],
            watch_clause=self.watch,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "hit_cap", "watch", "kept"),
    [
        ("1", True, "注視", 1),  # 信号で捨てる (推測では見逃していた)
        ("1", False, "", 2),  # 上限でなければ watch が空でも捨てない
        ("0", True, "注視", 2),  # 旗 0 は従来の推測
        ("0", False, "", 1),
    ],
)
async def test_truncation_signal_decides_the_last_item(
    monkeypatch: pytest.MonkeyPatch, mode: str, hit_cap: bool, watch: str, kept: int
) -> None:
    monkeypatch.setenv("SYNTHESIS_LENGTH_GATE", mode)
    clauses, _ = await slot_render.generate_clauses(
        llm=_CapLLM(hit_cap=hit_cap, watch=watch),  # type: ignore[arg-type]
        est=estimate(),
        period_label=LABEL,
    )
    assert len(clauses.items) == kept


def test_shadow_logs_fit_but_keeps_output(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SYNTHESIS_LENGTH_GATE", "shadow")
    monkeypatch.setattr(render_mod, "_MOVED_SECTION_MAX", {"daily": 40})
    monkeypatch.setattr(render_mod, "_MOVED_SECTION_MIN", {"daily": 40})
    seen: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(slot_render._log, "info", lambda e, **kw: seen.append((e, kw)))
    est = _many(30)
    plan = build_render_plan(est=est, period_label=LABEL, template=slot_render.TEMPLATE_V9)

    out, _ = slot_render.assemble_with_gate(
        _clauses_for(est), est=est, plan=plan, period_label=LABEL
    )

    assert len(out.weight_section) > render_mod._SECTION_MAX_CHARS  # 出力は従来どおり
    shadow = [kw for e, kw in seen if e == "synthesis_length_gate_shadow"]
    assert shadow and shadow[0]["compacted"] > 0


class _LegacyCapLLM:
    """旧経路: 上限に当たり、救済で後ろの節が空になった応答。"""

    model = "fake"

    async def generate_structured(self, prompt: str, schema: type, **_kw: Any) -> Any:
        note_observed()
        note_hit_cap()
        return render_mod._WireSections(headline="見出し", weight_section="途中で切れ")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["0", "shadow", "1"])
async def test_legacy_truncation_falls_back_to_deterministic_sections(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    monkeypatch.setenv("SLOT_RENDER", "0")
    monkeypatch.setenv("SYNTHESIS_LENGTH_GATE", mode)

    out = await render_mod.render_sections(
        llm=_LegacyCapLLM(),  # type: ignore[arg-type]
        est=estimate(),
        period_label=LABEL,
    )

    if mode == "1":
        for name in ("headline", "weight_section", "cog_section", "pir_section"):
            assert getattr(out, name).strip(), name  # 必須の節が埋まる
        assert "途中で切れ" not in out.weight_section
    else:
        assert out.weight_section == "途中で切れ"  # 従来どおり
