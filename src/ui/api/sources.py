"""Phase F: 統合 Source API。

unified SourceCandidate schema で全 transport (RSS/Atom/sitemap/HTML scraper) を統一表現する。

Endpoints:
- POST /api/v1/sources/discover: RSS/Atom/sitemap を並列 probe + preview 添付
- POST /api/v1/sources/preview_html_listing: LLM CSS selector + sample preview
- POST /api/v1/sources/preview_html_listing_explicit: 確定 selector を LLM 非依存で適用
- GET  /api/v1/sources/proxy_page: visual picker 用 same-origin proxy (READ_ONLY で 403)
- POST /api/v1/sources/register: transport に応じた yaml + pipelines.yaml + git commit

Phase E-1 の /auto_detect は Phase F MVP で削除 (V2 wizard が /discover に統一移行済)。
"""

from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse

from src.logging_config import get_logger
from src.tools.url_guard import UnsafeUrlError
from src.ui.api._source_discovery import discover_sources_async
from src.ui.api._source_html_preview import (
    preview_html_listing,
    preview_html_listing_explicit,
)
from src.ui.api._source_live_preview import preview_subscription
from src.ui.api._source_models import (
    BulkSourcesRequest,
    BulkSourcesResponse,
    DeleteSourceRequest,
    DeleteSourceResponse,
    DiscoverRequest,
    DiscoverResponse,
    EditableSourceResponse,
    LivePreviewRequest,
    LivePreviewResponse,
    PreviewExplicitRequest,
    PreviewHtmlListingRequest,
    PreviewHtmlListingResponse,
    PreviewUrlRequest,
    RegisterRequest,
    RegisterResponse,
    RenameSourceRequest,
    RenameSourceResponse,
    SetFolderRequest,
    SetFolderResponse,
    UpdateSourceRequest,
    UpdateSourceResponse,
)
from src.ui.api._source_proxy import build_proxy_html
from src.ui.api._source_writer import register_source, unregister_source

_log = get_logger(__name__)

# READ_ONLY=1 (mobile 公開 instance) では proxy_page を無効化し open proxy 化を防ぐ。
_READ_ONLY = os.environ.get("READ_ONLY", "0").strip() in ("1", "true", "yes", "on")

sources_api = APIRouter(prefix="/api/v1/sources", tags=["sources"])


# ===== Phase F endpoints =====


@sources_api.post("/discover", response_model=DiscoverResponse)
async def discover(req: DiscoverRequest) -> DiscoverResponse:
    """unified discovery: RSS/Atom/sitemap を統一 schema で返す。

    候補が 0 件なら UI が HTML scrape mode を促す。
    """
    candidates, notes = await discover_sources_async(
        req.url,
        max_preview_per_candidate=req.max_preview_per_candidate,
    )
    return DiscoverResponse(
        candidates=candidates,
        html_scrape_eligible=True,
        notes=notes,
        original_url=req.url,
    )


@sources_api.post("/preview_html_listing", response_model=PreviewHtmlListingResponse)
async def preview_html_listing_endpoint(
    req: PreviewHtmlListingRequest,
) -> PreviewHtmlListingResponse:
    """HTML scrape mode: user が指定した listing URL に対して LLM CSS selector + preview。"""
    try:
        candidate = await preview_html_listing(
            req.listing_url,
            hint=req.hint,
            max_preview=5,
        )
        return PreviewHtmlListingResponse(candidate=candidate, error=None)
    except Exception as e:  # noqa: BLE001
        _log.warning("preview_html_listing_failed", url=req.listing_url, error=str(e))
        return PreviewHtmlListingResponse(candidate=None, error=str(e))


