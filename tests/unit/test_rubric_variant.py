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


class TestExampleFormattingIsPreserved:
    """例の書式を変えない (2026-08-18)。

    ``json.dumps`` で作り直すと「測った変種」と「yaml へ書く形」が空白レベルで
    ずれる。差が出たときにキー除去の効果か整形の効果か切り分けられなくなる。
    """

    def _multiline(self, body: str) -> SummarizerRubric:
        return SummarizerRubric(
            sections=[RubricSection(field_id="title_ja", body="和訳")],
            examples=[RubricExample(label="例", json_text=body)],
        )

    def test_keeps_inline_array_and_indent(self) -> None:
        original = (
            "{\n"
            '  "title_ja": "題",\n'
            '  "iocs": ["1.2.3.4", "evil.example"],\n'
            '  "article_type": "breaking",\n'
            '  "summary": "本文"\n'
            "}"
        )

        got = drop_fields(self._multiline(original), ["article_type"]).examples[0].json_text

        assert '"iocs": ["1.2.3.4", "evil.example"],' in got  # 1 行配列のまま
        assert "article_type" not in got
        assert json.loads(got) == {
            "title_ja": "題",
            "iocs": ["1.2.3.4", "evil.example"],
            "summary": "本文",
        }

    def test_drops_trailing_comma_when_removed_key_was_last(self) -> None:
        """末尾キーを外すと直前行のカンマが余る — JSON として壊れてはいけない。"""
        original = '{\n  "title_ja": "題",\n  "article_type": "breaking"\n}'

        got = drop_fields(self._multiline(original), ["article_type"]).examples[0].json_text

        assert json.loads(got) == {"title_ja": "題"}
        assert '"title_ja": "題"\n' in got + "\n"  # カンマが残っていない

    def test_removes_multiline_object_value(self) -> None:
        original = (
            "{\n"
            '  "title_ja": "題",\n'
            '  "routing_flags": {\n'
            '    "japan_targeted": true,\n'
            '    "confidence": "high"\n'
            "  }\n"
            "}"
        )

        got = drop_fields(self._multiline(original), ["routing_flags"]).examples[0].json_text

        assert json.loads(got) == {"title_ja": "題"}
        assert "japan_targeted" not in got

    def test_single_line_json_falls_back_to_reformat(self) -> None:
        """行操作できない書き方では整形し直す (書式より測定の前提を優先)。"""
        original = '{"title_ja": "題", "article_type": "breaking"}'

        got = drop_fields(self._multiline(original), ["article_type"]).examples[0].json_text

        assert json.loads(got) == {"title_ja": "題"}

    def test_untouched_when_no_target_key(self) -> None:
        original = '{\n  "title_ja": "題"\n}'

        got = drop_fields(self._multiline(original), ["article_type"]).examples[0].json_text

        assert got == original  # 1 文字も変えない
