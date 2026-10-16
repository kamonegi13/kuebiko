"""借用を解消した step が、借り元と完全に同じ挙動であることを固定する (2026-10-16)。

最初は挙動を変えない: 解決先のモデル・timeout・課題の印・未学習 step の扱いが借り元と
同一。印を外す/独立に割り当てる判断は s24 の評価後で、そのときこの表と本テストを更新する。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from src.storage.config_store import save_config
from src.tools.model_tiers import (
    MODEL_TIERS_CONFIG_KEY,
    STEP_INHERITS,
    STEP_REGISTRY,
    Step,
    build_llm_for,
    invalidate_model_tiers_cache,
    resolve_step_model,
)
from src.tools.task_prefix import (
    PREFIX_TRAINED_MODELS,
    UNTRAINED_STEPS,
    TaskPrefixClient,
    client_for_step,
    prefix_for,
    with_task_prefix,
)

PAIRS = [
    (Step.IOC_LLM_VERIFY, Step.ARTICLE_SUMMARY),
    (Step.DETECT_CLAIM_WRITE, Step.SYNTHESIS_DETECT),
    (Step.LEDGER_ACH_AUX, Step.SYNTHESIS_ANALYSIS),
]

# 本番 model_tiers (2026-10-12 版 40) の形
PROD_DOC = {
    "reasoning": "claudecode:claude-sonnet-5",
    "narrative": "kuebiko-sft:n19",
    "fast": "kuebiko-sft:s23i",
    "dialog": "claudecode:haiku",
    "embedding": "snowflake-arctic-embed2",
    "step:synthesis_analysis": "kuebiko-sft:s23i",
    "step:synthesis_detect": "kuebiko-sft:s23i",
    "step:event_news": "kuebiko-sft:n19",
}


class _Cfg:
    ollama_base_url = "http://localhost:11434"
    claude_code_bridge_url = "http://host.docker.internal:8010"
    anthropic_api_key = ""


@pytest.fixture()
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("MODEL_TIERS_CONFIG_DB", raising=False)
    invalidate_model_tiers_cache()
    yield tmp_path / "borrowed.db"
    invalidate_model_tiers_cache()


def test_inherit_table_covers_exactly_the_pairs() -> None:
    assert dict(PAIRS) == STEP_INHERITS


@pytest.mark.parametrize(("new", "old"), PAIRS)
def test_tier_timeout_and_marker_match_donor(new: Step, old: Step) -> None:
    assert STEP_REGISTRY[new] == STEP_REGISTRY[old]
    assert prefix_for(new) == prefix_for(old) != ""


@pytest.mark.parametrize(("new", "old"), PAIRS)
@pytest.mark.parametrize(
    "model", sorted(PREFIX_TRAINED_MODELS | {"gemma4:26b", "kuebiko-sft:n17c"})
)
def test_prefixed_prompt_identical_for_every_model(new: Step, old: Step, model: str) -> None:
    # UNTRAINED_STEPS の扱いも含め、どのモデルでも付く印 (または付かない結果) が同じ
    assert with_task_prefix("本文", new, model) == with_task_prefix("本文", old, model)
    for untrained in UNTRAINED_STEPS.values():
        assert (new in untrained) == (old in untrained)


@pytest.mark.parametrize(("new", "old"), PAIRS)
def test_resolution_matches_donor_under_production_overrides(
    db_path: Path, new: Step, old: Step
) -> None:
    save_config(MODEL_TIERS_CONFIG_KEY, PROD_DOC, db_path=db_path)
    assert resolve_step_model(new, db_path=db_path) == resolve_step_model(old, db_path=db_path)


def test_production_form_resolves_to_s23i(db_path: Path) -> None:
    save_config(MODEL_TIERS_CONFIG_KEY, PROD_DOC, db_path=db_path)
    assert resolve_step_model(Step.IOC_LLM_VERIFY, db_path=db_path) == "kuebiko-sft:s23i"
    assert resolve_step_model(Step.DETECT_CLAIM_WRITE, db_path=db_path) == "kuebiko-sft:s23i"
    assert resolve_step_model(Step.LEDGER_ACH_AUX, db_path=db_path) == "kuebiko-sft:s23i"


def test_own_override_wins_over_inherited(db_path: Path) -> None:
    doc = {**PROD_DOC, "step:ledger_ach_aux": "gemma4:26b"}
    save_config(MODEL_TIERS_CONFIG_KEY, doc, db_path=db_path)
    assert resolve_step_model(Step.LEDGER_ACH_AUX, db_path=db_path) == "gemma4:26b"
    assert resolve_step_model(Step.SYNTHESIS_ANALYSIS, db_path=db_path) == "kuebiko-sft:s23i"


def test_no_override_falls_back_to_tier_like_donor(db_path: Path) -> None:
    # 借り元に上書きが無ければティア解決 (= 借り元と同じ)
    for new, old in PAIRS:
        assert resolve_step_model(new, db_path=db_path) == resolve_step_model(old, db_path=db_path)


def test_client_for_step_rebuilds_with_same_model_and_marker(db_path: Path) -> None:
    save_config(MODEL_TIERS_CONFIG_KEY, PROD_DOC, db_path=db_path)
    donor = build_llm_for(Step.SYNTHESIS_ANALYSIS, _Cfg(), db_path=db_path)  # type: ignore[arg-type]
    aux = client_for_step(donor, Step.LEDGER_ACH_AUX)
    assert isinstance(aux, TaskPrefixClient)
    assert aux.model == donor.model == "kuebiko-sft:s23i"
    assert aux._step is Step.LEDGER_ACH_AUX  # noqa: SLF001
    assert client_for_step(donor, Step.LEDGER_ACH_AUX) is aux  # キャッシュ
    assert isinstance(donor, TaskPrefixClient)
    assert donor.for_step(Step.SYNTHESIS_ANALYSIS) is donor


def test_client_for_step_keeps_inner_for_explicit_ref_and_ignores_plain_clients() -> None:
    sentinel: Any = object()
    assert client_for_step(sentinel, Step.IOC_LLM_VERIFY) is sentinel
    inner: Any = type("C", (), {"model": "kuebiko-sft:s23i"})()
    explicit = TaskPrefixClient(inner, Step.ARTICLE_SUMMARY)
    rebound = client_for_step(explicit, Step.IOC_LLM_VERIFY)
    assert isinstance(rebound, TaskPrefixClient)
    assert rebound._inner is inner  # noqa: SLF001