@sources_api.post(
    "/preview_html_listing_explicit",
    response_model=PreviewHtmlListingResponse,
)
async def preview_html_listing_explicit_endpoint(
    req: PreviewExplicitRequest,
) -> PreviewHtmlListingResponse:
    """visual picker / 手動入力で確定した selector を LLM 非依存で適用し preview を返す。"""
    try:
        candidate = await preview_html_listing_explicit(
            req.listing_url,
            article_link_selector=req.article_link_selector,
            title_selector=req.title_selector,
            max_preview=5,
            url_include_pattern=req.url_include_pattern,
        )
        return PreviewHtmlListingResponse(candidate=candidate, error=None)
    except Exception as e:  # noqa: BLE001
        _log.warning("preview_explicit_failed", url=req.listing_url, error=str(e))
        return PreviewHtmlListingResponse(candidate=None, error=str(e))


@sources_api.get("/proxy_page", response_class=HTMLResponse)
async def proxy_page(url: str, render: str = "auto") -> HTMLResponse:
    """visual picker 用: 指定 URL を sanitize + picker 注入して same-origin 配信。

    READ_ONLY instance では 403 (open proxy 防止 — GET なので read-only middleware を
    素通りするため endpoint 側で明示 block する)。SSRF は build_proxy_html 内で検証。
    """
    if _READ_ONLY:
        raise HTTPException(status_code=403, detail="閲覧専用のため利用できません")
    try:
        html = await build_proxy_html(url, render_mode=render)
    except UnsafeUrlError as e:
        raise HTTPException(status_code=400, detail=f"危険な URL です: {e}") from e
    except Exception as e:  # noqa: BLE001
        _log.warning("proxy_page_failed", url=url, error=str(e))
        raise HTTPException(status_code=502, detail="サイトの取得に失敗しました") from e
    return HTMLResponse(
        content=html,
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": "script-src 'self' 'unsafe-inline'; object-src 'none'",
        },
    )


@sources_api.post("/live_preview", response_model=LivePreviewResponse)
async def live_preview(req: LivePreviewRequest) -> LivePreviewResponse:
    """登録済みソースの最新情報をライブ取得し、機能しているか確認する。

    fetch 先 URL は config から解決 (request の任意 URL は fetch しない)。
    """
    try:
        return await preview_subscription(req.feed_id)
    except UnsafeUrlError as e:
        return LivePreviewResponse(ok=False, error=f"unsafe url: {e}")
    except Exception as e:  # noqa: BLE001
        _log.warning("live_preview_failed", feed_id=req.feed_id, error=str(e))
        return LivePreviewResponse(ok=False, error=str(e))


@sources_api.post("/preview_url", response_model=LivePreviewResponse)
def preview_url_endpoint(req: PreviewUrlRequest) -> LivePreviewResponse:
    """保存前の取得テスト: 編集中の URL を今その場で取得して中身を返す。

    「URL を直したが本当に取れるのか」を保存前に確かめられるようにする
    (壊れた設定を保存すると、次の定期実行まで無音で気付けないため)。
    """
    url = req.url.strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(
            status_code=400, detail="URL は http:// または https:// で始めてください"
        )
    try:
        from src.ui.api._source_live_preview import preview_url

        return preview_url(req.kind, url, url_include_pattern=req.url_include_pattern.strip())
    except UnsafeUrlError as e:
        return LivePreviewResponse(ok=False, error=f"安全でない URL です: {e}")
    except Exception as e:  # noqa: BLE001
        _log.warning("preview_url_failed", error=f"{type(e).__name__}: {e}")
        return LivePreviewResponse(ok=False, error=str(e))


@sources_api.post("/delete", response_model=DeleteSourceResponse)
def delete_source(req: DeleteSourceRequest) -> DeleteSourceResponse:
    """feed_id でソースを特定し全 transport (rss/scraper/watcher) を削除 + git commit。

    READ_ONLY instance では read-only middleware が POST を 403 で block する。
    """
    try:
        removed, commit = unregister_source(req.feed_id)
    except Exception as e:  # noqa: BLE001
        _log.warning("delete_source_failed", feed_id=req.feed_id, error=str(e))
        return DeleteSourceResponse(removed=False, error=str(e))
    if not removed:
        return DeleteSourceResponse(removed=False, error="該当ソースが見つかりません")
    return DeleteSourceResponse(removed=True, commit=commit)


