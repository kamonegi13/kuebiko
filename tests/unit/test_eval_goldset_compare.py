"""gold set の比較コマンドのテスト (対照必須化、2026-08-18)。

``compare A B`` は差を出すだけで**床 (同一設定でも動く幅) を教えなかった**。
temperature 0.3 で出力は確率的なため、床を取らずに変種と基準を比べると揺らぎを
効果と誤読する。器の側で対照を要求して事故を防ぐ。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from eval_goldset import (  # noqa: E402
    _agreement,
    _fill_rate,
    cmd_compare,
)


def _write_run(runs_dir: Path, label: str, rows: list[dict[str, Any]]) -> None:
    runs_dir.mkdir(parents=True, exist_ok=True)
    with (runs_dir / f"{label}.jsonl").open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _args(**kw: Any) -> argparse.Namespace:
    base: dict[str, Any] = {
        "label_a": "a",
        "label_b": "b",
        "floor": None,
        "no_floor": False,
        "show_ids": False,
    }
    base.update(kw)
    return argparse.Namespace(**base)


class TestFloorIsRequired:
    def test_missing_floor_stops_with_explanation(self, capsys: pytest.CaptureFixture[str]) -> None:
        code = cmd_compare(_args())

        assert code == 2
        err = capsys.readouterr().err
        assert "--floor" in err
        assert "揺らぎを効果と誤読" in err  # なぜ必要かを説明する

    def test_no_floor_opt_out_is_allowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import eval_goldset

        monkeypatch.setattr(eval_goldset, "RUNS_DIR", tmp_path)
        _write_run(tmp_path, "a", [{"article_id": "1", "summary": "x"}])
        _write_run(tmp_path, "b", [{"article_id": "1", "summary": "y"}])

        code = cmd_compare(_args(no_floor=True))

        assert code == 0
        assert "対照なし" in capsys.readouterr().out


class TestVerdictUsesFloor:
    """床を超えた変化だけ ★ を付ける (同じ差でも床が広ければ「床の内」)。"""

    def _setup(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, floor_noise: bool) -> None:
        import eval_goldset

        monkeypatch.setattr(eval_goldset, "RUNS_DIR", tmp_path)
        ids = [str(i) for i in range(10)]
        # A: 全件で note あり / B: 全件で note 空 = 充足率 -100pt
        _write_run(tmp_path, "a", [{"article_id": i, "note": "v"} for i in ids])
        _write_run(tmp_path, "b", [{"article_id": i, "note": ""} for i in ids])
        # 対照: floor_noise=True なら同一設定でも全件揺れる (床 100pt)
        _write_run(tmp_path, "c1", [{"article_id": i, "note": "v"} for i in ids])
        _write_run(
            tmp_path,
            "c2",
            [{"article_id": i, "note": "" if floor_noise else "v"} for i in ids],
        )

    def test_change_beyond_floor_is_flagged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._setup(tmp_path, monkeypatch, floor_noise=False)

        cmd_compare(_args(floor=["c1", "c2"]))

        line = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("note")][0]
        assert "★" in line

    def test_change_within_floor_is_not_flagged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._setup(tmp_path, monkeypatch, floor_noise=True)

        cmd_compare(_args(floor=["c1", "c2"]))

        line = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("note")][0]
        assert "床の内" in line


class TestMetrics:
    def _run(self, values: list[Any]) -> dict[str, dict[str, Any]]:
        return {str(i): {"article_id": str(i), "f": v} for i, v in enumerate(values)}

    def test_fill_rate_counts_non_empty(self) -> None:
        run = self._run(["v", "", None, [], "w"])

        assert _fill_rate(run, "f", [str(i) for i in range(5)]) == pytest.approx(0.4)

    def test_agreement_is_order_insensitive_for_lists(self) -> None:
        x = {"1": {"f": ["b", "a"]}}
        y = {"1": {"f": ["a", "b"]}}

        assert _agreement(x, y, "f", ["1"]) == 1.0

    def test_empty_and_none_count_as_equal(self) -> None:
        x = {"1": {"f": None}}
        y = {"1": {"f": ""}}

        assert _agreement(x, y, "f", ["1"]) == 1.0
