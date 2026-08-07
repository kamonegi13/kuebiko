"""dashboard_layout (configurable widgets 永続化, config_store SSoT) の unit test。

2026-07-14: 保存先を file → config_store (app_config_versions) へ移行。
- SSoT = DB (版履歴つき)。旧ファイルは初回 seed 専用。
"""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.storage.config_store import list_history
from src.ui.api import dashboard_layout as dl


@pytest.fixture
def _tmp_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """DB と seed ファイルの両方を tmp に隔離する。返り値 = seed ファイルパス。"""
    monkeypatch.setattr(dl, "_DB_PATH", tmp_path / "layout.db")
    seed = tmp_path / "dashboard_layout.json"
    monkeypatch.setattr(dl, "_LAYOUT_PATH", seed)
    return seed


def test_default_when_nothing_saved(_tmp_store: Path) -> None:
    raw = dl._load_layout()
    layout = dl.DashboardLayout.model_validate(raw)  # default は v2 として妥当
    ids = [w.id for w in layout.widgets]
    assert "standing_assessment" in ids
    assert len(ids) > 0
    assert len(ids) == len(set(ids))
    # 全 widget がグリッド内に収まる (x+w <= 12)
    assert all(w.x + w.w <= dl.GRID_COLS for w in layout.widgets)


def test_save_load_roundtrip_with_version_history(_tmp_store: Path) -> None:
    saved = dl.DashboardLayout.model_validate(
        {
            "widgets": [
                {"id": "anomaly", "x": 0, "y": 0, "w": 3, "h": 10},
                {"id": "holdings", "x": 3, "y": 0, "w": 6, "h": 15},
            ]
        },
    )
    dl._save_layout(saved)
    loaded = dl.DashboardLayout.model_validate(dl._load_layout())
    assert [(w.id, w.x, w.y, w.w, w.h) for w in loaded.widgets] == [
        ("anomaly", 0, 0, 3, 10),
        ("holdings", 3, 0, 6, 15),
    ]
    # 版履歴が残る (運用 config 原則 = DB + 版履歴) — 2 回目の保存で version 2
    dl._save_layout(saved)
    history = list_history(dl._LAYOUT_KEY, db_path=dl._DB_PATH)
    assert [h.version for h in history] == [2, 1]


def test_seed_from_legacy_file_once(_tmp_store: Path) -> None:
    # 旧ファイルがあり DB が空 → 初回 load で seed され、以後はファイル無しでも DB から読める
    _tmp_store.write_text(
        json.dumps({"widgets": [{"id": "holdings", "x": 0, "y": 0, "w": 6, "h": 10}]}),
        encoding="utf-8",
    )
    first = dl.DashboardLayout.model_validate(dl._load_layout())
    assert [w.id for w in first.widgets] == ["holdings"]
    _tmp_store.unlink()  # ファイルを消しても DB (seed 済み) から読める
    second = dl.DashboardLayout.model_validate(dl._load_layout())
    assert [w.id for w in second.widgets] == ["holdings"]
    # seed は version 1 として履歴に残る
    history = list_history(dl._LAYOUT_KEY, db_path=dl._DB_PATH)
    assert len(history) == 1
    assert "seed" in history[0].note


def test_legacy_v1_value_is_passed_through(_tmp_store: Path) -> None:
    # v1 (順序+span+高さpx) は素通しで返す — frontend が座標へ移行し次回 PUT で v2 化する。
    legacy = {
        "widgets": [
            {"id": "kpi_row", "span": 4},
            {"id": "mini_map", "span": 2, "h": 480, "config": {}},
        ]
    }
    _tmp_store.write_text(json.dumps(legacy), encoding="utf-8")
    raw = dl._load_layout()
    assert raw == legacy  # span/h(px) をそのまま保持


def test_invalid_stored_value_falls_back_to_default(_tmp_store: Path) -> None:
    from src.storage.config_store import save_config

    save_config(dl._LAYOUT_KEY, {"widgets": [{"id": "x", "x": 99, "w": 99}]}, db_path=dl._DB_PATH)
    raw = dl._load_layout()
    assert len(raw["widgets"]) > 1  # default に倒れる


def test_grid_bounds_validation() -> None:
    # グリッド内は有効
    assert dl.WidgetPlacement(id="x", x=6, w=6).x == 6
    # x+w がグリッド幅を超えると拒否
    with pytest.raises(ValidationError):
        dl.WidgetPlacement(id="x", x=7, w=6)
    with pytest.raises(ValidationError):
        dl.WidgetPlacement(id="x", w=13)
    with pytest.raises(ValidationError):
        dl.WidgetPlacement(id="x", h=2)  # 最小 3 行


def test_put_rejects_legacy_span_key() -> None:
    # 書込境界は v2 のみ (extra=forbid)。legacy キー混入は拒否 → v1/v2 判別が決定的に保たれる。
    with pytest.raises(ValidationError):
        dl.WidgetPlacement.model_validate({"id": "x", "span": 2})
