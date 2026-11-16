"""src.tools.s_switch_relabel のテスト (S-SWITCH-RETRAIN の軸・確率の付け直し)。

LLM は偽物の関数で置き換える (Ollama・DB に繋がない)。出力の行が既存の消費者
(``staging_inputs.load_axes_jsonl`` / ``train_severity_s3_model.load_probes``) で
読めることも固定する。
"""

from __future__ import annotations

import importlib
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from src.cti import severity_c
from src.tools.s_switch_relabel import (
    RelabelItem,
    items_from_sevc_rows,
    read_done_ids,
    relabel_axes,
    relabel_probes,
)
from src.tools.staging_inputs import load_axes_jsonl

AXES = {
    "scope": "single_org",
    "impact": "destructive",
    "confirmation": "claimed_only",
    "exploitation": "not_applicable",
    "actor": "criminal",
    "target": "general_org",
    "magnitude": "unknown",
}
QIDS = ("q1", "q2", "q3")


def _item(aid: str, body: str = "本文") -> RelabelItem:
    return RelabelItem(aid, f"見出し {aid}", "要約", body, "2026-11-01")


def _rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


class _Clock:
    def __init__(self, step: float) -> None:
        self.now = 0.0
        self._step = step

    def __call__(self) -> float:
        self.now += self._step
        return self.now


async def _ok_axes(title: str, summary: str, body: str, pub: str | None) -> Mapping[str, Any]:
    return AXES


class TestRelabelAxes:
    @pytest.mark.asyncio
    async def test_writes_rows_readable_by_staging_loader(self, tmp_path: Path) -> None:
        out = tmp_path / "axes.jsonl"
        r = await relabel_axes([_item("a"), _item("b")], model="m:new", out=out, classify=_ok_axes)
        assert (r.written, r.failed, r.stopped_early) == (2, 0, False)
        loaded = load_axes_jsonl(out, "m:new")
        assert set(loaded) == {"a", "b"}
        assert loaded["a"]["impact"] == "destructive"
        assert loaded["a"]["model"] == "m:new"

    @pytest.mark.asyncio
    async def test_resume_skips_done_and_no_body(self, tmp_path: Path) -> None:
        out = tmp_path / "axes.jsonl"
        await relabel_axes([_item("a")], model="m", out=out, classify=_ok_axes)
        calls: list[str] = []

        async def classify(title: str, summary: str, body: str, pub: str | None) -> Any:
            calls.append(title)
            return AXES

        r = await relabel_axes(
            [_item("a"), _item("b"), _item("c", body="")], model="m", out=out, classify=classify
        )
        assert calls == ["見出し b"]
        assert (r.written, r.skipped_done, r.skipped_no_body) == (1, 1, 1)
        assert [x["article_id"] for x in _rows(out)] == ["a", "b"]

    @pytest.mark.asyncio
    async def test_failure_is_not_written(self, tmp_path: Path) -> None:
        out = tmp_path / "axes.jsonl"

        async def fail(title: str, summary: str, body: str, pub: str | None) -> Any:
            return None

        r = await relabel_axes([_item("a")], model="m", out=out, classify=fail)
        assert (r.written, r.failed) == (0, 1)
        assert read_done_ids(out) == set()

    @pytest.mark.asyncio
    async def test_limit_stops_early(self, tmp_path: Path) -> None:
        out = tmp_path / "axes.jsonl"
        items = [_item(x) for x in "abcd"]
        r = await relabel_axes(items, model="m", out=out, classify=_ok_axes, limit=2)
        assert (r.written, r.stopped_early) == (2, True)

    @pytest.mark.asyncio
    async def test_time_budget_stops_early(self, tmp_path: Path) -> None:
        out = tmp_path / "axes.jsonl"
        items = [_item(x) for x in "abcdef"]
        # 1 回の clock 呼び出しで 1 秒進む。期限 5 秒なら全 6 件は終わらない
        r = await relabel_axes(
            items, model="m", out=out, classify=_ok_axes, max_seconds=5, clock=_Clock(1.0)
        )
        assert 0 < r.written < len(items)
        assert r.stopped_early


class TestRelabelProbes:
    @pytest.mark.asyncio
    async def test_row_shape_and_partial_missing_kept(self, tmp_path: Path) -> None:
        out = tmp_path / "probe.jsonl"

        async def probe(title: str, pub: str | None, body: str) -> Mapping[str, float | None]:
            return {"q1": 0.9, "q2": None, "q3": 0.1}

        r = await relabel_probes(
            [_item("a")], model="m:new", out=out, probe=probe, question_ids=QIDS
        )
        assert r.written == 1
        (row,) = _rows(out)
        assert row == {
            "article_id": "a",
            "p_yes": {"q1": 0.9, "q2": None, "q3": 0.1},
            "probe_model": "m:new",
            "missing": 1,
            "from": "probe",
        }

    @pytest.mark.asyncio
    async def test_all_missing_is_not_written(self, tmp_path: Path) -> None:
        out = tmp_path / "probe.jsonl"

        async def probe(title: str, pub: str | None, body: str) -> Mapping[str, float | None]:
            return {}

        r = await relabel_probes([_item("a")], model="m", out=out, probe=probe, question_ids=QIDS)
        assert (r.written, r.failed) == (0, 1)
        assert not read_done_ids(out)

    @pytest.mark.asyncio
    async def test_rows_readable_by_severity_trainer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        out = tmp_path / "probe.jsonl"

        async def probe(title: str, pub: str | None, body: str) -> Mapping[str, float | None]:
            return dict.fromkeys(severity_c.QUESTION_IDS, 0.5)

        await relabel_probes(
            [_item("a"), _item("b")],
            model="m",
            out=out,
            probe=probe,
            question_ids=severity_c.QUESTION_IDS,
        )
        pytest.importorskip("sklearn")  # trainer は import 時に sklearn を読む (学習用の依存)
        monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
        sys.modules.pop("train_severity_s3_model", None)
        trainer = importlib.import_module("train_severity_s3_model")
        probes = trainer.load_probes("m", out, max_missing=2)
        assert set(probes) == {"a", "b"}


def test_items_from_sevc_rows_falls_back_to_summary() -> None:
    items = items_from_sevc_rows(
        [
            {"article_id": "a", "title": "t", "summary": "s", "body": "", "published": None},
            {"article_id": "b", "title": "t", "summary": "s", "body": "b", "published": "2026-11"},
        ]
    )
    assert [(i.body, i.published) for i in items] == [("s", None), ("b", "2026-11")]


def test_read_done_ids_ignores_broken_lines(tmp_path: Path) -> None:
    p = tmp_path / "x.jsonl"
    p.write_text('{"article_id": "a"}\n{broken\n\n{"no_id": 1}\n', encoding="utf-8")
    assert read_done_ids(p) == {"a"}


@pytest.mark.asyncio
async def test_probe_questions_passes_model_through(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str | None] = []

    async def fake_probe_choice(*args: Any, **kwargs: Any) -> dict[str, float]:
        seen.append(kwargs.get("model"))
        return {severity_c.CHOICES[0]: 0.7, severity_c.CHOICES[1]: 0.3}

    monkeypatch.setattr("src.tools.decision_probe.probe_choice", fake_probe_choice)
    out = await severity_c.probe_questions(
        title="t", published=None, body="b", config=object(), client=None, model="m:new"
    )
    assert set(seen) == {"m:new"}
    assert set(out) == set(severity_c.QUESTION_IDS)
    seen.clear()
    await severity_c.probe_questions(
        title="t", published=None, body="b", config=object(), client=None
    )
    assert set(seen) == {None}  # 省略時は従来どおり (割当から解決)
