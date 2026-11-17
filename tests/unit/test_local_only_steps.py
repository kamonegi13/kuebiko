"""ローカルだけで動く step (翻訳・MITRE 和訳)。narrative ティアに属し、外部モデルへは流さない。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from src.storage.config_store import save_config
from src.tools.model_registry import RESIDENT_SLOTS
from src.tools.model_tiers import (
    MODEL_TIERS_CONFIG_KEY,
    STEP_REGISTRY,
    Step,
    Tier,
    invalidate_model_tiers_cache,
    resolve_step_model,
)

_LOCAL_ONLY = (Step.ARTICLE_TRANSLATE, Step.ACTOR_SYNC)


@pytest.fixture()
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("MODEL_TIERS_CONFIG_DB", raising=False)
    invalidate_model_tiers_cache()
    yield tmp_path / "local_only.db"
    invalidate_model_tiers_cache()


@pytest.mark.parametrize("step", _LOCAL_ONLY)
def test_translation_steps_belong_to_narrative_and_are_local_only(step: Step) -> None:
    spec = STEP_REGISTRY[step]
    assert spec.tier is Tier.NARRATIVE
    assert spec.local_only is True


@pytest.mark.parametrize("step", _LOCAL_ONLY)
def test_resolves_to_n_slot_without_override(step: Step, db_path: Path) -> None:
    assert resolve_step_model(step, db_path=db_path) == RESIDENT_SLOTS["N"]


@pytest.mark.parametrize("step", _LOCAL_ONLY)
def test_external_narrative_tier_falls_back_to_local(step: Step, db_path: Path) -> None:
    save_config(MODEL_TIERS_CONFIG_KEY, {"narrative": "claudecode:sonnet"}, db_path=db_path)
    invalidate_model_tiers_cache()
    # 物語系は外部へ行くが、翻訳・和訳はローカルの受け皿に留まる
    assert resolve_step_model(Step.EVENT_NEWS, db_path=db_path) == "claudecode:sonnet"
    assert resolve_step_model(step, db_path=db_path) == RESIDENT_SLOTS["N"]


@pytest.mark.parametrize("step", _LOCAL_ONLY)
def test_external_step_override_is_also_blocked(step: Step, db_path: Path) -> None:
    save_config(
        MODEL_TIERS_CONFIG_KEY,
        {
            f"step:{step.value}": "anthropic:claude-sonnet-5-5",
            "fallback:narrative": "kuebiko-sft:n20",
        },
        db_path=db_path,
    )
    invalidate_model_tiers_cache()
    assert resolve_step_model(step, db_path=db_path) == "kuebiko-sft:n20"


def test_other_steps_are_not_local_only() -> None:
    assert STEP_REGISTRY[Step.EVENT_NEWS].local_only is False
    assert STEP_REGISTRY[Step.ARTICLE_SUMMARY].local_only is False
