"""summarizer.j2 (legacy) と rubric 合成結果 (seed) の等価性テスト (WP4)。

WP1 の簡易チェック (「置換 19 / 削除 2 / 空白 2 / 未分類 0」「legacy 18,473 → 合成
18,008 字」) を恒久的な機械検証として固定する。差分は
``scripts/summarizer_diff_contract.py`` の期待差分表と完全一致することを要求し、
そこに載っていない差分 (= 未分類) が 1 件でもあれば fail する。

CLAUDE.md §10 の観察期間後、legacy .j2 本体を撤去する際にこのテストごと削除する
(scripts/summarizer_diff_contract.py の docstring に撤去条件を記載)。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from summarizer_diff_contract import (  # noqa: E402
    EXPECTED_DELETIONS,
    EXPECTED_REWRITES,
    LEGACY_PATH,
    MAX_WHITESPACE_DIFFS,
)
from verify_summarizer_rubric import (  # noqa: E402
    _build_deletion_texts,
    _build_rewrite_pairs,
    classify_diff,
)

from src.prompts.rubric_model import SummarizerRubric  # noqa: E402
from src.prompts.rubric_store import load_seed_rubric  # noqa: E402
from src.prompts.summarizer_composer import compose  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SEED_PATH = _REPO_ROOT / "config" / "prompts" / "summarizer_rubric.yaml"
# LEGACY_PATH は mypy override (ignore_missing_imports) 対象の scripts/ 由来で Any に
# なるため、Path を明示して Any 伝播 (no-any-return) を断つ。
_LEGACY_PATH: Path = _REPO_ROOT / str(LEGACY_PATH)
_GOLDEN_PATH = _REPO_ROOT / "tests" / "fixtures" / "summarizer_composed_expected.txt"


@pytest.fixture(scope="module")
def seed_rubric() -> SummarizerRubric:
    rubric = load_seed_rubric(_SEED_PATH)
    assert rubric is not None, "seed yaml が SummarizerRubric として parse できない"
    return rubric


@pytest.fixture(scope="module")
def legacy_text() -> str:
    return _LEGACY_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def composed_text(seed_rubric: SummarizerRubric) -> str:
    return compose(seed_rubric)


@pytest.fixture(scope="module")
def diff_counts(legacy_text: str, composed_text: str) -> Any:
    deletion_texts = _build_deletion_texts()
    rewrite_pairs = _build_rewrite_pairs()
    return classify_diff(
        legacy_text,
        composed_text,
        deletion_texts=deletion_texts,
        rewrite_pairs=rewrite_pairs,
        include_content=True,
    )


class TestLegacyEquivalence:
    """seed 合成 vs legacy の差分が §7-1 の契約表と完全一致することを固定する。"""

    def test_diff_matches_contract_exactly(self, diff_counts: Any) -> None:
        assert diff_counts.unclassified == 0, diff_counts.unclassified_notes
        assert diff_counts.deletions == len(EXPECTED_DELETIONS)
        assert diff_counts.rewrites == len(EXPECTED_REWRITES)
        assert diff_counts.whitespace <= MAX_WHITESPACE_DIFFS

    def test_composed_is_not_longer_than_legacy(self, legacy_text: str, composed_text: str) -> None:
        # 契約散文を削っただけなので合成側が長くなることはない (長くなる = 何か足している)
        assert len(composed_text) <= len(legacy_text)

    def test_golden_fixture_matches_seed_composition_byte_for_byte(
        self, composed_text: str
    ) -> None:
        assert _GOLDEN_PATH.exists(), (
            "golden fixture が無い。"
            "uv run python scripts/verify_summarizer_rubric.py "
            "--source seed --offline --write-golden で生成すること"
        )
        golden = _GOLDEN_PATH.read_text(encoding="utf-8")
        assert composed_text == golden
