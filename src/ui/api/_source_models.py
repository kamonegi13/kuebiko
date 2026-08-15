"""Phase F: Unified Source 抽象化 schema。

設計原則:
- transport (RSS/Atom/sitemap/HTML scraper) を **field 化** して UI から区別を見えなくする
- preview_articles を全 transport で uniform に提供 (user の意図確認 gate)
- quality 指標 (entries 数 / freshness / health) で同 schema で並列ランク
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

TransportLiteral = Literal["rss", "atom", "sitemap", "html_scraper"]


class PreviewArticle(BaseModel):
    """全 transport で uniform な記事 preview entry (user 意図確認用)。"""

    model_config = ConfigDict(frozen=True)

    title: str
    url: str
    published: datetime | None = None
    summary_preview: str = ""  # 先頭 200 char (RSS summary or trafilatura 抜粋)


class Quality(BaseModel):
    """SourceCandidate の量的健康指標。"""

    model_config = ConfigDict(frozen=True)

    entries_count: int = 0  # feed の最新 entry 数 / sitemap の URL 数
    freshness_days: int | None = None  # 最新 entry が何日前か
    health_score: int = 0  # 0-100 総合スコア (UI 並び順用)


class SourceCandidate(BaseModel):
    """transport 透過な統一 candidate。

    UI は transport を意識せず、 すべて同 row で表示する。
    user は preview_articles を見て「これが意図した source か」を判断する。
    """

    model_config = ConfigDict(frozen=False)  # mutate しないが ergonomic 優先

    transport: TransportLiteral
    fetch_url: str  # 実際に取得する URL (RSS/sitemap/listing page)
    source_name: str  # 表示用 (feed title / sitemap host / listing page title)
    path_display: str  # subcategory 識別の核 (URL path 部分)
    last_updated: datetime | None = None
    quality: Quality = Field(default_factory=Quality)
    preview_articles: list[PreviewArticle] = Field(default_factory=list)
    # debug/diagnosis 用 (UI には表示しない可)
    detected_via: str = ""  # "direct" / "html_link" / "common_path" / "subdomain" / "category_path"
    # html_scraper transport の場合のみ使用
    article_link_selector: str | None = None
    title_selector: str | None = None
    rationale: str | None = None  # LLM が selector を提案した根拠


class DiscoverRequest(BaseModel):
    """POST /api/v1/sources/discover の入力。"""

    url: str
    max_preview_per_candidate: int = 5


class DiscoverResponse(BaseModel):
    """POST /api/v1/sources/discover の応答。

    candidates が空なら HTML scrape mode への移行を UI が促す。
    """

    candidates: list[SourceCandidate] = Field(default_factory=list)
    html_scrape_eligible: bool = True
    notes: list[str] = Field(default_factory=list)
    original_url: str = ""


class PreviewHtmlListingRequest(BaseModel):
    """POST /api/v1/sources/preview_html_listing の入力。

    HTML scrape mode で user が「これが記事一覧 page」と指定した URL を解析する。
    LLM (main model, think=False) が CSS selector を提案 + sample articles preview を返す。
    """

    listing_url: str
    hint: str = ""  # 「advisory 系のみ」 など


class PreviewHtmlListingResponse(BaseModel):
    candidate: SourceCandidate | None = None  # transport=html_scraper の candidate
    error: str | None = None


class PreviewExplicitRequest(BaseModel):
    """POST /api/v1/sources/preview_html_listing_explicit の入力。

    visual picker / 手動入力で確定した CSS selector を LLM 非依存で適用する。
    """

    listing_url: str
    article_link_selector: str
    title_selector: str = ""


class LivePreviewRequest(BaseModel):
    """POST /api/v1/sources/live_preview の入力。

    登録済みソースが今も機能しているかを確認するため、feed_id で識別された
    ソースの最新情報をライブ取得する。fetch 先 URL は request ではなく config
    から解決する (任意 URL を fetch させない SSRF 対策)。
    """

    feed_id: str


class LivePreviewResponse(BaseModel):
    """live_preview の結果 (transport 透過)。"""

    ok: bool
    kind: str = ""  # rss / atom / html_scraper / sitemap
    checked_url: str = ""
    items: list[PreviewArticle] = Field(default_factory=list)
    error: str | None = None
    # どの段で取得できたか (fetch_policy)。"browser" = bot UA がブロックされており、
    # 本番の定期取得も同じエスカレーションで取得している (プレビュー = 本番と同一経路)
    fetch_stage: str = "bot"


class PreviewUrlRequest(BaseModel):
    """POST /api/v1/sources/preview_url の入力 (保存前の取得テスト)。

    編集中でまだ保存していない URL を対象にするため、config 解決ではなく
    request の URL を fetch する (SSRF は url_guard で塞ぐ)。
    """

    url: str
    kind: Literal["rss", "sitemap"]
    url_include_pattern: str = ""  # sitemap のみ


class DeleteSourceRequest(BaseModel):
    """POST /api/v1/sources/delete の入力。feed_id でソースを特定して削除する。"""

    feed_id: str


class DeleteSourceResponse(BaseModel):
    removed: bool
    commit: str | None = None
    error: str | None = None


class BulkSourcesRequest(BaseModel):
    """POST /api/v1/sources/bulk の入力。全 transport を feed_id で一括操作。"""

    feed_ids: list[str]
    action: Literal["enable", "disable", "delete"]


class BulkSourcesResponse(BaseModel):
    affected: int
    commit: str | None = None
    error: str | None = None


class SetFolderRequest(BaseModel):
    """POST /api/v1/sources/set_folder の入力。全 transport の folder を統一する。"""

    feed_ids: list[str]
    folder: str = ""  # 空文字で folder 解除


class SetFolderResponse(BaseModel):
    affected: int
    commit: str | None = None
    error: str | None = None


class RenameSourceRequest(BaseModel):
    """POST /api/v1/sources/set_display_name の入力。購読一覧の表示名を変更する。"""

    feed_id: str
    display_name: str


class RenameSourceResponse(BaseModel):
    affected: int
    commit: str | None = None
    error: str | None = None


class EditableSourceResponse(BaseModel):
    """GET /api/v1/sources/editable の出力 (編集フォームの初期値)。

    transport 固有フィールドは該当しない transport では null (UI は欄を出さない)。
    """

    feed_id: str
    transport: TransportLiteral
    display_name: str
    url: str
    folder: str
    enabled: bool
    article_link_selector: str | None = None
    url_include_pattern: str | None = None
    max_posts_per_run: int | None = None
    url_is_identity: bool = False


class UpdateSourceRequest(BaseModel):
    """POST /api/v1/sources/update の入力。null のフィールドは変更しない。"""

    feed_id: str
    url: str | None = None
    folder: str | None = None
    enabled: bool | None = None
    article_link_selector: str | None = None
    url_include_pattern: str | None = None
    max_posts_per_run: int | None = None


class UpdateSourceResponse(BaseModel):
    updated: bool
    # URL 変更で識別子が変わる (rss) ため、更新後の feed_id を返す
    feed_id: str
    error: str | None = None


class RegisterRequest(BaseModel):
    """POST /api/v1/sources/register の入力。

    Step 6 (UI) で user が name/folder/enabled を入力した後の登録。
    """

    candidate: SourceCandidate
    name: str  # kebab-case identifier
    folder: str = ""  # feed folder (RSS 用) or watcher group
    enabled: bool = True


class RegisterResponse(BaseModel):
    added: bool
    transport: TransportLiteral
    target_yaml: str  # config/sources/feeds.yaml / scrapers.yaml / watchers.yaml
    total_in_yaml: int
    appended_to_cluster: bool = False  # html_scraper 時 pipelines.yaml 追記成否
    commit: str | None = None
