"""深掘りの要点の構造 → コードが本文を組む形 (n20 T5、旗 DEEP_DIVE_SLOT_RENDER)。LLM は fake。"""

from __future__ import annotations

import json
from typing import Any, cast

import pytest

from src.digest.recap_points import (
    RecapPoint,
    RecapPointSection,
    RecapPointsOutput,
    dropped_points,
    to_recap,
)
from src.digest.recap_render import RecapOutput

KNOWN = frozenset({"a1", "a2", "a3"})


def _out() -> RecapPointsOutput:
    return RecapPointsOutput(
        sections=[
            RecapPointSection(
                heading="境界機器の悪用が続いた" + "長" * 40,
                points=[
                    RecapPoint(text="VPN の欠陥が悪用された", article_ids=["a1"]),
                    RecapPoint(text="同じ型の侵入が 2 件報じられた。", article_ids=["a1", "a2"]),
                    RecapPoint(text="作った id だけの要点", article_ids=["zz"]),
                    RecapPoint(text="", article_ids=["a3"]),
                ],
            ),
            RecapPointSection(heading="空の節", points=[RecapPoint(text="x", article_ids=[])]),
        ]
    )


class TestToRecap:
    def test_joins_points_and_unions_ids(self) -> None:
        recap = to_recap(_out(), known_ids=KNOWN)
        assert len(recap.sections) == 1  # 根拠の無い節は落ちる
        sec = recap.sections[0]
        assert sec.body == "VPN の欠陥が悪用された。同じ型の侵入が 2 件報じられた。"
        assert sec.article_ids == ["a1", "a2"]
        assert len(sec.heading) == 40

    def test_dropped_points_counted(self) -> None:
        assert dropped_points(_out(), known_ids=KNOWN) == 3

    def test_schema_all_required(self) -> None:
        schema = RecapPointsOutput.model_json_schema()
        assert schema["required"] == ["sections"]
        assert set(schema["$defs"]["RecapPoint"]["required"]) == {"text", "article_ids"}

    def test_shape_passes_the_n20_training_gate(self) -> None:
        import importlib.util
        from pathlib import Path

        spec = importlib.util.spec_from_file_location(
            "sft_gate_n20_schemas", Path("scripts/sft_gate_n20_schemas.py")
        )
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        good = RecapPointsOutput(
            sections=[
                RecapPointSection(
                    heading="見出し", points=[RecapPoint(text="要点", article_ids=["a1"])]
                )
            ]
        )
        assert mod.schema_errors_deep_dive(json.loads(good.model_dump_json()), "a1 a2") == []


def _cand(article_id: str) -> Any:
    from src.digest.db_filter import DigestCandidate

    return DigestCandidate(
        article_id=article_id,
        title=f"題 {article_id}",
        url=f"https://kuebiko.example/{article_id}",
        feed_title="Feed",
        summary="要約",
        importance="high",
        category="apt",
        posted_channel="watch",
        dedup_key=None,
        created_at="2026-10-20T00:00:00Z",
        discord_channel_id=None,
        discord_message_id=None,
    )


class _LLM:
    model = "fake"

    def __init__(self) -> None:
        self.calls: list[tuple[str, type]] = []

    async def generate_structured(self, *, prompt: str, schema: type, **_kw: Any) -> Any:
        self.calls.append((prompt, schema))
        if schema is RecapPointsOutput:
            return RecapPointsOutput(
                sections=[
                    RecapPointSection(
                        heading="節",
                        points=[RecapPoint(text="あ" * 450, article_ids=["a1", "a2"])],
                    )
                ]
            )
        from src.digest.recap_render import RecapSection

        return RecapOutput(
            sections=[RecapSection(heading="旧", body="い" * 600, article_ids=["a1", "a2"])]
        )


async def _run(llm: _LLM, template: str = "digest/weekly_recap.j2") -> str:
    from src.digest import llm_digest

    return await llm_digest.generate_digest(
        llm=cast(Any, llm),
        candidates=[_cand("a1"), _cand("a2")],
        template_name=template,
        period_label="P",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", [None, "0", "off"])
async def test_off_is_the_legacy_prose_path(
    monkeypatch: pytest.MonkeyPatch, flag: str | None
) -> None:
    if flag is None:
        monkeypatch.delenv("DEEP_DIVE_SLOT_RENDER", raising=False)
    else:
        monkeypatch.setenv("DEEP_DIVE_SLOT_RENDER", flag)
    llm = _LLM()
    md = await _run(llm)
    assert [s for _, s in llm.calls] == [RecapOutput]
    assert '"points"' not in llm.calls[0][0]
    assert "## 📌 旧" in md


@pytest.mark.asyncio
async def test_on_uses_points_and_code_renders(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEP_DIVE_SLOT_RENDER", "1")
    llm = _LLM()
    md = await _run(llm)
    assert [s for _, s in llm.calls] == [RecapPointsOutput]
    assert '"points"' in llm.calls[0][0]
    assert "## 📌 節" in md and "https://kuebiko.example/a2" in md


@pytest.mark.asyncio
async def test_shadow_keeps_prose_and_logs(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.digest import llm_digest

    monkeypatch.setenv("DEEP_DIVE_SLOT_RENDER", "shadow")
    seen: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(llm_digest._log, "info", lambda e, **kw: seen.append((e, kw)))
    llm = _LLM()
    md = await _run(llm)
    assert [s for _, s in llm.calls] == [RecapOutput, RecapPointsOutput]
    assert "## 📌 旧" in md
    shadow = [kw for e, kw in seen if e == "digest_recap_points_shadow"]
    assert shadow == [{"sections": 1, "uncited": 0, "thin_sections": 0, "selected": 2}]


@pytest.mark.asyncio
async def test_flag_ignored_for_other_templates(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.digest import llm_digest

    monkeypatch.setenv("DEEP_DIVE_SLOT_RENDER", "1")
    monkeypatch.setattr(llm_digest, "_render_prompt", lambda *a, **k: "PROMPT")
    llm = _LLM()
    await _run(llm, template="digest/other.j2")
    assert [s for _, s in llm.calls] == [RecapOutput]
