"""ダッシュボード レイアウト永続化 (v2: 明示座標グリッド)。

ユーザが配置した widget の座標 (x,y,w,h) と共有既定 config を保存する。
- GET  /api/v1/dashboard/layout : 現レイアウト (無ければ default)
- PUT  /api/v1/dashboard/layout : 保存 (READ_ONLY instance は app.py middleware が 403)

widget の中身 (registry) は frontend 側で定義。ここは「どの id を / どの座標に」だけを
保持する (server は widget 実装を知らない = 疎結合、将来 widget 追加が容易)。

保存先 (2026-07-14): **config_store (app_config_versions) が SSoT** — 「運用 config は DB +
版履歴」の原則に整合し、保存ごとに版が残り /api/v1/config-history/dashboard_layout で
履歴閲覧・revert できる。旧ファイル data/dashboard_layout.json は初回 seed 専用
(source_store の yaml seed と同じ規約。以後ファイルには書かない)。

v2 (2026-07-10): 旧 v1 (順序 + span + 実測 masonry) は編集 chrome を含んで高さを測る →
保存で chrome が消えて再計算 → ズレが連鎖する構造的欠陥があったため、12 カラム × 8px 行の
明示座標 (react-grid-layout) に移行。v1 値は GET で素通しし、frontend が座標へ移行して
次回 PUT で v2 化する (書込境界は v2 のみ受理)。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from src.logging_config import get_logger
from src.storage.config_store import get_config, save_config, seed_config_if_absent

_log = get_logger(__name__)

dashboard_layout_api = APIRouter(prefix="/api/v1/dashboard", tags=["dashboard"])

# config_store のキー (版履歴の単位)。config_history._KNOWN_KEYS にも登録済み。
_LAYOUT_KEY = "dashboard_layout"
# tests が monkeypatch する DB パス (None = 既定 = DATABASE_URL / dev SQLite)。
_DB_PATH: Path | None = None

# 初回 seed 専用の旧ファイル (CWD 非依存の絶対パス。security-review L3 準拠)。
# src/ui/api/dashboard_layout.py → parents[3] = リポジトリルート。
_LAYOUT_PATH = Path(__file__).resolve().parents[3] / "data" / "dashboard_layout.json"

# グリッド定数 (frontend の react-grid-layout 設定と一致させる)。
# 12 カラム × 8px 行、margin 12px → 1 行増加あたり実効 20px。
GRID_COLS = 12

# default layout (v2 明示座標)。file が無いときに返す。h の目安: 20 行 ≈ 390px。
# 既定レイアウト = 二眼(戦術×戦略)概観。上から KPI → システム → 統合 narrative →
# 脅威の構成と変化 / 今動いているアクター / 重要脆弱性 / PIR → ニュース脈。
# y は隙間なく連続させる (vertical compaction が no-op になる値)。
_DEFAULT_WIDGETS: list[dict[str, object]] = [
    {"id": "kpi_row", "x": 0, "y": 0, "w": 12, "h": 7},
    {"id": "status_strip", "x": 0, "y": 7, "w": 12, "h": 5},
    {"id": "standing_assessment", "x": 0, "y": 12, "w": 12, "h": 13},
    {"id": "situation", "x": 0, "y": 25, "w": 6, "h": 20},
    {"id": "geo_ranking", "x": 6, "y": 25, "w": 6, "h": 20},
    {"id": "geo_trend", "x": 0, "y": 45, "w": 12, "h": 11},
    {"id": "threat_picture", "x": 0, "y": 56, "w": 6, "h": 22},
    {"id": "jp_ci_threat", "x": 6, "y": 56, "w": 6, "h": 22, "config": {"days": 90}},
    {"id": "notable_actors", "x": 0, "y": 78, "w": 6, "h": 18},
    {"id": "vulnerabilities_kev", "x": 6, "y": 78, "w": 6, "h": 18},
    {"id": "pir_coverage", "x": 0, "y": 96, "w": 6, "h": 16},
    {"id": "holdings", "x": 6, "y": 96, "w": 6, "h": 16},
    {"id": "latest_headlines", "x": 0, "y": 112, "w": 12, "h": 24, "config": {"axis": "pmesii"}},
    # /app/health ページ廃止 (P4) に伴い死活監視を既定配置 (保存済みレイアウトには
    # 影響しない — 既存利用者はツールボックスから追加)。
    {"id": "health", "x": 0, "y": 136, "w": 6, "h": 14},
]


class WidgetPlacement(BaseModel):
    """v2: 12 カラム × 8px 行グリッドの明示座標。編集/表示が同一座標 = 保存でズレない。"""

    # legacy キー (span 等) の混入を書込境界で拒否 (v1/v2 の判別を決定的に保つ)。
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    # 一意インスタンス ID (同一 widget の設定違い複数配置用)。空なら frontend が採番。
    uid: str = Field(default="", max_length=64)
    x: int = Field(default=0, ge=0, le=GRID_COLS - 1)
    y: int = Field(default=0, ge=0, le=100_000)
    w: int = Field(default=6, ge=1, le=GRID_COLS)
    # 高さ (行数)。1 行 ≈ 20px、300 行 ≈ 6000px を上限とする。
    h: int = Field(default=15, ge=3, le=300)
    # per-widget 設定 (軸/件数/期間 等)。server は中身を解釈せず保持するだけ
    # (frontend widget が定義 = 疎結合、configurable widget の汎用基盤)。
    config: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _within_grid(self) -> WidgetPlacement:
        if self.x + self.w > GRID_COLS:
            msg = f"widget exceeds grid: x={self.x} + w={self.w} > {GRID_COLS}"
            raise ValueError(msg)
        return self


class DashboardLayout(BaseModel):
    widgets: list[WidgetPlacement] = Field(default_factory=list)


def _default_layout() -> dict[str, Any]:
    return DashboardLayout.model_validate({"widgets": _DEFAULT_WIDGETS}).model_dump()


def _is_legacy_layout(raw: dict[str, Any]) -> bool:
    """v1 (順序+span) 形式か。v2 は span を書かない (extra=forbid) ため決定的に判別できる。"""
    widgets = raw.get("widgets")
    return isinstance(widgets, list) and any(isinstance(w, dict) and "span" in w for w in widgets)


def _seed_from_file() -> dict[str, Any] | None:
    """旧ファイルが在れば config_store へ一度だけ取り込む (yaml seed と同じ規約)。"""
    if not _LAYOUT_PATH.exists():
        return None
    try:
        raw = json.loads(_LAYOUT_PATH.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        _log.warning("dashboard_layout_seed_failed", error=str(e))
        return None
    if not isinstance(raw, dict):
        return None
    seed_config_if_absent(
        _LAYOUT_KEY, raw, note="seed (data/dashboard_layout.json import)", db_path=_DB_PATH
    )
    return raw


def _load_layout() -> dict[str, Any]:
    """保存済みレイアウトを返す。legacy v1 は素通し (frontend が座標へ移行し次回 PUT で v2 化)。"""
    raw = get_config(_LAYOUT_KEY, db_path=_DB_PATH)
    if raw is None:
        raw = _seed_from_file()
    if raw is None or not isinstance(raw, dict):
        return _default_layout()
    if _is_legacy_layout(raw):
        return raw
    try:
        return DashboardLayout.model_validate(raw).model_dump()
    except ValidationError as e:
        _log.warning("dashboard_layout_invalid", error=str(e))
        return _default_layout()


def _save_layout(layout: DashboardLayout) -> None:
    version = save_config(
        _LAYOUT_KEY,
        layout.model_dump(),
        note=f"ウィジェット {len(layout.widgets)} 件を保存",
        db_path=_DB_PATH,
    )
    _log.info("dashboard_layout_saved_version", version=version)


@dashboard_layout_api.get("/layout")
def get_layout() -> dict[str, Any]:
    return _load_layout()


@dashboard_layout_api.put("/layout", response_model=DashboardLayout)
def put_layout(layout: DashboardLayout) -> DashboardLayout:
    _save_layout(layout)
    _log.info("dashboard_layout_saved", widget_count=len(layout.widgets))
    return layout


# ── 概観 (overview) 集計: 変化 + 内訳。 既存 PIR dashboard に倣い 60s in-process cache。 ──
_OVERVIEW_TTL_SECONDS = 60.0
_overview_cache: dict[int, tuple[float, dict[str, Any]]] = {}


@dashboard_layout_api.get("/overview")
def get_overview(days: int = 7) -> dict[str, Any]:
    """二眼概観の集計 (セクター/地域/敵対国籍の前期比 + KEV脆弱 + notable + 信頼度構成)。

    返す count は「報道/観測 件数」(攻撃件数ではない)。read-only・counts のみ (secret 非露出)。
    """
    import time

    from src.ui.services.overview import build_overview

    window = max(1, min(90, days))
    now = time.monotonic()
    cached = _overview_cache.get(window)
    if cached is not None and now - cached[0] < _OVERVIEW_TTL_SECONDS:
        return cached[1]
    data = build_overview(window_days=window)
    _overview_cache[window] = (now, data)
    return data
