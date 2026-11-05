"""多段 Spotlight (n20 T5、旗 SPOTLIGHT_STAGED、利用者判断 U-A)。LLM は fake。"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest

from src.spotlight import staged, staged_render
from src.spotlight.staged import OutlookPoint, StagedKeyEvent, StagedNote, StagedSpotlight

ANCHOR_1 = "CISA added the flaw to the KEV catalog after exploitation"
ANCHOR_2 = "日本の製造業の拠点で侵入が確認されたと同社は発表した"


def _cands() -> list[dict[str, Any]]:
    return [
        {
            "title": "Vendor patches VPN flaw CVE-2026-0001",
            "summary": f"{ANCHOR_1} in the wild.",
            "reliability": "news",
            "ttps": ["T1190"],
        },
        {
            "title": "国内工場への侵入",
            "summary": f"{ANCHOR_2}。原因は調査中。",
            "reliability": "social",
            "ttps": [],
        },
        {"title": "other", "summary": "nothing", "reliability": "official", "ttps": []},
    ]


def _merged(**over: Any) -> StagedSpotlight:
    base: dict[str, Any] = {
        "headline": "VPN の欠陥の悪用と国内工場への侵入",
        "key_events": [
            StagedKeyEvent(index=1, rationale="KEV 登載", anchor=ANCHOR_1),
            StagedKeyEvent(index=2, rationale="国内被害", anchor=ANCHOR_2),
        ],
        "event_notes": [
            StagedNote(index=2, kind="unknown", text="侵入の経路は不明。", anchor=ANCHOR_2),
            StagedNote(index=1, kind="caveat", text="影響範囲は限定的とされる。", anchor=ANCHOR_1),
        ],
        "outlook_points": [
            OutlookPoint(aspect="a", text="境界機器の悪用が続く [1]。"),
            OutlookPoint(aspect="b", text="同製品の追加の勧告。"),
        ],
    }
    base.update(over)
    return StagedSpotlight(**base)


class TestRecommendationTable:
    def test_rules_fire_from_candidate_text(self) -> None:
        recs = staged_render.recommendations(_cands()[:2])
        joined = "\n".join(recs)
        assert "CISA KEV に登載" in joined
        assert "CVE が付いた脆弱性" in joined
        assert "関係組織への通知・共有の要否" in joined  # 日本の組織 → 通知の判断
        assert "T1190" in joined

    def test_no_rule_no_c(self) -> None:
        assert staged_render.recommendations([_cands()[2]]) == []
        out = staged_render.render_outlook([("a", "x")], [])
        assert "(c)" not in out and "(d)" not in out

    def test_outlook_has_only_a_b_c(self) -> None:
        out = staged_render.render_outlook([("b", "指標"), ("a", "型")], ["推奨"])
        assert out.index("(a)") < out.index("(b)") < out.index("(c)")

    def test_source_tier_caveat(self) -> None:
        lines = staged_render.source_caveats([(1, _cands()[0]), (2, _cands()[1])])
        assert lines == ["[2]「国内工場への侵入」はSNS 等の投稿に基づく (裏付けは未確認)。"]


class TestCheckAndRender:
    def test_happy_path(self) -> None:
        res = staged.check_and_render(_merged(), _cands())
        assert res.key_event_indexes == (1, 2)
        assert res.caveats[0].startswith("[2]")  # 情報源の留保が先
        assert "影響範囲は限定的とされる。" in res.caveats
        assert res.unknowns == ("侵入の経路は不明。",)
        assert "(a) 今期のパターン" in res.outlook and "(c) 推奨" in res.outlook
        assert res.counts["note_dropped"] == 0 and res.counts["point_dropped"] == 0

    def test_bad_indexes_and_anchors(self) -> None:
        merged = _merged(
            key_events=[
                StagedKeyEvent(index=9, anchor=ANCHOR_1),
                StagedKeyEvent(index=1, anchor="this sentence is not in the candidate"),
                StagedKeyEvent(index=1, anchor=ANCHOR_1),
            ],
            event_notes=[
                StagedNote(index=3, kind="caveat", text="t", anchor=ANCHOR_1),  # 主要事象外
                StagedNote(index=1, kind="opinion", text="t", anchor=ANCHOR_1),  # kind 不正
                StagedNote(index=1, kind="caveat", text="t", anchor=ANCHOR_2),  # 出典ずれ
            ],
        )
        res = staged.check_and_render(merged, _cands())
        assert res.key_event_indexes == (1,)
        assert res.counts["event_index_bad"] == 2
        assert res.counts["event_anchor_bad"] == 1
        assert res.counts["note_dropped"] == 3

    def test_points_aspect_count_and_certainty(self) -> None:
        merged = _merged(
            outlook_points=[
                OutlookPoint(aspect="c", text="推奨を書いた"),
                OutlookPoint(aspect="a", text="確実に続く"),
                OutlookPoint(aspect="b", text="指標"),
                OutlookPoint(aspect="a", text="4 つ目は上限外"),
            ]
        )
        res = staged.check_and_render(merged, _cands())
        assert res.counts["point_dropped"] == 2
        assert "(b) 来週の指標: 指標" in res.outlook and "4 つ目" not in res.outlook

    def test_merged_shape_passes_the_n20_training_gate(self) -> None:
        import importlib.util
        from pathlib import Path

        spec = importlib.util.spec_from_file_location(
            "sft_gate_n20_schemas", Path("scripts/sft_gate_n20_schemas.py")
        )
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        filler = "filler sentence number {} for the anchor check"
        cands = _cands() + [
            dict(_cands()[2], title=f"filler {i}", summary=filler.format(i)) for i in range(3)
        ]
        user = staged.build_stage_prompt(
            "events",
            pir=_FakePir(),
            period_label="p",
            candidate_articles=cands,
            ledger_situations=[],
        )
        key_events = [
            StagedKeyEvent(index=1, rationale="r", anchor=ANCHOR_1),
            StagedKeyEvent(index=2, rationale="r", anchor=ANCHOR_2),
        ] + [
            StagedKeyEvent(index=i, rationale="r", anchor=filler.format(i - 4)) for i in range(4, 7)
        ]
        out = json.loads(_merged(key_events=key_events).model_dump_json())
        assert mod.schema_errors_spotlight(out, user) == []


@dataclass
class _FakeSignals:
    actors: list[str] = field(default_factory=list)
    sectors: list[str] = field(default_factory=list)
    countries: list[str] = field(default_factory=list)


@dataclass
class _FakePir:
    strong_signals: _FakeSignals = field(default_factory=_FakeSignals)
    id: str = "pir_test"
    title: str = "test"
    description: str = "desc"
    enabled: bool = True
    spotlight: Any = field(default_factory=lambda: type("S", (), {"enabled": True})())


class TestPrompts:
    @pytest.mark.parametrize("stage", ["events", "notes", "outlook"])
    def test_each_stage_renders(self, stage: str) -> None:
        p = staged.build_stage_prompt(
            stage,
            chosen=[{"index": 1, "title": "t", "rationale": "r"}],
            pir=_FakePir(),
            period_label="p",
            candidate_articles=_cands(),
            ledger_situations=[],
        )
        assert "[1] **Vendor patches" in p
        assert "(d)" not in p


class _StagedLLM:
    model = "fake"

    def __init__(self, delay: float = 0.0) -> None:
        self.schemas: list[str] = []
        self.delay = delay

    async def generate_structured(self, prompt: str, *, schema: Any, **_kw: Any) -> Any:
        self.schemas.append(schema.__name__)
        if self.delay:
            await asyncio.sleep(self.delay)
        m = _merged()
        if schema.__name__ == "_StageEvents":
            return schema(headline=m.headline, key_events=m.key_events)
        if schema.__name__ == "_StageNotes":
            return schema(event_notes=m.event_notes)
        if schema.__name__ == "_StageOutlook":
            return schema(outlook_points=m.outlook_points)
        return schema(
            headline="旧",
            key_events=[{"index": i} for i in range(1, 6)],
            outlook="旧の見通し",
            caveats=["c"],
            unknowns=["u"],
        )


@dataclass
class _FakeMatch:
    article_id: str
    title: str = "t"
    url: str = "https://kuebiko.example/a"
    feed_title: str = "feed"
    importance: str = "high"
    created_at: str = "2026-10-20T00:00:00+00:00"
    summary: str = ANCHOR_1
    feed_url: str = ""


def _patch(monkeypatch: pytest.MonkeyPatch) -> None:
    import src.spotlight.generator as gen

    matches = [_FakeMatch(article_id=f"a{i}") for i in range(6)]
    monkeypatch.setattr(gen, "evaluate_pir_matches", lambda *a, **k: matches)
    monkeypatch.setattr(gen, "_build_prompt", lambda *a, **k: "PROMPT")
    monkeypatch.setattr(gen, "_load_previous_spotlight", lambda **k: None)
    monkeypatch.setattr(gen, "_build_ledger_context", lambda *a, **k: [])

    class _FakeRepo:
        def entities_for_articles(self, *a: Any, **k: Any) -> dict[str, list[str]]:
            return {}

    monkeypatch.setattr(gen, "RunHistoryRepository", _FakeRepo)
    monkeypatch.setenv("SPOTLIGHT_TAIL_GATE", "0")
    monkeypatch.setenv("SPOTLIGHT_IDENTIFIER_GATE", "0")


async def _run(llm: Any) -> Any:
    import src.spotlight.generator as gen

    return await gen.generate_spotlight(
        _FakePir(),  # type: ignore[arg-type]
        llm=llm,
        assessment=object(),  # type: ignore[arg-type]
        now=datetime(2026, 10, 21, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_off_calls_legacy_once(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch)
    monkeypatch.delenv("SPOTLIGHT_STAGED", raising=False)
    llm = _StagedLLM()
    record = await _run(llm)
    assert llm.schemas == ["_LLMSpotlightOutput"]
    assert record.outlook == "旧の見通し"


@pytest.mark.asyncio
async def test_on_uses_three_stages(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch)
    monkeypatch.setenv("SPOTLIGHT_STAGED", "1")
    llm = _StagedLLM()
    record = await _run(llm)
    assert llm.schemas == ["_StageEvents", "_StageNotes", "_StageOutlook"]
    assert "(a) 今期のパターン" in record.outlook
    assert [k.article_id for k in record.key_events] == ["a0", "a1"]


@pytest.mark.asyncio
async def test_on_timeout_falls_back_to_legacy(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch)
    monkeypatch.setenv("SPOTLIGHT_STAGED", "on")
    monkeypatch.setenv("SPOTLIGHT_STAGED_TIMEOUT_SECONDS", "0.01")
    llm = _StagedLLM(delay=0.05)
    record = await _run(llm)
    assert llm.schemas[-1] == "_LLMSpotlightOutput"
    assert record.outlook == "旧の見通し"


@pytest.mark.asyncio
async def test_shadow_keeps_legacy_and_logs_elapsed(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch)
    monkeypatch.setenv("SPOTLIGHT_STAGED", "shadow")
    seen: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(staged._log, "info", lambda e, **kw: seen.append((e, kw)))
    llm = _StagedLLM()
    record = await _run(llm)
    assert llm.schemas == ["_LLMSpotlightOutput", "_StageEvents", "_StageNotes", "_StageOutlook"]
    assert record.outlook == "旧の見通し"
    shadow = [kw for e, kw in seen if e == "spotlight_staged_shadow"]
    assert shadow and "elapsed_s" in shadow[0] and shadow[0]["timeout_s"] == 3000.0


def test_timeout_default_is_3000_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.tools.n20_flags import spotlight_staged_timeout_seconds

    for raw in ("", "abc", "-5", "0"):
        monkeypatch.setenv("SPOTLIGHT_STAGED_TIMEOUT_SECONDS", raw)
        assert spotlight_staged_timeout_seconds() == 3000.0
    monkeypatch.setenv("SPOTLIGHT_STAGED_TIMEOUT_SECONDS", "900")
    assert spotlight_staged_timeout_seconds() == 900.0


class _SeqLLM:
    """段ごとに応答の列を返す fake (尽きたら最後を繰り返す)。"""

    model = "fake"

    def __init__(self, replies: dict[str, list[Any]]) -> None:
        self.replies = replies
        self.calls: list[tuple[str, str]] = []

    async def generate_structured(self, prompt: str, *, schema: Any, **_kw: Any) -> Any:
        self.calls.append((schema.__name__, prompt))
        seq = self.replies[schema.__name__]
        return seq.pop(0) if len(seq) > 1 else seq[0]


def _ctx() -> dict[str, Any]:
    return {
        "pir": _FakePir(),
        "period_label": "p",
        "candidate_articles": _cands(),
        "ledger_situations": [],
    }


def _stage_replies(**over: list[Any]) -> dict[str, list[Any]]:
    m = _merged()
    base: dict[str, list[Any]] = {
        "_StageEvents": [staged._StageEvents(headline=m.headline, key_events=m.key_events)],
        "_StageNotes": [staged._StageNotes(event_notes=m.event_notes)],
        "_StageOutlook": [staged._StageOutlook(outlook_points=m.outlook_points)],
    }
    base.update(over)
    return base


class TestStagedGates:
    @pytest.mark.asyncio
    async def test_tail_gate_resamples_notes_and_takes_the_fuller(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SPOTLIGHT_TAIL_GATE", "1")
        monkeypatch.setenv("SPOTLIGHT_IDENTIFIER_GATE", "0")
        full = staged._StageNotes(
            event_notes=[
                StagedNote(index=1, kind="caveat", text="範囲は限定的とされる。", anchor=ANCHOR_1),
                StagedNote(index=2, kind="unknown", text="侵入経路は不明。", anchor=ANCHOR_2),
                StagedNote(index=1, kind="unknown", text="被害件数は未確認。", anchor=ANCHOR_1),
            ]
        )
        llm = _SeqLLM(_stage_replies(_StageNotes=[staged._StageNotes(), full]))
        res = await staged.generate_staged(llm, **_ctx())  # type: ignore[arg-type]
        assert [c for c, _ in llm.calls].count("_StageNotes") == 2
        assert len(res.unknowns) == 2 and len(res.caveats) >= 2
        assert res.counts["tail_resamples"] == 1 and res.counts["tail_unmet"] == 0

    @pytest.mark.asyncio
    async def test_tail_gate_off_does_not_resample(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SPOTLIGHT_TAIL_GATE", "0")
        monkeypatch.setenv("SPOTLIGHT_IDENTIFIER_GATE", "0")
        llm = _SeqLLM(_stage_replies(_StageNotes=[staged._StageNotes()]))
        res = await staged.generate_staged(llm, **_ctx())  # type: ignore[arg-type]
        assert [c for c, _ in llm.calls] == ["_StageEvents", "_StageNotes", "_StageOutlook"]
        assert res.counts["tail_resamples"] == 0

    @pytest.mark.asyncio
    async def test_identifier_gate_rewrites_only_the_offending_stage(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SPOTLIGHT_TAIL_GATE", "0")
        monkeypatch.setenv("SPOTLIGHT_IDENTIFIER_GATE", "1")
        bad = staged._StageOutlook(
            outlook_points=[OutlookPoint(aspect="a", text="CVE-2026-9999 の悪用が続く [1]。")]
        )
        good = staged._StageOutlook(
            outlook_points=[OutlookPoint(aspect="a", text="CVE-2026-0001 の悪用が続く [1]。")]
        )
        llm = _SeqLLM(_stage_replies(_StageOutlook=[bad, good]))
        res = await staged.generate_staged(llm, **_ctx())  # type: ignore[arg-type]
        names = [c for c, _ in llm.calls]
        assert names == ["_StageEvents", "_StageNotes", "_StageOutlook", "_StageOutlook"]
        assert "CVE-2026-9999" in llm.calls[-1][1]  # 書き直しのヒントが具体値を示す
        assert "9999" not in res.outlook and "CVE-2026-0001" in res.outlook
        assert res.counts["identifier_retry"] == 1 and res.counts["identifier_unverified"] == 0

    @pytest.mark.asyncio
    async def test_identifier_rewrite_that_empties_the_tail_is_not_adopted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SPOTLIGHT_TAIL_GATE", "1")
        monkeypatch.setenv("SPOTLIGHT_IDENTIFIER_GATE", "1")
        full = staged._StageNotes(
            event_notes=[
                StagedNote(index=1, kind="caveat", text="範囲は限定的とされる。", anchor=ANCHOR_1),
                StagedNote(
                    index=2, kind="unknown", text="CVE-2026-7777 の経路は不明。", anchor=ANCHOR_2
                ),
                StagedNote(index=1, kind="unknown", text="被害件数は未確認。", anchor=ANCHOR_1),
            ]
        )
        llm = _SeqLLM(_stage_replies(_StageNotes=[full, staged._StageNotes()]))
        res = await staged.generate_staged(llm, **_ctx())  # type: ignore[arg-type]
        assert len(res.unknowns) == 2 and len(res.caveats) >= 2  # 書き直し前を残す
        assert res.counts["identifier_retry"] == 1 and res.counts["identifier_unverified"] == 1

    @pytest.mark.asyncio
    async def test_tail_resample_exception_keeps_best_so_far(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SPOTLIGHT_TAIL_GATE", "1")
        monkeypatch.setenv("SPOTLIGHT_IDENTIFIER_GATE", "0")

        class _Boom(_SeqLLM):
            async def generate_structured(self, prompt: str, *, schema: Any, **kw: Any) -> Any:
                if (
                    schema.__name__ == "_StageNotes"
                    and self.calls
                    and any(c == "_StageNotes" for c, _ in self.calls)
                ):
                    raise RuntimeError("secret body text")
                return await super().generate_structured(prompt, schema=schema, **kw)

        llm = _Boom(_stage_replies(_StageNotes=[staged._StageNotes()]))
        res = await staged.generate_staged(llm, **_ctx())  # type: ignore[arg-type]
        assert res.counts["tail_resamples"] == 1 and res.counts["tail_unmet"] == 1
        assert res.headline

    @pytest.mark.asyncio
    async def test_identifier_rewrite_of_events_keeps_the_selection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SPOTLIGHT_TAIL_GATE", "0")
        monkeypatch.setenv("SPOTLIGHT_IDENTIFIER_GATE", "1")
        m = _merged()
        bad = staged._StageEvents(headline="CVE-2026-8888 の悪用", key_events=m.key_events)
        retry = staged._StageEvents(
            headline="VPN の欠陥の悪用", key_events=[StagedKeyEvent(index=3, rationale="r")]
        )
        llm = _SeqLLM(_stage_replies(_StageEvents=[bad, retry]))
        res = await staged.generate_staged(llm, **_ctx())  # type: ignore[arg-type]
        assert res.headline == "VPN の欠陥の悪用"
        assert res.key_event_indexes == (1, 2)


class TestStagedPromptRestoredContext:
    def test_each_stage_carries_the_production_context(self) -> None:
        pir = _FakePir(strong_signals=_FakeSignals(["APT-X"], ["energy"], ["JP"]))
        cands = [
            dict(c, article_id=f"id{i}", url=f"https://kuebiko.example/{i}")
            for i, c in enumerate(_cands())
        ]
        prev = {"period_label": "前期", "headline": "前期の見出し", "outlook": "前期の見通し"}
        fresh = {"dated": 10, "retrospective": 4, "retrospective_pct": 40}
        for stage in ("events", "notes", "outlook"):
            p = staged.build_stage_prompt(
                stage,
                pir=pir,
                period_label="p",
                candidate_articles=cands,
                ledger_situations=[],
                previous_spotlight=prev,
                freshness=fresh,
            )
            for needle in (
                "APT-X",
                "article_id: id0",
                "url: https://kuebiko.example/0",
                "前期の見出し",
                "振り返り報道",
                "観測技術」に挙がったものだけ",
                "Admiralty",
            ):
                assert needle in p, (stage, needle)
        events = staged.build_stage_prompt(
            "events", pir=pir, period_label="p", candidate_articles=cands, ledger_situations=[]
        )
        assert "importance=high の事象を **優先的に**" in events and "1 件に集約" in events
        notes = staged.build_stage_prompt(
            "notes", pir=pir, period_label="p", candidate_articles=cands, ledger_situations=[]
        )
        assert "初期侵入経路" in notes and "数値の出所" in notes
