"""判定基準 → プロンプト合成の unit test (WP1)。

合成結果が **legacy .j2 と同じ構造** (persona include / 記事変数 / few-shot フェンス) を
保ち、jinja2 Environment が ``dispatch._load_template`` と完全に同一構成であることを固定する
(``_persona_local.j2`` の解決・autoescape 無効・StrictUndefined は既存挙動)。
"""

from __future__ import annotations

from typing import cast

import jinja2
import pytest

from src.pipeline import dispatch
from src.prompts.rubric_model import RubricExample, RubricSection, SectionKind, SummarizerRubric
from src.prompts.rubric_store import invalidate_summarizer_cache
from src.prompts.sample_article import SAMPLE_ARTICLE, SAMPLE_BODY
from src.prompts.summarizer_composer import (
    ARTICLE_BLOCK,
    LEGACY_TEMPLATE_PATH,
    PERSONA_INCLUDE,
    RUBRIC_HEADING,
    build_environment,
    build_template,
    compose,
    loader_roots,
)


def _section(
    field_id: str,
    *,
    kind: SectionKind = "rubric",
    body: str = "判定基準の本文。",
) -> RubricSection:
    return RubricSection(field_id=field_id, kind=kind, title="", body=body, note="")


@pytest.fixture(autouse=True)
def _clean_cache() -> None:
    invalidate_summarizer_cache()


@pytest.fixture
def rubric() -> SummarizerRubric:
    return SummarizerRubric(
        intro="導入文 1 行目\n導入文 2 行目",
        sections=[
            _section("title_ja", body="和訳の基準。"),
            _section("summary", body="要約の基準。"),
            _section("diamond", kind="suppressed", body=""),
            _section("remediation", body="対処の基準。"),
        ],
        examples=[
            RubricExample(label="参考", json_text='{\n  "title_ja": "例"\n}'),
            RubricExample(label="", json_text='{"title_ja": "無ラベル例"}'),
        ],
    )


def test_starts_with_persona_include(rubric: SummarizerRubric) -> None:
    composed = compose(rubric)

    assert composed.split("\n")[0] == PERSONA_INCLUDE


def test_contains_article_variable_block(rubric: SummarizerRubric) -> None:
    composed = compose(rubric)

    assert ARTICLE_BLOCK in composed
    for var in ("{{ article.title }}", "{{ article.feed_title }}", "{{ body }}"):
        assert var in composed


def test_suppressed_section_with_empty_body_is_not_emitted(rubric: SummarizerRubric) -> None:
    composed = compose(rubric)

    assert "``diamond``" not in composed


def test_sections_keep_config_order(rubric: SummarizerRubric) -> None:
    composed = compose(rubric)

    assert composed.index("``title_ja``") < composed.index("``summary``")
    assert composed.index("``summary``") < composed.index("``remediation``")
    assert RUBRIC_HEADING in composed


def test_examples_are_fenced_with_label_heading(rubric: SummarizerRubric) -> None:
    composed = compose(rubric)

    assert "# 出力例 (参考)" in composed
    assert "# 出力例\n" in composed  # label 空なら括弧を出さない
    assert composed.count("```json") == 2
    assert composed.count("```") == 4


def test_composed_text_is_valid_jinja_source(rubric: SummarizerRubric) -> None:
    # 構文エラーなら from_string がここで例外を投げる
    template = build_template(rubric, path=LEGACY_TEMPLATE_PATH)

    assert isinstance(template, jinja2.Template)


def test_renders_with_sample_article(rubric: SummarizerRubric) -> None:
    template = build_template(rubric, path=LEGACY_TEMPLATE_PATH)

    rendered = template.render(article=SAMPLE_ARTICLE, body=SAMPLE_BODY)

    assert SAMPLE_ARTICLE.title in rendered
    assert SAMPLE_ARTICLE.feed_title in rendered
    assert "SAMPLE-ACTOR" in rendered
    assert "{{" not in rendered  # StrictUndefined 下で全変数が解決されている


def test_autoescape_is_disabled(rubric: SummarizerRubric) -> None:
    raw = SummarizerRubric(
        sections=[_section("summary", body="A & B < C > D の判定。")],
    )

    rendered = build_template(raw, path=LEGACY_TEMPLATE_PATH).render(
        article=SAMPLE_ARTICLE,
        body=SAMPLE_BODY,
    )

    assert "A & B < C > D" in rendered
    assert "&amp;" not in rendered
    assert "&lt;" not in rendered


def test_exactly_one_trailing_newline(rubric: SummarizerRubric) -> None:
    composed = compose(rubric)

    assert composed.endswith("\n")
    assert not composed.endswith("\n\n")


def test_environment_matches_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    # composer を無効化して legacy 経路の Environment を取り出し、構成を突き合わせる
    monkeypatch.setenv("SUMMARIZER_COMPOSER", "0")
    legacy = dispatch._load_template(LEGACY_TEMPLATE_PATH)
    env = build_environment(LEGACY_TEMPLATE_PATH)

    legacy_loader = cast(jinja2.FileSystemLoader, legacy.environment.loader)
    loader = cast(jinja2.FileSystemLoader, env.loader)
    assert list(loader.searchpath) == list(legacy_loader.searchpath)
    assert loader_roots(LEGACY_TEMPLATE_PATH) == list(legacy_loader.searchpath)
    assert env.autoescape == legacy.environment.autoescape
    assert env.keep_trailing_newline is legacy.environment.keep_trailing_newline
    assert env.undefined is legacy.environment.undefined