@sources_api.post("/bulk", response_model=BulkSourcesResponse)
def bulk_sources(req: BulkSourcesRequest) -> BulkSourcesResponse:
    """全 transport (rss/scraper/watcher) を feed_id で一括 enable/disable/delete。

    READ_ONLY instance では write middleware が POST を 403 で block する。
    """
    if not req.feed_ids:
        raise HTTPException(status_code=400, detail="対象のソースを選択してください")
    try:
        from src.ui.api._source_manager import bulk

        affected, commit = bulk(req.feed_ids, req.action)
    except Exception as e:  # noqa: BLE001
        _log.warning("bulk_sources_failed", action=req.action, error=str(e))
        return BulkSourcesResponse(affected=0, error=str(e))
    return BulkSourcesResponse(affected=affected, commit=commit)


@sources_api.post("/set_folder", response_model=SetFolderResponse)
def set_folder_endpoint(req: SetFolderRequest) -> SetFolderResponse:
    """全 transport の folder を feed_id 指定で一括設定 (空文字で解除)。"""
    if not req.feed_ids:
        raise HTTPException(status_code=400, detail="対象のソースを選択してください")
    import re as _re

    if not _re.match(r"^[a-z0-9_]*$", req.folder):
        raise HTTPException(status_code=400, detail="folder は英小文字+数字+_ のみ (空可)")
    try:
        from src.ui.api._source_manager import set_folder

        affected, commit = set_folder(req.feed_ids, req.folder.strip())
    except Exception as e:  # noqa: BLE001
        _log.warning("set_folder_failed", error=str(e))
        return SetFolderResponse(affected=0, error=str(e))
    return SetFolderResponse(affected=affected, commit=commit)


@sources_api.post("/set_display_name", response_model=RenameSourceResponse)
def set_display_name_endpoint(req: RenameSourceRequest) -> RenameSourceResponse:
    """既存ソースの購読一覧表示名を変更 (RSS=name / scraper・watcher=feed_title)。

    READ_ONLY instance では write middleware が POST を 403 で block する。
    """
    if not req.feed_id.strip():
        raise HTTPException(status_code=400, detail="対象のソースを選択してください")
    if not req.display_name.strip():
        raise HTTPException(status_code=400, detail="表示名は空にできません")
    try:
        from src.ui.api._source_manager import rename_source

        affected, commit = rename_source(req.feed_id, req.display_name)
    except Exception as e:  # noqa: BLE001
        _log.warning("set_display_name_failed", feed_id=req.feed_id, error=str(e))
        return RenameSourceResponse(affected=0, error=str(e))
    if affected == 0:
        return RenameSourceResponse(affected=0, error="該当ソースが見つかりません")
    return RenameSourceResponse(affected=affected, commit=commit)


@sources_api.get("/folders")
def list_folders() -> dict[str, list[str]]:
    """登録済みソースで実際に使われているフォルダ一覧 (分類の SSoT)。

    候補をコード側に固定すると分類を変えたときに stale 化する (旧 news_en/advisory_jp
    のハードコードが実データと乖離していた) ため、常に実データから引く。
    """
    from src.ui.api._source_manager import list_sources

    try:
        return {"folders": sorted({s.folder for s in list_sources() if s.folder})}
    except Exception as e:  # noqa: BLE001 — 一覧取得失敗で登録フローを止めない
        _log.warning("list_folders_failed", error=str(e))
        return {"folders": []}


@sources_api.get("/editable", response_model=EditableSourceResponse)
def get_editable_endpoint(feed_id: str) -> EditableSourceResponse:
    """1 ソースの編集可能フィールドを返す (編集フォームの初期値)。"""
    from src.ui.api._source_editor import get_editable

    src = get_editable(feed_id)
    if src is None:
        raise HTTPException(status_code=404, detail="該当ソースが見つかりません")
    return EditableSourceResponse(**vars(src))


