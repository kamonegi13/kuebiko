"""深掘りの LLM 呼出 2 本を別 step に分けた不変条件 (2026-09-20)。

選定 (rubric 採点 = 判定) と本文 (narrative = 生成) は課題の種類が違う。1 本の client を
両方に配ると、**本文の腕を替えたときに選定まで黙って替わる** (step 借用の再発)。

detect の実測がこの分離の根拠: 判定特化の s17 は 5 日中 2 日で開設 0 件 (全件棄却) と、
生成側の gemma4:26b と別の挙動を示した。選定も同型の課題なので巻き込ませない。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from src.config_loader import PipelineConfig
from src.storage.config_store import save_config
from src.tools.llm_client import LLMClient
from src.tools.model_tiers import (
    MODEL_TIERS_CONFIG_KEY,
    Step,
    build_llm_for,
    invalidate_model_tiers_cache,
)


@pytest.fixture()
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("MODEL_TIERS_CONFIG_DB", raising=False)
    invalidate_model_tiers_cache()
    yield tmp_path / "tiers.db"
    invalidate_model_tiers_cache()


def test_select_step_is_independent_of_the_narrative_step(db_path: Path) -> None:
    save_config(MODEL_TIERS_CONFIG_KEY, {"step:digest_deep_dive": "gemma4:31b"}, db_path=db_path)

    narrative = build_llm_for(Step.DIGEST_DEEP_DIVE, _cfg(), db_path=db_path)
    select = build_llm_for(Step.DIGEST_DEEP_DIVE_SELECT, _cfg(), db_path=db_path)

    assert narrative.model == "gemma4:31b"  # 本文だけ替えたのに
    assert select.model != "gemma4:31b"  # 選定は連れて行かれない


@pytest.mark.asyncio
async def test_runner_sends_select_llm_to_the_selector_and_llm_to_the_writer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.digest import runner as mod

    narrative_llm = AsyncMock(spec=LLMClient)
    select_llm = AsyncMock(spec=LLMClient)
    seen: dict[str, Any] = {}

    async def fake_select(*, llm: LLMClient, **_: Any) -> list[Any]:
        seen["select"] = llm
        return []

    monkeypatch.setattr(mod, "select_deep_dive_articles", fake_select)
    monkeypatch.setattr(
        mod,
        "fetch_for_deep_dive_candidates",
        lambda **_: type("R", (), {"candidates": ["x"], "stage_counts": {}})(),
    )
    monkeypatch.setattr(mod, "fetch_recent_brief_titles", lambda **_: [])

    await mod.run_digest_pipeline(
        config=_cfg(),
        pipeline=_pipeline(),
        llm=narrative_llm,
        publishers={},
        select_llm=select_llm,
        dry_run=True,
    )

    assert seen["select"] is select_llm  # 選定は選定用の client で走る
    narrative_llm.generate.assert_not_awaited()  # 候補ゼロなら本文は呼ばない


def _cfg() -> Any:
    from src.config_loader import AppConfig

    return AppConfig(ollama_base_url="http://localhost:11434")


def _pipeline() -> PipelineConfig:
    return PipelineConfig.model_validate(
        {
            "name": "weekly-recap",
            "source": {
                "type": "digest_weekly_recap",
                "max_articles": 30,
                "digest_lookback_hours": 168,
                "digest_max_items": 30,
            },
            "processor": {
                "extract_method": "trafilatura",
                "extract_min_length": 200,
                "target_language": "ja",
            },
        }
    )
