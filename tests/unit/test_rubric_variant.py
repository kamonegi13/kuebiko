"""rubric 変種 (評価用、DB 非保存) のテスト。"""

from __future__ import annotations

import json

from src.eval.rubric_variant import drop_fields
from src.prompts.rubric_model import RubricExample, RubricSection, SummarizerRubric


def _rubric() -> SummarizerRubric:
    return SummarizerRubric(
        intro="イントロ",
        sections=[
            RubricSection(field_id="title_ja", body="和訳タイトル"),
            RubricSection(field_id="article_type", body="記事タイプ"),
            RubricSection(field_id="summary", body="要約"),
        ],
        examples=[
            RubricExample(
                label="例1",
                json_text=json.dumps(
                    {"title_ja": "題", "article_type": "breaking", "summary": "本文"},
                    ensure_ascii=False,
                ),
            )
        ],
    )


class TestDropFields:
    def test_removes_section(self) -> None:
        got = drop_fields(_rubric(), ["article_type"])
        assert [s.field_id for s in got.sections] == ["title_ja", "summary"]

    def test_removes_key_from_examples_too(self) -> None:
        """基準から外したのに例で示し続けると、何を測ったか分からなくなる。"""
        got = drop_fields(_rubric(), ["article_type"])
        assert "article_type" not in got.examples[0].json_text
        assert "title_ja" in got.examples[0].json_text

    def test_original_is_not_mutated(self) -> None:
        """評価中に本番の rubric を汚さない。"""
        original = _rubric()
        drop_fields(original, ["article_type"])
        assert [s.field_id for s in original.sections] == [
            "title_ja",
            "article_type",
            "summary",
        ]
        assert "article_type" in original.examples[0].json_text

    def test_empty_drop_returns_same_rubric(self) -> None:
        original = _rubric()
        assert drop_fields(original, []) is original

    def test_unknown_field_is_a_noop(self) -> None:
        got = drop_fields(_rubric(), ["nonexistent"])
        assert len(got.sections) == 3

    def test_broken_example_json_is_left_untouched(self) -> None:
        """例の JSON が壊れていても評価の前提を静かに変えない。"""
        r = _rubric().model_copy(
            update={"examples": [RubricExample(label="壊れ", json_text="{not json")]}
        )
        got = drop_fields(r, ["article_type"])
        assert got.examples[0].json_text == "{not json"