# 1 回あたり取得件数の上限。大きすぎる値で 1 ソースが run 時間を食い潰すのを防ぐ。
_MAX_POSTS_CEILING = 50
_MAX_SELECTOR_LEN = 200


def _validate_update(req: UpdateSourceRequest) -> None:
    """入力を境界で検証する。壊れた設定を保存させない (取得は次 run まで走らないため)。"""
    import re as _re

    if req.url is not None:
        url = req.url.strip()
        if not url.startswith(("http://", "https://")):
            raise HTTPException(
                status_code=400, detail="URL は http:// または https:// で始めてください"
            )
        try:
            from src.tools.url_guard import assert_safe_public_url

            assert_safe_public_url(url)
        except UnsafeUrlError as e:
            raise HTTPException(status_code=400, detail=f"安全でない URL です: {e}") from e
    if req.folder is not None and not _re.match(r"^[a-z0-9_]*$", req.folder.strip()):
        raise HTTPException(status_code=400, detail="folder は英小文字+数字+_ のみ (空可)")
    if req.article_link_selector is not None:
        sel = req.article_link_selector.strip()
        if not sel or len(sel) > _MAX_SELECTOR_LEN:
            raise HTTPException(
                status_code=400,
                detail=f"記事リンクのセレクタは 1〜{_MAX_SELECTOR_LEN} 文字で指定してください",
            )
    if req.url_include_pattern:
        try:
            _re.compile(req.url_include_pattern)
        except _re.error as e:
            raise HTTPException(status_code=400, detail=f"URL パターンが不正です: {e}") from e
    if req.max_posts_per_run is not None and not (1 <= req.max_posts_per_run <= _MAX_POSTS_CEILING):
        raise HTTPException(
            status_code=400,
            detail=f"1 回あたり取得件数は 1〜{_MAX_POSTS_CEILING} で指定してください",
        )


@sources_api.post("/update", response_model=UpdateSourceResponse)
def update_source_endpoint(req: UpdateSourceRequest) -> UpdateSourceResponse:
    """1 ソースの取得設定を更新する (削除→再登録なしで直せるようにする)。"""
    if not req.feed_id.strip():
        raise HTTPException(status_code=400, detail="対象のソースを選択してください")
    _validate_update(req)
    from src.ui.api._source_editor import SourcePatch, update_source

    patch = SourcePatch(
        url=req.url.strip() if req.url is not None else None,
        folder=req.folder.strip() if req.folder is not None else None,
        enabled=req.enabled,
        article_link_selector=(
            req.article_link_selector.strip() if req.article_link_selector is not None else None
        ),
        url_include_pattern=(
            req.url_include_pattern.strip() if req.url_include_pattern is not None else None
        ),
        max_posts_per_run=req.max_posts_per_run,
    )
    try:
        new_feed_id, changed = update_source(req.feed_id, patch)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001 — 保存失敗を 500 にせず UI にそのまま見せる
        _log.warning("update_source_failed", error=f"{type(e).__name__}: {e}")
        return UpdateSourceResponse(updated=False, feed_id=req.feed_id, error=str(e))
    return UpdateSourceResponse(updated=changed, feed_id=new_feed_id)


@sources_api.post("/register", response_model=RegisterResponse)
def register(req: RegisterRequest) -> RegisterResponse:
    """transport に応じて適切な yaml + pipelines.yaml に登録 + git commit。"""
    import re

    name = req.name.strip()
    if not re.match(r"^[a-z0-9][a-z0-9\-]*$", name):
        raise HTTPException(
            status_code=400,
            detail="name は kebab-case (英小+数+-) のみ",
        )
    if not req.candidate.source_name:
        raise HTTPException(status_code=400, detail="ソース名が空です")
    try:
        target_yaml, total, appended, commit = register_source(
            req.candidate,
            name,
            req.folder.strip(),
            req.enabled,
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except FileNotFoundError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    return RegisterResponse(
        added=True,
        transport=req.candidate.transport,
        target_yaml=target_yaml,
        total_in_yaml=total,
        appended_to_cluster=appended,
        commit=commit,
    )
