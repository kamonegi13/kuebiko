"""judgment_classifier の層分け不変量テスト (python_block kind、group 4 の 2 本目、2026-08-21)。

ioc_llm_verifier (tests/unit/test_ioc_llm_verifier_prompt_layering.py) と同じ核心を持つ:
**seed の blocks で合成した結果が legacy (frozen) 実装の出力と byte 一致**すること。
judgment_classifier は候補を「値そのもの」でなく「表示済み文字列」として渡す設計のため、
合成は最終文字列を組んでから既存と同じ ``.format()`` を通す (ioc_llm_verifier の
候補リスト直接埋め込みとは経路が違う — 詳細は judgment_classifier.py の層分けコメント)。

既存の judgment_classifier テスト (tests/unit/test_judgment_classifier.py、分類挙動・
_sanitize_named_actor 等) は一切変更しない — 本ファイルは追加のみ。
"""

from __future__ import annotations

from typing import TypeVar, cast

import pytest
from pydantic import BaseModel

import src.cti.judgment_classifier as j
from src.cti.actor_normalizer import ActorAlias
from src.cti.judgment_classifier import (
    SAMPLE_CONTEXT,
    SLOT_IDS,
    JudgmentOut,
    classify_judgment,
    compose_from_blocks,
)
from src.prompts.prompt_store import (
    build_prompt_environment,
    build_python_block_source,
    compose_python_block,
    invalidate_prompt_cache,
    load_seed_rubric,
    python_block_sample_targets,
    python_block_slot_ids,
    validate_python_block_rubric,
)
from src.prompts.registry import PromptSpec, get_spec
from src.prompts.rubric_model import RubricExample
from src.tools.llm_client import (
    DEFAULT_MAX_TOKENS,
    DEFAULT_TEMPERATURE,
    MAX_STRUCTURED_ATTEMPTS,
    LLMClient,
    LLMResponse,
)

_T = TypeVar("_T", bound=BaseModel)

_CANDS = [ActorAlias(id="qilin", canonical="Qilin"), ActorAlias(id="apt29", canonical="APT29")]


def _spec() -> PromptSpec:
    spec = get_spec("judgment_classifier")
    assert spec is not None
    return spec


# ---------- golden 用サンプル context (3 ケース以上: 候補あり / 候補なし / 長文本文) ----------

_CANDS_PRESENT: dict[str, str] = {
    "candidates": "qilin, apt29",
    "category": "apt",
    "published": "2026-08-21",
    "title": "kuebiko.example への標的型攻撃",
    "body": "kuebiko.example 系ネットワークへの不正アクセスに関する本文 (test)。",
}
_NO_CANDS: dict[str, str] = {
    "candidates": "(候補なし — subject_actor_id は空)",
    "category": "?",
    "published": "?",
    "title": "",
    "body": "",
}
_LONG_BODY: dict[str, str] = {
    "candidates": "qilin",
    "category": "breach",
    "published": "2026-01-01",
    "title": "長いタイトル" * 5,
    "body": "本文の繰り返し (kuebiko.example test)。" * 500,
}


class TestGolden:
    """核心不変量: seed の blocks で合成した結果が legacy (frozen) 実装と byte 一致。"""

    @pytest.mark.parametrize(
        "context",
        [_CANDS_PRESENT, _NO_CANDS, _LONG_BODY],
        ids=["candidates_present", "no_candidates", "long_body"],
    )
    def test_seed_composition_is_byte_identical_to_legacy(self, context: dict[str, str]) -> None:
        spec = _spec()
        rubric = load_seed_rubric(spec)
        assert rubric is not None
        blocks = {s.field_id: s.body for s in rubric.sections}
        assert compose_from_blocks(blocks, context) == j._build_prompt_legacy(context)  # noqa: SLF001

    def test_seed_covers_every_slot(self) -> None:
        spec = _spec()
        rubric = load_seed_rubric(spec)
        assert rubric is not None
        assert validate_python_block_rubric(spec, rubric) == []
        assert {s.field_id for s in rubric.sections} == set(SLOT_IDS)

    def test_sample_context_has_every_placeholder_non_empty(self) -> None:
        """保存前検証・preview 用のサンプル context が format() の全 key を非空で埋める。"""
        assert set(SAMPLE_CONTEXT) == {"candidates", "category", "published", "title", "body"}
        assert all(v.strip() for v in SAMPLE_CONTEXT.values())


