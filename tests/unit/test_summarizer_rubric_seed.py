"""同梱 seed (config/prompts/summarizer_rubric.yaml) の不変量 (WP1)。

seed は「legacy .j2 と等価な既定値」であり、**24 フィールドの完全被覆**がこの層の
最重要不変量 (宣言の無いフィールドは UI から見えず、判定基準を編集する術が消える)。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.pipeline.summary import SummaryOutput
from src.prompts.rubric_model import SummarizerRubric
from src.prompts.rubric_store import load_seed_rubric
from src.prompts.sample_article import SAMPLE_ARTICLE, SAMPLE_BODY
from src.prompts.summarizer_composer import LEGACY_TEMPLATE_PATH, build_template, compose

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SEED_PATH = _REPO_ROOT / "config" / "prompts" / "summarizer_rubric.yaml"
_LEGACY_PATH = _REPO_ROOT / LEGACY_TEMPLATE_PATH


@pytest.fixture(scope="module")
def seed() -> SummarizerRubric:
    rubric = load_seed_rubric(_SEED_PATH)
    assert rubric is not None, "seed yaml が SummarizerRubric として parse できない"
    return rubric


def test_seed_parses_as_rubric(seed: SummarizerRubric) -> None:
    assert seed.schema_version == 1
    assert seed.intro.strip() != ""
    assert len(seed.examples) == 3


def test_seed_covers_every_summary_output_field(seed: SummarizerRubric) -> None:
    declared = {s.field_id for s in seed.sections}

    assert declared == set(SummaryOutput.model_fields)
    assert len(declared) == 24


def test_seed_splits_rubric_and_suppressed(seed: SummarizerRubric) -> None:
    kinds = [s.kind for s in seed.sections]

    assert kinds.count("rubric") == 18
    assert kinds.count("suppressed") == 6
    suppressed = {s.field_id for s in seed.sections if s.kind == "suppressed"}
    assert suppressed == {
        "editorial_stance",
        "bluf",
        "diamond",
        "event_date",
        "event_date_basis",
        "compromise_date",
    }
    # editorial_stance だけは legacy と同じ位置 (routing_flags と pmesii_axes の間) に残す
    order = [s.field_id for s in seed.sections]
    assert order.index("routing_flags") + 1 == order.index("editorial_stance")
    assert order.index("editorial_stance") + 1 == order.index("pmesii_axes")


def test_seed_renders_with_sample_article(seed: SummarizerRubric) -> None:
    template = build_template(seed, path=_LEGACY_PATH)

    rendered = template.render(article=SAMPLE_ARTICLE, body=SAMPLE_BODY)

    assert SAMPLE_ARTICLE.title in rendered
    assert "# フィールド別の判定基準" in rendered
    assert "{{" not in rendered


def test_composed_is_not_longer_than_legacy(seed: SummarizerRubric) -> None:
    composed = compose(seed)
    legacy = _LEGACY_PATH.read_text(encoding="utf-8")

    # 契約散文を削っただけなので合成側が長くなることはない (長くなる = 何か足している)
    assert len(composed) <= len(legacy)


def test_no_body_has_trailing_newline(seed: SummarizerRubric) -> None:
    # 合成側がセクション間に \n を入れるため、body の末尾改行 (yaml の ``|`` chomp ミス) は
    # 余分な空行になって legacy との等価性を壊す
    offenders = [s.field_id for s in seed.sections if s.body != s.body.rstrip("\n")]

    assert offenders == []
