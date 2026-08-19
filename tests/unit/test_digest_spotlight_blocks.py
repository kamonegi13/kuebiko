"""digest/spotlight 群 4 本 (weekly_recap / pir_daily_focus / deep_dive_rubric /
pir_spotlight) の層分け不変量テスト。

status_synthesis (2 本目、tests/unit/test_status_synthesis_blocks.py) と同型。核心は
golden: **seed の blocks で合成した結果が legacy .j2 と byte 一致**すること。4 本分を
parametrize でまとめる (1 ファイルで可、というタスク指示に従う)。
"""

from __future__ import annotations

import pytest

from src.prompts.block_composer import compose_blocks, skeleton_slots, validate_blocks
from src.prompts.prompt_store import (
    build_prompt_template,
    invalidate_prompt_cache,
    load_seed_rubric,
)
from src.prompts.registry import PromptSpec, get_spec
from src.prompts.sample_context import sample_context_for

# prompt_id → skeleton の slot 数 (extract_prompt_blocks.py --apply 実行時の cut-plan と一致)
_EXPECTED_SLOT_COUNT: dict[str, int] = {
    "weekly_recap": 7,
    "pir_daily_focus": 6,
    "deep_dive_rubric": 7,
    "pir_spotlight": 7,
}

# 各プロンプトの render 結果に必ず含まれるはずの block 由来テキスト (実証用の目印)。
# block 本文からの抜粋 — legacy .j2 由来テキストと一致していれば synthesize が効いている証拠。
_EXPECTED_RENDERED_MARKER: dict[str, str] = {
    "weekly_recap": "それでは Weekly Recap を出力してください。",
    "pir_daily_focus": "出力は **要点本文のみ**",
    "deep_dive_rubric": "純粋な JSON のみ出力。",
    "pir_spotlight": "出力は **JSON のみ**",
}


def _spec(prompt_id: str) -> PromptSpec:
    spec = get_spec(prompt_id)
    if spec is None or spec.skeleton_path is None:
        raise RuntimeError(f"{prompt_id} spec が registry にありません")
    return spec


_PROMPT_IDS: tuple[str, ...] = tuple(_EXPECTED_SLOT_COUNT)


class TestGolden:
    @pytest.mark.parametrize("prompt_id", _PROMPT_IDS)
    def test_seed_composition_is_byte_identical_to_legacy(self, prompt_id: str) -> None:
        """層分けの不変量: seed 合成 == legacy .j2 (byte 一致)。"""
        spec = _spec(prompt_id)
        skeleton = spec.skeleton_path.read_text(encoding="utf-8")  # type: ignore[union-attr]
        legacy = spec.legacy_path.read_text(encoding="utf-8")
        rubric = load_seed_rubric(spec)
        assert rubric is not None
        assert compose_blocks(skeleton, rubric) == legacy

    @pytest.mark.parametrize("prompt_id", _PROMPT_IDS)
    def test_seed_covers_every_skeleton_slot(self, prompt_id: str) -> None:
        spec = _spec(prompt_id)
        skeleton = spec.skeleton_path.read_text(encoding="utf-8")  # type: ignore[union-attr]
        rubric = load_seed_rubric(spec)
        assert rubric is not None
        assert validate_blocks(skeleton, rubric) == []
        assert len(skeleton_slots(skeleton)) == _EXPECTED_SLOT_COUNT[prompt_id]


class TestValidation:
    @pytest.mark.parametrize("prompt_id", _PROMPT_IDS)
    def test_missing_block_is_an_error(self, prompt_id: str) -> None:
        # Arrange: mission block (全 4 本に共通して存在) を欠いた rubric
        spec = _spec(prompt_id)
        skeleton = spec.skeleton_path.read_text(encoding="utf-8")  # type: ignore[union-attr]
        rubric = load_seed_rubric(spec)
        assert rubric is not None
        broken = rubric.model_copy(
            update={"sections": [s for s in rubric.sections if s.field_id != "mission"]}
        )

        # Act / Assert
        errors = validate_blocks(skeleton, broken)
        assert any("mission" in e for e in errors)

    @pytest.mark.parametrize("prompt_id", _PROMPT_IDS)
    def test_unknown_block_is_an_error_not_silent(self, prompt_id: str) -> None:
        """skeleton に無い block = 書いても捨てられる write-only — 黙認しない。"""
        spec = _spec(prompt_id)
        skeleton = spec.skeleton_path.read_text(encoding="utf-8")  # type: ignore[union-attr]
        rubric = load_seed_rubric(spec)
        assert rubric is not None
        extra = rubric.sections[0].model_copy(update={"field_id": "no_such_slot"})
        broken = rubric.model_copy(update={"sections": [*rubric.sections, extra]})
        errors = validate_blocks(skeleton, broken)
        assert any("no_such_slot" in e for e in errors)

    @pytest.mark.parametrize("prompt_id", _PROMPT_IDS)
    def test_unreplaced_marker_would_render_invisibly(self, prompt_id: str) -> None:
        """マーカーは Jinja コメント — 万一残っても render 出力に現れない (fail-safe)。"""
        spec = _spec(prompt_id)
        skeleton = spec.skeleton_path.read_text(encoding="utf-8")  # type: ignore[union-attr]
        assert "{# BLOCK: mission #}" in skeleton


class TestStoreFallback:
    @pytest.mark.parametrize("prompt_id", _PROMPT_IDS)
    def test_env_flag_zero_falls_back_to_legacy(
        self, prompt_id: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spec = _spec(prompt_id)
        invalidate_prompt_cache(spec.prompt_id)
        monkeypatch.setenv(spec.env_flag, "0")
        try:
            assert build_prompt_template(spec, spec.legacy_path) is None
        finally:
            invalidate_prompt_cache(spec.prompt_id)

    @pytest.mark.parametrize("prompt_id", _PROMPT_IDS)
    def test_template_builds_and_renders_with_sample_context(self, prompt_id: str) -> None:
        """seed 由来の合成テンプレートがサンプル context で render できる (構文の実証)。"""
        spec = _spec(prompt_id)
        invalidate_prompt_cache(spec.prompt_id)
        template = build_prompt_template(spec, spec.legacy_path)
        assert template is not None
        context = sample_context_for(prompt_id)
        assert context is not None, f"sample_context.py に {prompt_id} が未定義です"
        rendered = template.render(**context)
        # 指示散文 (block 由来) が実際に出力へ届いている
        assert _EXPECTED_RENDERED_MARKER[prompt_id] in rendered
        invalidate_prompt_cache(spec.prompt_id)


class TestEnvStyleMatchesConsumer:
    """registry の env_style が消費側の実際の Jinja Environment 構成と一致することを固定する。

    差異があると (例: StrictUndefined の有無) 合成テンプレートの挙動が legacy と
    乖離するため、消費側の Environment 定義箇所を直接読んで確認したうえで固定する。
    """

    @pytest.mark.parametrize("prompt_id", _PROMPT_IDS)
    def test_env_style_is_synthesis(self, prompt_id: str) -> None:
        # 4 本の消費側 (src/digest/llm_digest.py, src/digest/pir_daily_focus.py,
        # src/digest/deep_dive_selector.py, src/spotlight/generator.py) はいずれも
        # jinja2.Environment(loader=FileSystemLoader("prompts"), autoescape=False,
        # keep_trailing_newline=True) — StrictUndefined 無し。既存の "synthesis" style と
        # 完全一致するため新規 style は追加しない。
        assert _spec(prompt_id).env_style == "synthesis"