class TestValidation:
    def test_missing_block_is_an_error(self) -> None:
        spec = _spec()
        rubric = load_seed_rubric(spec)
        assert rubric is not None
        broken = rubric.model_copy(
            update={"sections": [s for s in rubric.sections if s.field_id != "intent"]}
        )
        errors = validate_python_block_rubric(spec, broken)
        assert any("intent" in e for e in errors)

    def test_unknown_block_is_an_error_not_silent(self) -> None:
        spec = _spec()
        rubric = load_seed_rubric(spec)
        assert rubric is not None
        extra = rubric.sections[0].model_copy(update={"field_id": "no_such_slot"})
        broken = rubric.model_copy(update={"sections": [*rubric.sections, extra]})
        errors = validate_python_block_rubric(spec, broken)
        assert any("no_such_slot" in e for e in errors)

    def test_duplicate_block_id_is_an_error(self) -> None:
        spec = _spec()
        rubric = load_seed_rubric(spec)
        assert rubric is not None
        dup = rubric.sections[0]
        broken = rubric.model_copy(update={"sections": [*rubric.sections, dup]})
        errors = validate_python_block_rubric(spec, broken)
        assert any("重複" in e for e in errors)

    def test_examples_are_rejected(self) -> None:
        """python_block 方式は few-shot examples を使わない (block 方式と同じ制約)。"""
        spec = _spec()
        rubric = load_seed_rubric(spec)
        assert rubric is not None
        broken = rubric.model_copy(update={"examples": [RubricExample(label="x", json_text="{}")]})
        errors = validate_python_block_rubric(spec, broken)
        assert any("examples" in e for e in errors)


class TestBraceInjectionIsRejected:
    """prose block への生の ``{`` 混入は保存前検証 (サンプル context での実合成) が拒否する。

    compose_from_blocks は最終文字列を組んでから ``.format(**context)`` を通す設計のため、
    block に不一致な ``{``/``{}``/``{未知キー}`` が混入すると ValueError/IndexError/KeyError
    のいずれかで落ちる。validate_python_block_rubric はこれをサンプル context での実合成
    まで通して拾う (最後の砦)。
    """

    @pytest.mark.parametrize(
        "injected",
        ["途中に { が混入", "空括弧 {} も混入", "未知キー {no_such_key} も混入"],
        ids=["lone_brace", "empty_braces", "unknown_key"],
    )
    def test_stray_brace_in_block_fails_validation(self, injected: str) -> None:
        spec = _spec()
        rubric = load_seed_rubric(spec)
        assert rubric is not None
        broken = rubric.model_copy(
            update={
                "sections": [
                    s.model_copy(update={"body": s.body + "\n" + injected})
                    if s.field_id == "intro"
                    else s
                    for s in rubric.sections
                ]
            }
        )
        errors = validate_python_block_rubric(spec, broken)
        assert errors, f"生の {{ 混入 ({injected}) が検証を通過してしまった"
        assert any("サンプル候補での合成に失敗" in e for e in errors)


class TestDispatchRegistration:
    """judgment_classifier が python_block dispatch 表に正しく登録されていることの実証。"""

    def test_slot_ids_resolve_via_store(self) -> None:
        assert python_block_slot_ids(_spec()) == SLOT_IDS

    def test_sample_targets_resolve_via_store(self) -> None:
        assert python_block_sample_targets(_spec()) == SAMPLE_CONTEXT

    def test_sample_targets_is_a_copy_not_shared_reference(self) -> None:
        """呼び手の変異が SAMPLE_CONTEXT 自体を汚染しない (イミュータブル規約)。"""
        result = python_block_sample_targets(_spec())
        result["candidates"] = "変異させてみる"
        assert SAMPLE_CONTEXT["candidates"] != "変異させてみる"


class TestStoreFallback:
    def test_env_flag_zero_falls_back_to_legacy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        spec = _spec()
        invalidate_prompt_cache(spec.prompt_id)
        monkeypatch.setenv(spec.env_flag, "0")
        try:
            assert build_python_block_source(spec, SAMPLE_CONTEXT) is None
            assert j._build_prompt(SAMPLE_CONTEXT) == j._build_prompt_legacy(SAMPLE_CONTEXT)  # noqa: SLF001
        finally:
            invalidate_prompt_cache(spec.prompt_id)

    def test_composer_enabled_by_default_produces_a_result(self) -> None:
        spec = _spec()
        invalidate_prompt_cache(spec.prompt_id)
        try:
            result = build_python_block_source(spec, SAMPLE_CONTEXT)
            assert result is not None
            # seed = legacy byte 一致なので、実際に区別できるのは build が None でないことだけ。
            assert result == j._build_prompt_legacy(SAMPLE_CONTEXT)  # noqa: SLF001
        finally:
            invalidate_prompt_cache(spec.prompt_id)

    def test_build_judgment_prompt_matches_legacy_when_seed_untouched(self) -> None:
        """公開エントリポイント (build_judgment_prompt) が seed=legacy byte 一致の下では
        composer フラグ (既定 ON) の有無で見た目上区別なく動く。"""
        invalidate_prompt_cache("judgment_classifier")
        try:
            prompt = j.build_judgment_prompt(
                title="t",
                category="apt",
                body="本文 (test)。",
                published="2026-08-21",
                candidates=_CANDS,
            )
            expected_context = {
                "candidates": "qilin, apt29",
                "category": "apt",
                "published": "2026-08-21",
                "title": "t",
                "body": "本文 (test)。",
            }
            assert prompt == j._build_prompt_legacy(expected_context)  # noqa: SLF001
        finally:
            invalidate_prompt_cache("judgment_classifier")


