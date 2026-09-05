"""step 単位のモデル割当 (step override) のテスト。

2 族分割 (2026-09-06、docs/research/llm_training/SYNTHESIS.md §8): 特化 SFT モデルは
ティアより細かい粒度で効く — 同じ fast ティアでも triage/article_summary は S 族、
pir_llm_judge 等は当面 base のまま。ティア一括割当では表現できないため、
model_tiers config doc 内の予約キー ``step:<step名>`` で step 単位の上書きを持つ。

設計上の不変条件:
- 予約キーは **フラット** (``step:triage``)。ネスト dict は ``_load_tier_map`` の
  str→str フィルタで黙って落ちるため使わない。
- override 不在・空文字は従来どおりティア解決 (挙動保存 = 既存環境で migration 不要)。
- 保存検証は未知 step 名を拒否し (typo の fail-fast)、中華系 denylist は
  ティア割当と同じ 3 層で効く。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from src.storage.config_store import save_config
from src.tools.llm_client import OllamaClient
from src.tools.model_tiers import (
    MODEL_TIERS_CONFIG_KEY,
    Step,
    build_llm_for,
    invalidate_model_tiers_cache,
    load_step_overrides,
    resolve_step_model,
    validate_model_tiers,
)


def _cfg() -> Any:
    class _Cfg:
        ollama_base_url = "http://localhost:11434"
        claude_code_bridge_url = "http://host.docker.internal:8010"
        anthropic_api_key = ""

    return _Cfg()


@pytest.fixture()
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("MODEL_TIERS_CONFIG_DB", raising=False)
    invalidate_model_tiers_cache()
    yield tmp_path / "test_step_override.db"
    invalidate_model_tiers_cache()


class TestResolveStepModel:
    def test_without_override_falls_back_to_tier(self, db_path: Path) -> None:
        # override 未設定 = 従来のティア解決 (挙動保存)
        assert resolve_step_model(Step.TRIAGE, db_path=db_path) == "gemma4:26b"
        assert resolve_step_model(Step.PIR_SPOTLIGHT, db_path=db_path) == "gemma4:31b"

    def test_override_wins_over_tier(self, db_path: Path) -> None:
        save_config(
            MODEL_TIERS_CONFIG_KEY,
            {"step:triage": "kuebiko-sft:s1", "step:pir_spotlight": "kuebiko-sft:26b"},
            db_path=db_path,
        )
        invalidate_model_tiers_cache()
        assert resolve_step_model(Step.TRIAGE, db_path=db_path) == "kuebiko-sft:s1"
        assert resolve_step_model(Step.PIR_SPOTLIGHT, db_path=db_path) == "kuebiko-sft:26b"
        # override の無い step は同ティアでもティア割当のまま
        assert resolve_step_model(Step.ARTICLE_SUMMARY, db_path=db_path) == "gemma4:26b"
        assert resolve_step_model(Step.EVENT_NEWS, db_path=db_path) == "gemma4:31b"

    def test_empty_override_means_absent(self, db_path: Path) -> None:
        # 空文字 = 解除 (UI で消したときの表現)。ティアに戻る。
        save_config(MODEL_TIERS_CONFIG_KEY, {"step:triage": ""}, db_path=db_path)
        invalidate_model_tiers_cache()
        assert resolve_step_model(Step.TRIAGE, db_path=db_path) == "gemma4:26b"


class TestBuildLlmForUsesOverride:
    def test_build_llm_for_resolves_step_override(self, db_path: Path) -> None:
        save_config(MODEL_TIERS_CONFIG_KEY, {"step:triage": "kuebiko-sft:s1"}, db_path=db_path)
        invalidate_model_tiers_cache()
        llm = build_llm_for(Step.TRIAGE, _cfg(), db_path=db_path)
        assert isinstance(llm, OllamaClient)
        assert llm.model == "kuebiko-sft:s1"
        # 同ティアの他 step は巻き込まれない
        other = build_llm_for(Step.ARTICLE_SUMMARY, _cfg(), db_path=db_path)
        assert other.model == "gemma4:26b"


class TestLoadStepOverrides:
    def test_returns_only_step_keys(self, db_path: Path) -> None:
        save_config(
            MODEL_TIERS_CONFIG_KEY,
            {"fast": "gemma4:26b", "step:triage": "kuebiko-sft:s1", "step:event_news": ""},
            db_path=db_path,
        )
        invalidate_model_tiers_cache()
        # UI GET 用: 空文字 (解除) は返さない
        assert load_step_overrides(db_path=db_path) == {"triage": "kuebiko-sft:s1"}


class TestValidation:
    def test_valid_override_passes(self) -> None:
        raw = dict(_BASE_TIERS, **{"step:triage": "kuebiko-sft:s1"})
        assert validate_model_tiers(raw) == []

    def test_unknown_step_name_rejected(self) -> None:
        raw = dict(_BASE_TIERS, **{"step:no_such_step": "gemma4:26b"})
        errs = validate_model_tiers(raw)
        assert any("no_such_step" in e for e in errs)

    def test_embed_step_not_overridable(self) -> None:
        raw = dict(_BASE_TIERS, **{"step:embed": "gemma4:26b"})
        errs = validate_model_tiers(raw)
        assert any("embed" in e for e in errs)

    def test_forbidden_model_rejected_in_override(self) -> None:
        # 中華系 denylist はティア割当と同じく step override にも効く (CLAUDE.md §4)
        raw = dict(_BASE_TIERS, **{"step:triage": "qwen2:7b"})
        errs = validate_model_tiers(raw)
        assert errs != []

    def test_empty_override_allowed(self) -> None:
        # 空文字 = 解除は保存可
        raw = dict(_BASE_TIERS, **{"step:triage": ""})
        assert validate_model_tiers(raw) == []

    def test_non_string_override_rejected(self) -> None:
        raw = dict(_BASE_TIERS, **{"step:triage": 123})
        errs = validate_model_tiers(raw)
        assert any("triage" in e for e in errs)


class TestMergePreservingStepOverrides:
    """UI のティア保存 (step キーなし) が既存 override を消さないこと。"""

    def test_tier_only_save_inherits_existing_overrides(self) -> None:
        from src.tools.model_tiers import merge_preserving_step_overrides

        doc = dict(_BASE_TIERS)
        merged = merge_preserving_step_overrides(doc, {"triage": "kuebiko-sft:s1"})
        assert merged["step:triage"] == "kuebiko-sft:s1"
        assert merged["fast"] == "gemma4:26b"
        assert doc == _BASE_TIERS  # 入力は変異させない

    def test_explicit_step_keys_take_full_control(self) -> None:
        from src.tools.model_tiers import merge_preserving_step_overrides

        doc = dict(_BASE_TIERS, **{"step:event_news": "kuebiko-sft:26b"})
        merged = merge_preserving_step_overrides(doc, {"triage": "kuebiko-sft:s1"})
        # doc が step キーを持つ = 明示管理。含めなかった override は削除の意思
        assert "step:triage" not in merged
        assert merged["step:event_news"] == "kuebiko-sft:26b"


_BASE_TIERS: dict[str, str] = {
    "reasoning": "gemma4:31b",
    "narrative": "gemma4:31b",
    "fast": "gemma4:26b",
    "dialog": "gemma4:26b",
    "embedding": "snowflake-arctic-embed2",
}
