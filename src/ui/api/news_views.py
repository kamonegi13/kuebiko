"""ニュースの絞り込みビュー (名前付きの絞り込みの組) の管理 API。

GET /api/v1/news-views   利用者保存ビュー一覧 (ops の DB SSoT)。
PUT /api/v1/news-views   全量保存 (検証 → config_store 版保存)。

既定のビュー (注目/日本/重大/脆弱性/国家系/地政学/すべて) は frontend 側の定数
(frontend/src/components/news/views.ts の BUILTIN_VIEWS) が持つ。ここで管理するのは
**利用者が保存した** ビューだけ (docs/news_filter_ux.md §3-4)。

READ_ONLY instance では PUT は middleware (REMOTE_WRITE_ALLOWLIST に未登録) が
403 で遮断する — 遠隔からの保存は意図的に許可しない (利用者判断、2026-10-04)。
写し (Cloudflare Pages, VITE_MIRROR=1 ビルド) はこの API を一切呼ばず、
利用者ビューは端末の localStorage のみに保存する。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from src.logging_config import get_logger
from src.storage.config_store import get_config, save_config

news_views_api = APIRouter(prefix="/api/v1/news-views", tags=["news-views"])

_log = get_logger(__name__)

_CONFIG_KEY = "news_views"

# テスト用の db_path 差し替えポイント (dashboard_layout.py と同じパターン)。
# 本番は None = config_store の既定 (DATABASE_URL / data/run_history.db)。
_DB_PATH: Path | None = None

# 保存件数の上限 (個人運用規模では十分大きい固定値。無制限 append による肥大を防ぐ)。
_MAX_VIEWS = 100


class NewsViewFilters(BaseModel):
    """絞り込みの組。すべて任意 — 未指定の項目はその軸を絞り込まない。

    フィールド名は NewsPage/EventNewsPage の URL クエリパラメータ名と揃える
    (docs/news_filter_ux.md: URL param 名と API は変えない)。
    """

    model_config = ConfigDict(extra="forbid")

    search: str | None = None
    min_severity: Literal["", "S3", "S2", "S1"] | None = None
    relevant_only: bool | None = None
    include_strategic: bool | None = None
    jp: Literal["", "targeted_affected", "mentioned"] | None = None
    since: str | None = None
    sort: str | None = None
    category: str | None = None
    feed: str | None = None
    intent: str | None = None
    pir: str | None = None
    actor: str | None = None
    affected_vendor: str | None = None
    body: str | None = None
    channel: str | None = None
    # 事象ニュース専用の軸
    min_independent_sources: int | None = None
    has_news: bool | None = None
    status: str | None = None


class NewsViewEntry(BaseModel):
    """1 件のビュー (利用者保存)。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=60)
    filters: NewsViewFilters


class SaveNewsViewsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    views: list[NewsViewEntry] = Field(max_length=_MAX_VIEWS)


@news_views_api.get("")
def list_news_views() -> dict[str, Any]:
    """利用者保存ビュー一覧 (DB)。未保存なら空配列 (既定ビューは含まない)。"""
    value = get_config(_CONFIG_KEY, db_path=_DB_PATH)
    views = value if isinstance(value, list) else []
    return {"views": views}


@news_views_api.put("")
def save_news_views(req: SaveNewsViewsRequest) -> dict[str, Any]:
    """全量保存 (リネーム/削除も含め、常に全件を送り直す)。id 重複は 400。"""
    ids = [v.id for v in req.views]
    if len(ids) != len(set(ids)):
        raise HTTPException(status_code=400, detail="ビュー id が重複しています")
    rows = [v.model_dump(exclude_none=True) for v in req.views]
    version = save_config(
        _CONFIG_KEY, rows, note="UI 保存 (ニュースの絞り込みビュー)", db_path=_DB_PATH
    )
    _log.info("news_views_saved", count=len(rows), version=version)
    return {"ok": True, "version": version, "count": len(rows)}