class TestUIEditReflection:
    """UI 編集相当 (block 文字列の変更) が合成結果へ実際に反映されることの実証。"""

    def test_edited_block_changes_the_composed_prompt(self) -> None:
        spec = _spec()
        seed = load_seed_rubric(spec)
        assert seed is not None
        edited = seed.model_copy(
            update={
                "sections": [
                    s.model_copy(update={"body": "編集後の intent 判定基準テキスト (test)"})
                    if s.field_id == "intent"
                    else s
                    for s in seed.sections
                ]
            }
        )
        out = compose_python_block(spec, edited, SAMPLE_CONTEXT)
        assert "編集後の intent 判定基準テキスト (test)" in out
        assert "espionage(諜報・情報窃取" not in out
        # code 所有部分 (見出し・候補行・出力形式) は編集の影響を受けない
        assert "候補 (記事に登場した既知アクター): qilin, apt29" in out
        assert '"editorial_stance":"..."' in out
        assert "# 記事" in out


class TestEnvStyleAndEnvironmentGuard:
    def test_env_style_is_none(self) -> None:
        assert _spec().env_style == "none"

    def test_build_prompt_environment_rejects_none_style(self) -> None:
        spec = _spec()
        with pytest.raises(ValueError, match="env_style=none"):
            build_prompt_environment(spec, spec.legacy_path)


class _CapturingLLM(LLMClient):
    """常に固定 JudgmentOut を返し、実際に送信されたプロンプトを記録する fake。"""

    def __init__(self, out: JudgmentOut) -> None:
        self._out = out
        self.prompts: list[str] = []

    @property
    def model(self) -> str:
        return "fake"

    async def generate(
        self,
        prompt: str,
        system: str | None = None,
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        think: bool | None = None,
    ) -> LLMResponse:  # pragma: no cover
        raise NotImplementedError

    async def generate_structured(
        self,
        prompt: str,
        schema: type[_T],
        system: str | None = None,
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        think: bool | None = None,
        max_attempts: int = MAX_STRUCTURED_ATTEMPTS,
    ) -> _T:
        self.prompts.append(prompt)
        return cast(_T, self._out)


class TestEndToEndWiring:
    """本番経路 (classify_judgment) が層分けされた _build_prompt を実際に使うことの実証。"""

    async def test_classify_judgment_uses_the_layered_prompt(self) -> None:
        llm = _CapturingLLM(JudgmentOut(intent="financial"))
        res = await classify_judgment(
            cast(LLMClient, llm),
            title="標的型攻撃の発生",
            category="apt",
            body="kuebiko.example への不正アクセスが観測された (test)。",
            published="2026-08-21",
            candidates=_CANDS,
        )
        assert res is not None
        assert llm.prompts, "LLM が呼ばれていません"
        prompt = llm.prompts[0]
        assert "あなたは CTI アナリスト" in prompt  # intro block 由来
        assert "espionage(諜報・情報窃取" in prompt  # intent block 由来
        assert "候補 (記事に登場した既知アクター): qilin, apt29" in prompt  # code 所有の候補行
        assert "タイトル: 標的型攻撃の発生" in prompt  # code 所有の記事データ部


class TestComposeFailureIsStructurallyGraceful:
    """合成経路がどこで raise しても legacy に落ちる (契約: prompt 組み立ては crash 点にしない)。

    下層 (build_python_block_source) が各所で例外を握る事実に依存せず、
    _build_prompt 自体の関門で保証されることを monkeypatch で固定する。
    """

    def test_composed_path_raising_falls_back_to_legacy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(context: dict[str, str]) -> str | None:
            raise RuntimeError("store unavailable")

        monkeypatch.setattr(j, "_composed_prompt", _boom)

        prompt = j._build_prompt(_CANDS_PRESENT)  # noqa: SLF001

        assert prompt == j._build_prompt_legacy(_CANDS_PRESENT)  # noqa: SLF001

    async def test_classify_judgment_survives_compose_failure_end_to_end(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(context: dict[str, str]) -> str | None:
            raise RuntimeError("store unavailable")

        monkeypatch.setattr(j, "_composed_prompt", _boom)
        llm = _CapturingLLM(JudgmentOut(intent="espionage"))

        res = await classify_judgment(
            cast(LLMClient, llm),
            title="t",
            category="apt",
            body="kuebiko.example への不正アクセスが観測された (test)。",
            published="2026-08-21",
            candidates=_CANDS,
        )

        assert res is not None
        assert res.intent == "espionage"
        assert llm.prompts and "あなたは CTI アナリスト" in llm.prompts[0]
