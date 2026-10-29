"""scripts/pair_same_input_control.py: 入力が揃わない組の除外・直列実行・一致率・群 (mock)。"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import pair_same_input_control as psic  # noqa: E402

TEXTS = {
    "a": psic.ArticleText("ta", "sa", "fa"),
    "b": psic.ArticleText("tb", "sb", "fb"),
    "c": psic.ArticleText("tc", "sc", "fc"),
}


def test_fetch_texts_drops_articles_without_summary() -> None:
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE articles(article_id TEXT, title TEXT, summary TEXT, feed_title TEXT)")
    c.execute(
        "INSERT INTO articles VALUES ('a','ta','sa','fa'),('b','tb','','fb'),('c','tc',NULL,'fc')"
    )
    assert set(psic.fetch_texts(c, ["a", "b", "c", "zz"])) == {"a"}


def test_split_usable_counts_excluded() -> None:
    pairs = [psic.PairInput("a", "b"), psic.PairInput("a", "x")]
    usable, excluded = psic.split_usable(pairs, TEXTS)
    assert usable == [pairs[0]] and excluded == 1


def test_run_models_is_strictly_serial() -> None:
    events: list[str] = []

    def make(model: str) -> psic.Judge:
        async def judge(prompt: str) -> bool | None:
            events.append(f"start:{model}")
            await asyncio.sleep(0)
            events.append(f"end:{model}")
            return True

        return judge

    pairs = [psic.PairInput("a", "b"), psic.PairInput("b", "c")]
    out = asyncio.run(psic.run_models(["m1", "m2"], pairs, TEXTS, make))
    assert out == {"m1": [True, True], "m2": [True, True]}
    # m1 の全件が終わってから m2 が始まる / 1 件ずつ start-end が交互
    assert events == ["start:m1", "end:m1"] * 2 + ["start:m2", "end:m2"] * 2


def test_agreement_ignores_failures() -> None:
    assert psic.agreement([True, False, None], [True, True, True]) == (2, 0.5)
    assert psic.agreement([None], [True]) == (0, None)


def test_group_summary_connects_joined_pairs_only() -> None:
    pairs = [psic.PairInput("a", "b"), psic.PairInput("b", "c"), psic.PairInput("c", "d")]
    s = psic.group_summary(pairs, [True, True, False])
    assert s == {"groups": 1, "largest": 3, "nodes": 4}


class _FakeModel:
    feature_names = ("f1", "f2", "llm_same", "llm_known")

    def joins(self, vec: list[float], *, llm_available: bool) -> bool:
        return vec[2] == 1.0 and llm_available


def test_ml_joined_for_needs_features_and_passes_llm_flags() -> None:
    m = _FakeModel()
    assert psic.ml_joined_for(m, {"f1": 1.0, "f2": 2.0}, True) is True
    assert psic.ml_joined_for(m, {"f1": 1.0, "f2": 2.0}, None) is False
    assert psic.ml_joined_for(m, {"f1": 1.0}, True) is None
    assert psic.ml_joined_for(m, None, True) is None
    assert psic.ml_joined_for(None, {"f1": 1.0, "f2": 2.0}, True) is None


def test_load_pairs_reports_line_number(tmp_path: Path) -> None:
    f = tmp_path / "p.jsonl"
    f.write_text('{"left_id":"a","right_id":"b"}\n{"x":1}\n', encoding="utf-8")
    with pytest.raises(ValueError, match=":2 "):
        psic.load_pairs(f)
