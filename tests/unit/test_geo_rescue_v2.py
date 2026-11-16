"""地政学の救済 v2 (GEO_RESCUE_V2) の本番組み込みのテスト。LLM は fake。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

import src.pipeline.geo_rescue_judge as gj
from src.pipeline.filters import _rescue_geopolitical
from src.pipeline.geo_rescue_judge import (
    Q_ALLIED_RESPONSE,
    Q_CYBER_GEO,
    Q_WATCHED_ACTION,
    GeoJudgeResult,
    judged_rescued_gated,
)
from src.pipeline.geo_rescue_v2 import geo_rescue_v2_mode
from src.tools.article_model import Article
from src.tools.article_triage import ArticleTriage, TriageDecision

KEEP = {"medium", "high"}
_GEO_TITLE = "Russia navy drills near Baltic"  # A〜C の手掛かりあり
_CYBER_TITLE = "National cyber strategy published by ministry"  # D の手掛かりのみ
_NO_CUE_TITLE = "Company launches new phone"


def _article(title: str, i: str = "a") -> Article:
    return Article(
        id=i,
        url=f"https://example.com/{i}",
        title=title,
        summary_html="",
        published=datetime(2026, 11, 16, tzinfo=UTC),
        feed_title="Feed",
        feed_url="https://example.com/feed",
    )


def _decisions(title: str) -> list[tuple[Article, str, bool, str]]:
    return [(_article(title), "low", False, "地政学のため")]


class _Fake:
    """flat と pir の判定 (importance) と問いの P(Yes) を固定する。"""

    def __init__(self, mp: pytest.MonkeyPatch, *, flat: str, pir: str, q: dict[str, float | None]):
        self.calls: list[str] = []
        self.pir_calls = 0
        self.llm: Any = AsyncMock()

        async def _triage(this: ArticleTriage, article: Article) -> TriageDecision:
            is_pir = bool(getattr(this, "_force_pir", False))
            self.pir_calls += int(is_pir)
            return TriageDecision(importance=cast(Any, pir if is_pir else flat), reason="r")

        async def _probe(step: Any, prompt: str, choices: Any, *, config: Any) -> Any:
            for key, question in (
                ("qw", Q_WATCHED_ACTION),
                ("qc", Q_ALLIED_RESPONSE),
                ("qd", Q_CYBER_GEO),
            ):
                if question in prompt:
                    self.calls.append(key)
                    p = q.get(key)
                    return None if p is None else {"Yes": p, "No": 1 - p}
            raise AssertionError("unknown question")

        mp.setattr(ArticleTriage, "triage", _triage)
        mp.setattr(gj, "probe_choice", _probe)


async def _run(title: str, **kw: Any) -> tuple[list[tuple[Article, str, bool, str]], set[str]]:
    return await _rescue_geopolitical(
        _decisions(title),
        kw.pop("llm"),
        keep_importance=KEEP,
        think=False,
        v2_config=object(),  # type: ignore[arg-type]
        **kw,
    )


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRIAGE_FLAT", "1")
    monkeypatch.setenv("PIR_DRIVEN_TRIAGE", "1")
    monkeypatch.delenv("GEO_RESCUE_PROMPT", raising=False)
    monkeypatch.delenv("GEO_RESCUE_V2", raising=False)


def test_mode_defaults_to_zero_and_unknown_is_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    assert geo_rescue_v2_mode() == "0"
    monkeypatch.setenv("GEO_RESCUE_V2", "SHADOW")
    assert geo_rescue_v2_mode() == "shadow"
    monkeypatch.setenv("GEO_RESCUE_V2", "1")
    assert geo_rescue_v2_mode() == "1"
    monkeypatch.setenv("GEO_RESCUE_V2", "yes")
    assert geo_rescue_v2_mode() == "0"


def test_gated_formula() -> None:
    geo = GeoJudgeResult(cues=("nation:RU",), p_watched_action=None, p_allied_response=None)
    none = GeoJudgeResult(cues=(), p_watched_action=None, p_allied_response=None)
    assert judged_rescued_gated(flat_rescued=True, pir_rescued=False, judge=geo)
    assert not judged_rescued_gated(flat_rescued=True, pir_rescued=False, judge=none)
    assert not judged_rescued_gated(flat_rescued=False, pir_rescued=False, judge=geo)


@pytest.mark.asyncio
async def test_flag_off_matches_legacy(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _Fake(monkeypatch, flat="low", pir="high", q={"qw": 0.9})
    _out, rescued = await _run(_GEO_TITLE, llm=fake.llm)
    assert rescued == set()
    assert fake.pir_calls == 0 and fake.calls == []


@pytest.mark.asyncio
async def test_flat_rescued_without_cue_is_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEO_RESCUE_V2", "1")
    fake = _Fake(monkeypatch, flat="medium", pir="medium", q={})
    out, rescued = await _run(_NO_CUE_TITLE, llm=fake.llm)
    assert rescued == set() and out[0][1] == "low"
    assert fake.calls == [] and fake.pir_calls == 0


@pytest.mark.asyncio
async def test_flat_rescued_with_geo_cue_kept_without_questions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEO_RESCUE_V2", "1")
    fake = _Fake(monkeypatch, flat="high", pir="low", q={})
    out, rescued = await _run(_GEO_TITLE, llm=fake.llm)
    assert rescued == {"a"} and out[0][1] == "medium"
    assert fake.calls == []


@pytest.mark.parametrize(("qd", "expected"), [(0.9, {"a"}), (0.1, set())])
@pytest.mark.asyncio
async def test_flat_rescued_cyber_only_needs_qd(
    monkeypatch: pytest.MonkeyPatch, qd: float, expected: set[str]
) -> None:
    monkeypatch.setenv("GEO_RESCUE_V2", "1")
    fake = _Fake(monkeypatch, flat="medium", pir="low", q={"qd": qd})
    _out, rescued = await _run(_CYBER_TITLE, llm=fake.llm)
    assert rescued == expected
    assert fake.calls == ["qd"]


@pytest.mark.asyncio
async def test_pir_rescued_with_geo_cue_and_qw_is_rescued(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEO_RESCUE_V2", "1")
    fake = _Fake(monkeypatch, flat="low", pir="medium", q={"qw": 0.9, "qc": 0.1})
    _out, rescued = await _run(_GEO_TITLE, llm=fake.llm)
    assert rescued == {"a"}
    assert fake.pir_calls == 1


@pytest.mark.asyncio
async def test_pir_rescued_without_cue_is_not_rescued(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEO_RESCUE_V2", "1")
    fake = _Fake(monkeypatch, flat="low", pir="high", q={"qw": 0.9})
    _out, rescued = await _run(_NO_CUE_TITLE, llm=fake.llm)
    assert rescued == set() and fake.calls == []


@pytest.mark.asyncio
async def test_question_failure_falls_back_to_flat(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEO_RESCUE_V2", "1")
    # pir は救うが問いが全て読めない -> flat (low) のまま
    fake = _Fake(monkeypatch, flat="low", pir="medium", q={"qw": None, "qc": None})
    _out, rescued = await _run(_GEO_TITLE, llm=fake.llm)
    assert rescued == set()


@pytest.mark.asyncio
async def test_question_failure_keeps_flat_rescue(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEO_RESCUE_V2", "1")
    # flat が救い D の手掛かりのみ、QD が読めない -> 従来どおり flat の救済のまま
    fake = _Fake(monkeypatch, flat="medium", pir="low", q={"qd": None})
    _out, rescued = await _run(_CYBER_TITLE, llm=fake.llm)
    assert rescued == {"a"}


@pytest.mark.asyncio
async def test_low_budget_falls_back_to_flat(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEO_RESCUE_V2", "1")
    fake = _Fake(monkeypatch, flat="medium", pir="low", q={})
    # 手掛かり無しの flat 救済は v2 なら落ちるが、予算切れなら従来どおり救う
    _out, rescued = await _run(_NO_CUE_TITLE, llm=fake.llm, v2_remaining=lambda: 5.0)
    assert rescued == {"a"}
    assert fake.calls == []


@pytest.mark.asyncio
async def test_shadow_does_not_change_output(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEO_RESCUE_V2", "shadow")
    fake = _Fake(monkeypatch, flat="medium", pir="low", q={})
    out, rescued = await _run(_NO_CUE_TITLE, llm=fake.llm)
    assert rescued == {"a"} and out[0][1] == "medium"  # v2 なら落ちるが shadow は従来どおり
    fake2 = _Fake(monkeypatch, flat="low", pir="high", q={"qw": 0.9})
    _out, rescued2 = await _run(_GEO_TITLE, llm=fake2.llm)
    assert rescued2 == set()  # v2 なら救うが shadow は従来どおり
