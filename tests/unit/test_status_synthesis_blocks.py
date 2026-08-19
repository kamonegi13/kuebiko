"""status_synthesis の層分け (2 本目、block 方式) の不変量テスト。

核心は golden: **seed の blocks で合成した結果が legacy .j2 と byte 一致**すること。
summarizer (1 本目) の期待差分契約より強くて単純な不変量 — 逸脱は UI 編集でのみ生まれ、
版履歴に残る。
"""

from __future__ import annotations

from pathlib import Path

from src.prompts.block_composer import compose_blocks, skeleton_slots, validate_blocks
from src.prompts.prompt_store import (
    build_prompt_template,
    invalidate_prompt_cache,
    load_seed_rubric,
)
from src.prompts.registry import get_spec

_maybe_spec = get_spec("status_synthesis")
if _maybe_spec is None or _maybe_spec.skeleton_path is None:
    raise RuntimeError("status_synthesis spec が registry にありません")
_SPEC = _maybe_spec
_SKELETON = _maybe_spec.skeleton_path.read_text(encoding="utf-8")
_LEGACY = _SPEC.legacy_path.read_text(encoding="utf-8")


class TestGolden:
    def test_seed_composition_is_byte_identical_to_legacy(self) -> None:
        """層分けの不変量: seed 合成 == legacy .j2 (byte 一致)。"""
        rubric = load_seed_rubric(_SPEC)
        assert rubric is not None
        assert compose_blocks(_SKELETON, rubric) == _LEGACY

    def test_seed_covers_every_skeleton_slot(self) -> None:
        rubric = load_seed_rubric(_SPEC)
        assert rubric is not None
        assert validate_blocks(_SKELETON, rubric) == []
        assert len(skeleton_slots(_SKELETON)) == 13


class TestValidation:
    def test_missing_block_is_an_error(self) -> None:
        # Arrange: mission block を欠いた rubric
        rubric = load_seed_rubric(_SPEC)
        assert rubric is not None
        broken = rubric.model_copy(
            update={"sections": [s for s in rubric.sections if s.field_id != "mission"]}
        )

        # Act / Assert
        errors = validate_blocks(_SKELETON, broken)
        assert any("mission" in e for e in errors)

    def test_unknown_block_is_an_error_not_silent(self) -> None:
        """skeleton に無い block = 書いても捨てられる write-only — 黙認しない。"""
        rubric = load_seed_rubric(_SPEC)
        assert rubric is not None
        extra = rubric.sections[0].model_copy(update={"field_id": "no_such_slot"})
        broken = rubric.model_copy(update={"sections": [*rubric.sections, extra]})
        errors = validate_blocks(_SKELETON, broken)
        assert any("no_such_slot" in e for e in errors)

    def test_unreplaced_marker_would_render_invisibly(self) -> None:
        """マーカーは Jinja コメント — 万一残っても render 出力に現れない (fail-safe)。"""
        assert "{# BLOCK: mission #}" in _SKELETON


class TestStoreFallback:
    def test_env_flag_zero_falls_back_to_legacy(self, monkeypatch: object, tmp_path: Path) -> None:
        import pytest

        mp = monkeypatch
        assert isinstance(mp, pytest.MonkeyPatch)
        invalidate_prompt_cache(_SPEC.prompt_id)
        mp.setenv(_SPEC.env_flag, "0")
        try:
            assert build_prompt_template(_SPEC, _SPEC.legacy_path) is None
        finally:
            invalidate_prompt_cache(_SPEC.prompt_id)

    def test_template_builds_and_renders_with_minimal_context(self) -> None:
        """seed 由来の合成テンプレートが最小 context で render できる (構文の実証)。"""
        invalidate_prompt_cache(_SPEC.prompt_id)
        template = build_prompt_template(_SPEC, _SPEC.legacy_path)
        assert template is not None
        rendered = template.render(
            period_type="daily",
            period_label="2026-08-20",
            baseline_weeks=4,
            axes_data=[],
            trend_clusters=[],
            high_importance_articles=[],
            nation_correlation=[],
            nation_window_days=7,
            forecast_indicators=[],
            freshness={},
            pir_context=[],
            previous_synthesis=None,
        )
        # 指示散文 (block 由来) が実際に出力へ届いている
        assert "出力 field の完全性チェック" in rendered
        assert "純粋な JSON のみ出力。" in rendered
        invalidate_prompt_cache(_SPEC.prompt_id)
