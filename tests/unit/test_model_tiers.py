"""モデル能力ティア (model_tiers) のテスト。

モデルの実割当は DB (config_store "model_tiers") が SSoT、bootstrap 既定は
コードの ``BUILTIN_MODEL_TIERS`` (.env 非経由 = channels / product_routing と同パターン)。
ここでは DB→BUILTIN の解決順・build_llm_for の step→model・whitelist 上流・保存検証を検証する。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from src.config_loader import AppConfig
from src.storage.config_store import save_config
from src.tools.llm_client import OllamaClient, is_model_allowed
from src.tools.model_tiers import (
    BUILTIN_MODEL_TIERS,
    MODEL_TIERS_CONFIG_KEY,
    STEP_REGISTRY,
    Step,
    Tier,
    build_llm_for,
    invalidate_model_tiers_cache,
    load_model_tiers,
    resolve_embedding_model,
    resolve_tier_model,
    seed_model_tiers_if_absent,
    validate_model_tiers,
)


def _cfg(base_url: str = "http://localhost:11434") -> Any:
    """build_llm_for が使う base_url のみ持つ簡易 config スタンドイン。

    モデル選択は DB/BUILTIN 由来なので config には base_url 以外不要。
    """

    class _Cfg:
        ollama_base_url = base_url
        claude_code_bridge_url = "http://host.docker.internal:8010"
        anthropic_api_key = ""

    return _Cfg()


@pytest.fixture()
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("MODEL_TIERS_CONFIG_DB", raising=False)
    invalidate_model_tiers_cache()
    yield tmp_path / "test_model_tiers.db"
    invalidate_model_tiers_cache()


class TestRegistry:
    def test_every_step_registered(self) -> None:
        # 新 step を enum に足したら registry も更新される安全網。
        assert set(STEP_REGISTRY.keys()) == set(Step)

    def test_reasoning_and_narrative_tier_steps(self) -> None:
        # think ティア分離 (2026-07-24): 構造化分析=reasoning / 散文生成=narrative。
        reasoning = {s for s, spec in STEP_REGISTRY.items() if spec.tier is Tier.REASONING}
        narrative = {s for s, spec in STEP_REGISTRY.items() if spec.tier is Tier.NARRATIVE}
        assert reasoning == {Step.SYNTHESIS_ANALYSIS}
        assert narrative == {
            Step.SYNTHESIS_NARRATIVE,
            Step.PIR_SPOTLIGHT,
            Step.LEDGER_DEEP_REVIEW,
        }

    def test_embed_is_embedding_tier(self) -> None:
        assert STEP_REGISTRY[Step.EMBED].tier is Tier.EMBEDDING


class TestBuiltin:
    def test_builtin_has_all_tiers(self) -> None:
        assert set(BUILTIN_MODEL_TIERS.keys()) == {t.value for t in Tier}

    def test_builtin_values(self) -> None:
        assert BUILTIN_MODEL_TIERS["reasoning"] == "gemma4:31b"
        assert BUILTIN_MODEL_TIERS["fast"] == "gemma4:26b"
        assert BUILTIN_MODEL_TIERS["dialog"] == "gemma4:26b"
        assert BUILTIN_MODEL_TIERS["embedding"] == "snowflake-arctic-embed2"

    def test_builtin_models_are_allowed(self) -> None:
        # BUILTIN は中華系であってはならない (whitelist 通過必須)。
        assert all(is_model_allowed(m) for m in BUILTIN_MODEL_TIERS.values())


class TestResolve:
    def test_empty_db_falls_back_to_builtin(self, db_path: Path) -> None:
        assert resolve_tier_model(Tier.FAST, db_path=db_path) == "gemma4:26b"
        assert resolve_tier_model(Tier.REASONING, db_path=db_path) == "gemma4:31b"
        assert resolve_tier_model(Tier.EMBEDDING, db_path=db_path) == "snowflake-arctic-embed2"

    def test_db_overrides_builtin(self, db_path: Path) -> None:
        save_config(MODEL_TIERS_CONFIG_KEY, {"fast": "mistral-small3.2:24b"}, db_path=db_path)
        invalidate_model_tiers_cache()
        assert resolve_tier_model(Tier.FAST, db_path=db_path) == "mistral-small3.2:24b"
        # DB 未指定ティアは BUILTIN に fallback
        assert resolve_tier_model(Tier.REASONING, db_path=db_path) == "gemma4:31b"

    def test_rollback_flag_ignores_db(self, db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        save_config(MODEL_TIERS_CONFIG_KEY, {"fast": "mistral-small3.2:24b"}, db_path=db_path)
        invalidate_model_tiers_cache()
        monkeypatch.setenv("MODEL_TIERS_CONFIG_DB", "0")
        # flag=0 → DB を飛ばして BUILTIN に直行
        assert resolve_tier_model(Tier.FAST, db_path=db_path) == "gemma4:26b"

    def test_resolve_embedding_model(self, db_path: Path) -> None:
        assert resolve_embedding_model(db_path=db_path) == "snowflake-arctic-embed2"

    def test_load_model_tiers_all(self, db_path: Path) -> None:
        assert load_model_tiers(db_path=db_path) == dict(BUILTIN_MODEL_TIERS)


class TestBuildLLM:
    @pytest.mark.parametrize(
        ("step", "expected_model", "expected_timeout"),
        [
            (Step.ARTICLE_SUMMARY, "gemma4:26b", 300.0),
            (Step.TRIAGE, "gemma4:26b", 300.0),
            (Step.SYNTHESIS_DETECT, "gemma4:26b", 900.0),
            (Step.PIR_DAILY_FOCUS, "gemma4:26b", 120.0),
            (Step.PIR_COMPILE, "gemma4:26b", 180.0),
            # spotlight は reasoning ティア (本番 31b 踏襲) だが timeout は step 固有 600s
            (Step.PIR_SPOTLIGHT, "gemma4:31b", 600.0),
            (Step.DIGEST_DEEP_DIVE, "gemma4:26b", 900.0),
            (Step.SYNTHESIS_NARRATIVE, "gemma4:31b", 900.0),
        ],
    )
    def test_step_resolves_to_builtin_model_and_timeout(
        self, db_path: Path, step: Step, expected_model: str, expected_timeout: float
    ) -> None:
        # 空 DB → BUILTIN。BUILTIN は本番実効モデルと一致するため behavior-preserving。
        client = build_llm_for(step, _cfg(), db_path=db_path)
        assert client.model == expected_model
        assert cast(OllamaClient, client)._timeout_seconds == expected_timeout  # noqa: SLF001

    def test_embedding_step_raises(self, db_path: Path) -> None:
        with pytest.raises(ValueError, match="埋込"):
            build_llm_for(Step.EMBED, _cfg(), db_path=db_path)


class TestSeed:
    def test_seed_if_absent(self, db_path: Path) -> None:
        assert seed_model_tiers_if_absent(db_path=db_path) is True
        invalidate_model_tiers_cache()
        assert resolve_tier_model(Tier.FAST, db_path=db_path) == "gemma4:26b"
        # 既投入なら no-op
        assert seed_model_tiers_if_absent(db_path=db_path) is False

    def test_db_value_pins_over_builtin(self, db_path: Path) -> None:
        # DB に固定した値は BUILTIN より優先 (UI 編集が SSoT)。
        save_config(MODEL_TIERS_CONFIG_KEY, {"fast": "llama3.1:8b"}, db_path=db_path)
        invalidate_model_tiers_cache()
        assert resolve_tier_model(Tier.FAST, db_path=db_path) == "llama3.1:8b"


class TestValidate:
    def test_valid(self) -> None:
        assert (
            validate_model_tiers(
                {
                    "reasoning": "gemma4:31b",
                    "fast": "gemma4:26b",
                    "dialog": "gemma4:26b",
                    "embedding": "snowflake-arctic-embed2",
                }
            )
            == []
        )

    def test_forbidden_model_rejected_per_tier(self) -> None:
        errs = validate_model_tiers(
            {
                "reasoning": "qwen3:32b",
                "fast": "gemma4:26b",
                "dialog": "gemma4:26b",
                "embedding": "",
            }
        )
        assert any("reasoning" in e for e in errs)

    def test_empty_reasoning_rejected(self) -> None:
        errs = validate_model_tiers(
            {"reasoning": "", "fast": "gemma4:26b", "dialog": "gemma4:26b", "embedding": ""}
        )
        assert any("reasoning" in e for e in errs)

    def test_empty_embedding_allowed(self) -> None:
        # 埋込は空 = 無効化を許容
        assert (
            validate_model_tiers(
                {
                    "reasoning": "gemma4:31b",
                    "fast": "gemma4:26b",
                    "dialog": "gemma4:26b",
                    "embedding": "",
                }
            )
            == []
        )

    def test_missing_tier_rejected(self) -> None:
        errs = validate_model_tiers({"fast": "gemma4:26b"})
        assert any("reasoning" in e for e in errs)

    def test_non_dict_rejected(self) -> None:
        assert validate_model_tiers(["gemma4:26b"])


class TestWhitelistSSoT:
    @pytest.mark.parametrize(
        "model",
        ["qwen3:32b", "deepseek-r1:14b", "baichuan2:13b", "ernie-4.5", "kimi-k2", "hunyuan-large"],
    )
    def test_forbidden_families_blocked(self, model: str) -> None:
        assert is_model_allowed(model) is False

    @pytest.mark.parametrize(
        "model",
        [
            "gemma4:31b",
            "gemma4:26b",
            "mistral-small3.2:24b",
            "llama3.1:8b",
            "snowflake-arctic-embed2",
        ],
    )
    def test_allowed_models_pass(self, model: str) -> None:
        assert is_model_allowed(model) is True

    def test_empty_blocked(self) -> None:
        assert is_model_allowed("") is False


class TestAnthropicDispatch:
    """外部 LLM 開放 (§4 改訂 2026-07-18): anthropic: prefix の dispatch と検証。"""

    @staticmethod
    def _cfg_with_key(api_key: str = "sk-test") -> Any:
        class _Cfg:
            ollama_base_url = "http://localhost:11434"
            anthropic_api_key = api_key

        return _Cfg()

    def test_anthropic_ref_builds_fallback_wrapped_client(self, db_path: Path) -> None:
        from src.tools.anthropic_client import AnthropicClient
        from src.tools.llm_fallback import FallbackLLMClient

        save_config(
            MODEL_TIERS_CONFIG_KEY,
            {"fast": "anthropic:claude-haiku-4-5"},
            db_path=db_path,
        )
        invalidate_model_tiers_cache()
        client = build_llm_for(Step.TRIAGE, self._cfg_with_key(), db_path=db_path)
        # 外部 ref はローカル fallback 付き wrapper で返る
        assert isinstance(client, FallbackLLMClient)
        assert isinstance(client._primary, AnthropicClient)  # noqa: SLF001
        assert client.model == "anthropic:claude-haiku-4-5"
        assert (
            client._primary._timeout_seconds  # noqa: SLF001
            == STEP_REGISTRY[Step.TRIAGE].timeout_seconds
        )
        # fallback はティアのローカル既定
        assert client._fallback.model == BUILTIN_MODEL_TIERS["fast"]  # noqa: SLF001

    def test_anthropic_ref_without_key_falls_back_to_local(self, db_path: Path) -> None:
        from src.tools.llm_client import OllamaClient

        save_config(
            MODEL_TIERS_CONFIG_KEY,
            {"fast": "anthropic:claude-haiku-4-5"},
            db_path=db_path,
        )
        invalidate_model_tiers_cache()
        # 構築失敗 (キー未設定) は fallback ON (既定) ならローカルで継続
        client = build_llm_for(Step.TRIAGE, self._cfg_with_key(api_key=""), db_path=db_path)
        assert isinstance(client, OllamaClient)
        assert client.model == BUILTIN_MODEL_TIERS["fast"]

    def test_anthropic_ref_without_key_raises_when_fallback_off(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.tools.llm_client import LLMError

        save_config(
            MODEL_TIERS_CONFIG_KEY,
            {"fast": "anthropic:claude-haiku-4-5"},
            db_path=db_path,
        )
        invalidate_model_tiers_cache()
        monkeypatch.setenv("LLM_LOCAL_FALLBACK", "0")
        with pytest.raises(LLMError, match="ANTHROPIC_API_KEY"):
            build_llm_for(Step.TRIAGE, self._cfg_with_key(api_key=""), db_path=db_path)

    def test_local_ref_still_builds_ollama(self, db_path: Path) -> None:
        from src.tools.llm_client import OllamaClient

        client = build_llm_for(Step.TRIAGE, self._cfg_with_key(), db_path=db_path)
        assert isinstance(client, OllamaClient)

    def test_validate_accepts_anthropic_on_fast(self) -> None:
        errs = validate_model_tiers(
            {
                "reasoning": "gemma4:31b",
                "fast": "anthropic:claude-haiku-4-5",
                "dialog": "gemma4:26b",
                "embedding": "snowflake-arctic-embed2",
            },
        )
        assert errs == []

    def test_validate_rejects_anthropic_on_embedding(self) -> None:
        errs = validate_model_tiers(
            {
                "reasoning": "gemma4:31b",
                "fast": "gemma4:26b",
                "dialog": "gemma4:26b",
                "embedding": "anthropic:claude-haiku-4-5",
            },
        )
        assert any("外部プロバイダ" in e for e in errs)

    def test_validate_blocks_forbidden_behind_external_prefix(self) -> None:
        # denylist はプロバイダ横断 — "anthropic:qwen-max" も弾く (§4)
        errs = validate_model_tiers(
            {
                "reasoning": "gemma4:31b",
                "fast": "anthropic:qwen-max",
                "embedding": "",
            },
        )
        assert any("禁止系" in e for e in errs)


class TestDialogTier:
    """対話ティア新設 (2026-07-19): 対話系 step の分離と挙動保存。"""

    def test_dialog_steps_mapped(self) -> None:
        dialog = {s for s, spec in STEP_REGISTRY.items() if spec.tier is Tier.DIALOG}
        assert dialog == {
            Step.PIR_COMPILE,
            Step.SELECTOR_PROPOSAL,
            Step.PRECISE_SEARCH,
            Step.ASSISTANT_CHAT,
        }

    def test_article_translate_is_fast_tier(self) -> None:
        # 2026-07-25 品質比較: ローカル 26B ≥ haiku → 翻訳は fast (ローカル既定)。
        # dialog に置くと外部モデル割当時にバックログ翻訳が外部消費になるため。
        assert STEP_REGISTRY[Step.ARTICLE_TRANSLATE].tier is Tier.FAST

    def test_dialog_builtin_matches_fast(self, db_path: Path) -> None:
        # 分離時の挙動保存: DB 未保存 (既存環境) では従来どおり fast と同モデルに解決
        assert resolve_tier_model(Tier.DIALOG, db_path=db_path) == BUILTIN_MODEL_TIERS["fast"]

    def test_assistant_chat_resolves_via_dialog_tier(self, db_path: Path) -> None:
        # dialog だけ差し替えても fast (収集系) は不変 — 新設の狙いそのもの
        save_config(MODEL_TIERS_CONFIG_KEY, {"dialog": "llama3.1:8b"}, db_path=db_path)
        invalidate_model_tiers_cache()
        chat = build_llm_for(Step.ASSISTANT_CHAT, _cfg(), db_path=db_path)
        summary = build_llm_for(Step.ARTICLE_SUMMARY, _cfg(), db_path=db_path)
        assert chat.model == "llama3.1:8b"
        assert summary.model == BUILTIN_MODEL_TIERS["fast"]


class TestClaudeCodeDispatch:
    """Claude Code サブスク経由 (claudecode: prefix) の dispatch と検証。"""

    def test_claudecode_ref_builds_bridge_client(self, db_path: Path) -> None:
        from src.tools.claude_code_client import ClaudeCodeClient

        save_config(MODEL_TIERS_CONFIG_KEY, {"dialog": "claudecode:haiku"}, db_path=db_path)
        invalidate_model_tiers_cache()

        class _Cfg:
            ollama_base_url = "http://localhost:11434"
            anthropic_api_key = ""
            claude_code_bridge_url = "http://host.docker.internal:8010"

        client = build_llm_for(Step.ASSISTANT_CHAT, cast(AppConfig, _Cfg()), db_path=db_path)
        from src.tools.llm_fallback import FallbackLLMClient

        assert isinstance(client, FallbackLLMClient)
        assert isinstance(client._primary, ClaudeCodeClient)  # noqa: SLF001
        assert client.model == "claudecode:haiku"

    def test_validate_rejects_claudecode_on_embedding(self) -> None:
        errs = validate_model_tiers(
            {
                "reasoning": "gemma4:31b",
                "fast": "gemma4:26b",
                "dialog": "gemma4:26b",
                "embedding": "claudecode:haiku",
            },
        )
        assert any("外部プロバイダ" in e for e in errs)

    def test_validate_accepts_claudecode_on_dialog(self) -> None:
        errs = validate_model_tiers(
            {
                "reasoning": "gemma4:31b",
                "fast": "gemma4:26b",
                "dialog": "claudecode:sonnet",
                "embedding": "",
            },
        )
        assert errs == []


class TestBuildForRef:
    """明示 model ref での構築 (分析チャット等の画面単位 override)。"""

    @staticmethod
    def _cfg() -> Any:
        class _C:
            ollama_base_url = "http://localhost:11434"
            anthropic_api_key = ""
            claude_code_bridge_url = "http://host.docker.internal:8010"

        return _C()

    def test_local_ref_uses_step_timeout(self) -> None:
        from src.tools.llm_client import OllamaClient
        from src.tools.model_tiers import build_llm_for_ref

        client = build_llm_for_ref("llama3.1:8b", Step.ASSISTANT_CHAT, self._cfg())
        assert isinstance(client, OllamaClient)
        assert client.model == "llama3.1:8b"
        assert (
            client._timeout_seconds  # noqa: SLF001
            == STEP_REGISTRY[Step.ASSISTANT_CHAT].timeout_seconds
        )

    def test_claudecode_ref_wrapped_with_fallback(self) -> None:
        from src.tools.llm_fallback import FallbackLLMClient
        from src.tools.model_tiers import build_llm_for_ref

        client = build_llm_for_ref("claudecode:sonnet", Step.ASSISTANT_CHAT, self._cfg())
        assert isinstance(client, FallbackLLMClient)
        assert client.model == "claudecode:sonnet"

    def test_forbidden_ref_rejected(self) -> None:
        from src.tools.llm_client import LLMForbiddenModelError
        from src.tools.model_tiers import build_llm_for_ref

        with pytest.raises(LLMForbiddenModelError):
            build_llm_for_ref("qwen3:32b", Step.ASSISTANT_CHAT, self._cfg())
        with pytest.raises(LLMForbiddenModelError):
            build_llm_for_ref("claudecode:qwen-max", Step.ASSISTANT_CHAT, self._cfg())


def test_is_external_model_by_prefix() -> None:
    """narrative think 方針の判定: 外部 prefix (composite 名含む) のみ True。"""
    from src.tools.model_tiers import is_external_model

    assert is_external_model("claudecode:sonnet") is True
    assert is_external_model("anthropic:claude-sonnet-5") is True
    # fallback 発動後の composite 名も外部扱い (ローカルアームは llm_fallback がクランプ)
    assert is_external_model("claudecode:sonnet→gemma4:31b") is True
    assert is_external_model("gemma4:26b") is False
    assert is_external_model("gemma4:31b") is False


class TestNarrativeThink:
    """narrative ティアの think 方針 (2026-07-24 think ティア分離)。"""

    def test_resolve_defaults_to_auto(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        from src.tools.model_tiers import invalidate_model_tiers_cache, resolve_narrative_think

        invalidate_model_tiers_cache()
        assert resolve_narrative_think(db_path=tmp_path / "t.db") == "auto"

    def test_resolve_reads_saved_value(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        from src.tools.model_tiers import (
            MODEL_TIERS_CONFIG_KEY,
            invalidate_model_tiers_cache,
            resolve_narrative_think,
        )

        db = tmp_path / "t2.db"
        save_config(
            MODEL_TIERS_CONFIG_KEY,
            {**dict(jr_builtin()), "narrative_think": "off"},
            db_path=db,
        )
        invalidate_model_tiers_cache()
        assert resolve_narrative_think(db_path=db) == "off"

    def test_validate_rejects_bad_think_value(self) -> None:
        from src.tools.model_tiers import validate_model_tiers

        doc = {**dict(jr_builtin()), "narrative_think": "always"}
        assert any("narrative_think" in e for e in validate_model_tiers(doc))
        doc_ok = {**dict(jr_builtin()), "narrative_think": "off"}
        assert validate_model_tiers(doc_ok) == []

    def test_validate_allows_missing_narrative_key(self) -> None:
        """旧版 doc (narrative 分離前) の revert 互換 — BUILTIN fallback で解決する。"""
        from src.tools.model_tiers import validate_model_tiers

        legacy = {k: v for k, v in jr_builtin().items() if k != "narrative"}
        assert validate_model_tiers(legacy) == []

    def test_narrative_external_gets_think_wrapper(self, db_path: Path) -> None:
        """外部モデル + auto → ThinkOnClient 包装。off/ローカル/reasoning は包装なし。"""
        from src.tools.llm_fallback import ThinkOnClient

        save_config(MODEL_TIERS_CONFIG_KEY, {"narrative": "claudecode:sonnet"}, db_path=db_path)
        invalidate_model_tiers_cache()
        client = build_llm_for(Step.PIR_SPOTLIGHT, _cfg(), db_path=db_path)
        assert isinstance(client, ThinkOnClient)
        assert client.model == "claudecode:sonnet"

        # narrative_think=off → 包装なし
        save_config(
            MODEL_TIERS_CONFIG_KEY,
            {"narrative": "claudecode:sonnet", "narrative_think": "off"},
            db_path=db_path,
        )
        invalidate_model_tiers_cache()
        assert not isinstance(
            build_llm_for(Step.PIR_SPOTLIGHT, _cfg(), db_path=db_path), ThinkOnClient
        )

        # ローカル割当 (BUILTIN) → 包装なし
        save_config(MODEL_TIERS_CONFIG_KEY, {}, db_path=db_path)
        invalidate_model_tiers_cache()
        assert not isinstance(
            build_llm_for(Step.PIR_SPOTLIGHT, _cfg(), db_path=db_path), ThinkOnClient
        )

        # reasoning ティア (構造化分析) は外部でも包装なし = think 常時 OFF
        save_config(MODEL_TIERS_CONFIG_KEY, {"reasoning": "claudecode:sonnet"}, db_path=db_path)
        invalidate_model_tiers_cache()
        assert not isinstance(
            build_llm_for(Step.SYNTHESIS_ANALYSIS, _cfg(), db_path=db_path), ThinkOnClient
        )

        # 夜間精査 step は narrative ティア経由 = 外部+auto なら think 包装 (UI 1:1 原則)
        save_config(MODEL_TIERS_CONFIG_KEY, {"narrative": "claudecode:sonnet"}, db_path=db_path)
        invalidate_model_tiers_cache()
        assert isinstance(
            build_llm_for(Step.LEDGER_DEEP_REVIEW, _cfg(), db_path=db_path), ThinkOnClient
        )


def jr_builtin() -> dict[str, str]:
    from src.tools.model_tiers import BUILTIN_MODEL_TIERS

    return dict(BUILTIN_MODEL_TIERS)


class TestExternalChoices:
    """モデル選択肢の動的化 (2026-07-24): Models API 取得 + curated fallback。"""

    def test_fetched_ids_populate_both_providers(self) -> None:
        from src.ui.api.model_tiers import build_external_choices

        ext, cc = build_external_choices(
            ["claude-fable-5", "claude-sonnet-5"], key_enabled=True, bridge_enabled=True
        )
        assert "anthropic:claude-fable-5" in ext
        assert "anthropic:claude-sonnet-5" in ext
        # claudecode はエイリアス先頭 + エイリアス非カバー系列 (fable) のみ完全名を追加
        assert cc[0] == "claudecode:sonnet"
        assert "claudecode:claude-fable-5" in cc
        assert "claudecode:claude-sonnet-5" not in cc  # sonnet はエイリアスがカバー

    def test_latest_per_family_curation(self) -> None:
        """旧世代の完全名で dropdown を埋めない (利用者指摘 2026-07-24)。"""
        from src.ui.api.model_tiers import build_external_choices

        fetched = [
            "claude-sonnet-5",
            "claude-fable-5",
            "claude-opus-4-8",
            "claude-opus-4-7",
            "claude-sonnet-4-6",
            "claude-opus-4-5-20251101",
            "claude-haiku-4-5-20251001",
        ]
        ext, cc = build_external_choices(fetched, key_enabled=True, bridge_enabled=True)
        # anthropic は系列最新のみ (sonnet-5 / fable-5 / opus-4-8 / haiku-4-5)
        assert ext == [
            "anthropic:claude-sonnet-5",
            "anthropic:claude-fable-5",
            "anthropic:claude-opus-4-8",
            "anthropic:claude-haiku-4-5-20251001",
        ]
        # claudecode はエイリアス 3 + fable のみ
        assert cc == [
            "claudecode:sonnet",
            "claudecode:haiku",
            "claudecode:opus",
            "claudecode:claude-fable-5",
        ]

    def test_fetch_failure_falls_back_to_curated(self) -> None:
        from src.tools.model_tiers import ANTHROPIC_MODEL_CHOICES, CLAUDE_CODE_MODEL_CHOICES
        from src.ui.api.model_tiers import build_external_choices

        ext, cc = build_external_choices(None, key_enabled=True, bridge_enabled=True)
        assert ext == list(ANTHROPIC_MODEL_CHOICES)
        assert cc == list(CLAUDE_CODE_MODEL_CHOICES)

    def test_disabled_providers_are_empty(self) -> None:
        from src.ui.api.model_tiers import build_external_choices

        ext, cc = build_external_choices(
            ["claude-sonnet-5"], key_enabled=False, bridge_enabled=False
        )
        assert ext == []
        assert cc == []

    def test_denylist_filters_fetched_ids(self) -> None:
        from src.ui.api.model_tiers import build_external_choices

        ext, _cc = build_external_choices(
            ["claude-sonnet-5", "qwen-max"], key_enabled=True, bridge_enabled=False
        )
        assert ext == ["anthropic:claude-sonnet-5"]
