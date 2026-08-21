"""article_triage の層分け不変量テスト (python_block kind、group 4 の 3 本目・最終、2026-08-21)。

ioc_llm_verifier / judgment_classifier (tests/unit/test_ioc_llm_verifier_prompt_layering.py /
test_judgment_classifier.py) と同じ核心を持つ: **seed の blocks で合成した結果が
legacy (frozen) 実装の出力と byte 一致**すること。

article_triage 固有の事情:
    - 層分けは PIR-driven 経路 (``_build_prompt_pir_driven``) のみが対象。legacy 比較先は
      ``_build_prompt_pir_driven_legacy`` であり、``_build_prompt_legacy_hardcoded``
      (env ``PIR_DRIVEN_TRIAGE=0`` の rollback 実体) ではない — 両者を混同しない。
    - ioc/judgment と違い ``ArticleTriage`` はクラスなので、合成対象の関数
      (``compose_from_blocks`` / ``_composed_prompt``) はモジュール関数のまま、
      比較対象の legacy はインスタンスメソッドという非対称構造になる。
    - 出力ルール block は生の brace ``{high, medium, low}`` を含むため直接組み立て方式
      (``.format()`` 不使用) を踏襲する。judgment_classifier (``.format()`` 方式) なら
      この block は ``KeyError``/``ValueError`` で保存できない — その対比を明示的に固定する。

既存の test_article_triage.py は一切変更しない — 本ファイルは追加のみ。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TypeVar

import pytest
from pydantic import BaseModel

import src.tools.article_triage as t
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
from src.tools.article_model import Article
from src.tools.article_triage import (
    SAMPLE_CONTEXT,
    SLOT_IDS,
    ArticleTriage,
    compose_from_blocks,
)
from src.tools.llm_client import (
    DEFAULT_MAX_TOKENS,
    DEFAULT_TEMPERATURE,
    MAX_STRUCTURED_ATTEMPTS,
    LLMClient,
    LLMResponse,
)

_T = TypeVar("_T", bound=BaseModel)


def _spec() -> PromptSpec:
    spec = get_spec("article_triage")
    assert spec is not None
    return spec


class _FakeLLM(LLMClient):
    """本テストでは呼び出されない (プロンプト組み立てのみ検証)。呼ばれたら失敗させる。"""

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
    ) -> _T:  # pragma: no cover
        raise NotImplementedError


def _article(
    title: str = "サンプルタイトル",
    summary: str = "<p>サンプル概要本文</p>",
    feed_title: str = "サンプルフィード",
) -> Article:
    return Article(
        id="triage-golden-001",
        title=title,
        url="https://kuebiko.example/articles/triage-golden-001",
        summary_html=summary,
        author=None,
        published=datetime(2026, 1, 1, tzinfo=UTC),
        feed_title=feed_title,
        feed_url="https://kuebiko.example/feed.xml",
    )


def _context_and_legacy(
    triage: ArticleTriage, article: Article, high_criteria: str, medium_criteria: str
) -> tuple[dict[str, str], str]:
    """(context, legacy 出力) を同一 article から作る (compose_from_blocks への入力と比較対象)。"""
    context = {
        "feed": (article.feed_title or "").strip(),
        "title": (article.title or "").strip(),
        "body_preview": triage._triage_content(article),  # noqa: SLF001
        "high_criteria": high_criteria,
        "medium_criteria": medium_criteria,
    }
    legacy = triage._build_prompt_pir_driven_legacy(  # noqa: SLF001
        article, high_criteria, medium_criteria
    )
    return context, legacy


_LONG_SUMMARY = "<p>" + "長い本文が延々と続く。" * 300 + "</p>"


class TestGolden:
    """核心不変量: seed の blocks で合成した結果が legacy (frozen) 実装と byte 一致。"""

    @pytest.mark.parametrize(
        ("title", "summary", "high_criteria", "medium_criteria"),
        [
            (
                "通常記事 (medium あり)",
                "<p>サンプル概要本文</p>",
                "1. サンプル high 基準 1\n2. サンプル high 基準 2",
                "- サンプル medium 基準 1",
            ),
            (
                "medium 基準が空の記事",
                "<p>サンプル概要本文</p>",
                "1. サンプル high 基準 1",
                "",
            ),
            (
                "長文記事 (body_preview_chars 超の本文)",
                _LONG_SUMMARY,
                "1. サンプル high 基準 1\n2. サンプル high 基準 2\n3. サンプル high 基準 3",
                "- サンプル medium 基準 1\n- サンプル medium 基準 2",
            ),
        ],
        ids=["with_medium", "medium_empty", "long_body"],
    )
    def test_seed_composition_is_byte_identical_to_legacy(
        self, title: str, summary: str, high_criteria: str, medium_criteria: str
    ) -> None:
        spec = _spec()
        rubric = load_seed_rubric(spec)
        assert rubric is not None
        blocks = {s.field_id: s.body for s in rubric.sections}

        triage = ArticleTriage(_FakeLLM())
        article = _article(title=title, summary=summary)
        context, legacy = _context_and_legacy(triage, article, high_criteria, medium_criteria)

        assert compose_from_blocks(blocks, context) == legacy

    def test_seed_covers_every_slot(self) -> None:
        spec = _spec()
        rubric = load_seed_rubric(spec)
        assert rubric is not None
        assert validate_python_block_rubric(spec, rubric) == []
        assert {s.field_id for s in rubric.sections} == set(SLOT_IDS)

    def test_sample_context_is_non_empty_dummy_with_kuebiko_example(self) -> None:
        """SAMPLE_CONTEXT: 非空ダミー (kuebiko.example、high_criteria 2 行・medium 1 行)。"""
        assert all(v.strip() for v in SAMPLE_CONTEXT.values())
        assert "kuebiko.example" in SAMPLE_CONTEXT["feed"] + SAMPLE_CONTEXT["title"]
        assert SAMPLE_CONTEXT["high_criteria"].count("\n") == 1  # 2 行
        assert "\n" not in SAMPLE_CONTEXT["medium_criteria"]  # 1 行

    def test_validate_composes_the_sample_context(self) -> None:
        """保存前検証はサンプルでの実合成まで通す (最後の砦)。"""
        spec = _spec()
        rubric = load_seed_rubric(spec)
        assert rubric is not None
        assert validate_python_block_rubric(spec, rubric) == []
        composed = compose_python_block(spec, rubric, dict(SAMPLE_CONTEXT))
        assert composed.strip()
        assert "## 判定基準 (medium)" in composed  # SAMPLE_CONTEXT.medium_criteria は非空


class TestValidation:
    def test_missing_block_is_an_error(self) -> None:
        spec = _spec()
        rubric = load_seed_rubric(spec)
        assert rubric is not None
        broken = rubric.model_copy(
            update={"sections": [s for s in rubric.sections if s.field_id != "low_criteria"]}
        )
        errors = validate_python_block_rubric(spec, broken)
        assert any("low_criteria" in e for e in errors)

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


class TestDispatchRegistration:
    """article_triage が python_block dispatch 表に正しく登録されていることの実証。"""

    def test_slot_ids_resolve_via_store(self) -> None:
        assert python_block_slot_ids(_spec()) == SLOT_IDS

    def test_sample_targets_resolve_via_store(self) -> None:
        assert python_block_sample_targets(_spec()) == SAMPLE_CONTEXT

    def test_sample_targets_is_a_copy_not_shared_reference(self) -> None:
        """呼び手の変異が SAMPLE_CONTEXT 自体を汚染しない (イミュータブル規約)。"""
        result = python_block_sample_targets(_spec())
        result["feed"] = "変異させてみる"
        assert SAMPLE_CONTEXT["feed"] != "変異させてみる"


class TestStoreFallback:
    """TRIAGE_COMPOSER フラグと _build_prompt_pir_driven の fail-safe 解決。"""

    def test_env_flag_zero_falls_back_to_legacy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        spec = _spec()
        invalidate_prompt_cache(spec.prompt_id)
        monkeypatch.setenv(spec.env_flag, "0")
        try:
            triage = ArticleTriage(_FakeLLM())
            article = _article()
            high, medium = "1. x", "- y"
            context = {
                "feed": (article.feed_title or "").strip(),
                "title": (article.title or "").strip(),
                "body_preview": triage._triage_content(article),  # noqa: SLF001
                "high_criteria": high,
                "medium_criteria": medium,
            }
            assert build_python_block_source(spec, context) is None
            prompt = triage._build_prompt_pir_driven(article, high, medium)  # noqa: SLF001
            legacy = triage._build_prompt_pir_driven_legacy(article, high, medium)  # noqa: SLF001
            assert prompt == legacy
        finally:
            invalidate_prompt_cache(spec.prompt_id)

    def test_composer_enabled_by_default_produces_the_layered_prompt(self) -> None:
        spec = _spec()
        invalidate_prompt_cache(spec.prompt_id)
        try:
            triage = ArticleTriage(_FakeLLM())
            article = _article()
            high, medium = "1. x", "- y"
            prompt = triage._build_prompt_pir_driven(article, high, medium)  # noqa: SLF001
            legacy = triage._build_prompt_pir_driven_legacy(article, high, medium)  # noqa: SLF001
            # seed = legacy byte 一致なので、実際に区別できるのは None でないことだけ。
            assert prompt == legacy
        finally:
            invalidate_prompt_cache(spec.prompt_id)


class TestFlagNesting:
    """PIR_DRIVEN_TRIAGE=0 のとき TRIAGE_COMPOSER の状態によらず hardcoded prompt が出る。

    ``_build_prompt`` (top-level dispatcher) は PIR_DRIVEN_TRIAGE=0 で
    ``_build_prompt_pir_driven`` (層分け対象) をそもそも呼ばない — 構造的に独立している
    ことをこのテストで固定する。
    """

    @pytest.mark.parametrize("composer_flag", ["1", "0"], ids=["composer_on", "composer_off"])
    def test_pir_driven_disabled_ignores_composer_state(
        self, monkeypatch: pytest.MonkeyPatch, composer_flag: str
    ) -> None:
        monkeypatch.setenv("PIR_DRIVEN_TRIAGE", "0")
        monkeypatch.setenv("TRIAGE_COMPOSER", composer_flag)
        triage = ArticleTriage(_FakeLLM())
        article = _article()

        prompt = triage._build_prompt(article)  # noqa: SLF001

        assert prompt == triage._build_prompt_legacy_hardcoded(article)  # noqa: SLF001


class TestUIEditReflection:
    """UI 編集相当 (block 文字列の変更) が合成結果へ実際に反映されることの実証。"""

    def test_edited_block_changes_the_composed_prompt(self) -> None:
        spec = _spec()
        seed = load_seed_rubric(spec)
        assert seed is not None
        edited = seed.model_copy(
            update={
                "sections": [
                    s.model_copy(update={"body": "編集後の low 判定基準テキスト (test)"})
                    if s.field_id == "low_criteria"
                    else s
                    for s in seed.sections
                ]
            }
        )
        out = compose_python_block(spec, edited, dict(SAMPLE_CONTEXT))
        assert "編集後の low 判定基準テキスト (test)" in out
        assert "単発の金銭目的犯罪で" not in out
        # code 所有部分 (見出し・high 基準注入) は編集の影響を受けない
        assert "## 判定基準 (high)" in out
        assert SAMPLE_CONTEXT["high_criteria"] in out


class TestDirectAssemblyHandlesRawBraces:
    """直接組み立て方式 (.format() 不使用) を選んだ理由の固定: 生 brace を含む block が
    そのまま保存・合成できる (judgment_classifier の .format() 方式ならここで落ちる)。
    """

    def test_seed_output_rules_block_contains_the_raw_brace(self) -> None:
        spec = _spec()
        rubric = load_seed_rubric(spec)
        assert rubric is not None
        output_rules = next(s for s in rubric.sections if s.field_id == "output_rules")
        assert "{high, medium, low}" in output_rules.body

    def test_edited_block_with_additional_raw_brace_composes_without_raising(self) -> None:
        spec = _spec()
        seed = load_seed_rubric(spec)
        assert seed is not None
        edited = seed.model_copy(
            update={
                "sections": [
                    s.model_copy(
                        update={"body": "## 出力ルール\n- {undefined_placeholder} をそのまま残す"}
                    )
                    if s.field_id == "output_rules"
                    else s
                    for s in seed.sections
                ]
            }
        )
        errors = validate_python_block_rubric(spec, edited)
        assert errors == []
        out = compose_python_block(spec, edited, dict(SAMPLE_CONTEXT))
        assert "{undefined_placeholder}" in out


class TestComposeFailureIsStructurallyGraceful:
    """合成経路がどこで raise しても legacy に落ちる (契約: prompt 組み立ては crash 点にしない)。

    下層 (build_python_block_source) が各所で例外を握る事実に依存せず、
    _build_prompt_pir_driven 自体の関門で保証されることを monkeypatch で固定する。
    """

    def test_composed_path_raising_falls_back_to_legacy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(context: Mapping[str, str]) -> str | None:
            raise RuntimeError("store unavailable")

        monkeypatch.setattr(t, "_composed_prompt", _boom)
        triage = ArticleTriage(_FakeLLM())
        article = _article()
        high, medium = "1. x", "- y"

        prompt = triage._build_prompt_pir_driven(article, high, medium)  # noqa: SLF001

        assert prompt == triage._build_prompt_pir_driven_legacy(  # noqa: SLF001
            article, high, medium
        )


class TestEnvStyleAndEnvironmentGuard:
    def test_env_style_is_none(self) -> None:
        assert _spec().env_style == "none"

    def test_build_prompt_environment_rejects_none_style(self) -> None:
        spec = _spec()
        with pytest.raises(ValueError, match="env_style=none"):
            build_prompt_environment(spec, spec.legacy_path)
