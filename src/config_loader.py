"""設定ローダ (Step 3)。

ローダは 3 系統:

1. ``AppConfig``: ``.env`` および環境変数から読むランタイム設定 (pydantic-settings)
2. ``PipelineConfig``: ``config/pipelines.yaml`` のパイプライン定義 (pyyaml)

公開関数:
    ``load_app_config()`` -> ``AppConfig``
    ``load_pipelines(path=...)`` -> ``list[PipelineConfig]``

エラーは可能な限り「どのファイル / どのキーが原因か」を判別できる
``FileNotFoundError`` または ``ValueError`` (元の例外を ``__cause__`` に保持)
として返す。
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# ---------- Type aliases ----------

# C1 (チャンネル config-driven 化): Literal 固定 → str に緩和。
# チャンネル定義の SSoT は src/tools/channel_registry.py (DB 正 + built-in fallback)。
# built-in 5: alert(即応) / brief(朝読) / watch(弱信号) / ops(運用) / japan_watch(日本標的)
# 値の検証は channel_registry.known_channel_ids() に対する validator で行う。
DiscordChannel = str
SourceType = Literal[
    "rss",
    "grok_email",
    "web_scraper",
    "web_scraper_cluster",
    # Phase 5T-J: DB に蓄積済 article から digest を生成する集約 source
    # digest_research は Phase Diamond pir-daily-focus で pir_daily_focus に置き換え済
    "digest_weekly_recap",  # F1: weekly recap (中間層を 7d まとめ)
    # Phase G1 trend は Phase H で UI (V1/V3) に移行、配信 source としては廃止
    # Phase H: 週次 LLM taxonomy review (新カテゴリ提案、UI で承認)
    "taxonomy_review",
    # Phase 3 (Synthesis): 週次 / 月次 状況総括の永続化
    "status_synthesis",
    # Phase Diamond verify-spotlight: PIR 縦断 narrative
    "pir_spotlight",
    # Actors Stage 4: MITRE ATT&CK → Actor 辞書の週次逐次同期 (自動適用 + レビュー提案)
    "mitre_actor_sync",
    # 段4(a) 朝レンダー統合: synthesis ナラティブ + PIR daily focus を 1 製品に統合
    # (旧 pir_daily_focus + daily-status-synthesis-morning の重複投稿を解消)
    "morning_brief",
    # 較正格子 P1 (self_evolving_tuning_design §6): 遅延正解ラベルの週次収穫
    "tuning_label_harvest",
]
ExtractMethod = Literal["trafilatura", "playwright"]


# ---------- AppConfig (.env / 環境変数) ----------


class AppConfig(BaseSettings):
    """``.env`` と環境変数から読むランタイム設定。

    必須: Discord Webhook 1 本 (alert)。
    任意: IMAP / Ollama / システムは sensible default 付き。

    ``.env`` は CWD 直下を参照する。テストでは ``monkeypatch.chdir`` で
    分離可能 (詳細は ``tests/test_config_loader.py``)。
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        frozen=True,
    )

    # --- Discord Webhooks (役割ベース 5 チャンネル) ---
    # alert: 即応 / brief: 朝読 / watch: 観察 / ops: 運用 / japan_watch: 日本標的
    # japan_watch は省略可。空時は alert にフォールバック (registry の fallback 定義)。
    discord_webhook_alert: str = ""
    discord_webhook_brief: str = ""
    discord_webhook_watch: str = ""
    discord_webhook_ops: str = ""
    discord_webhook_japan_watch: str = ""
    # Phase 5T-K: digest link 生成用 (https://discord.com/channels/{guild}/{ch}/{msg})
    # 空なら digest は元記事 URL で表示 (fallback)。
    discord_guild_id: str = ""

    # --- IMAP (Phase 2 で利用) ---
    imap_host: str = "imap.gmail.com"
    imap_port: int = 993
    imap_user: str = ""
    imap_password: str = ""

    # --- Ollama ---
    ollama_base_url: str = "http://localhost:11434"
    # モデルの実割当は能力ティア方式 (src/tools/model_tiers.py)。runtime の SSoT は DB
    # (config_store "model_tiers", UI「モデル」タブ)。bootstrap 既定はコードの
    # BUILTIN_MODEL_TIERS で、.env はモデル選択に関与しない (2026-07-08 に MAIN/SYNTHESIS/
    # EMBED/EXTRACT/SPOTLIGHT スロットを撤去)。ここに残るのは base_url と埋込 query prefix のみ。
    # 検索改善 A1: asymmetric retrieval の query prefix (snowflake-arctic-embed2 /
    # multilingual-e5 は query 側のみ "query: " を前置)。モデルに応じて上書き可。
    ollama_embed_query_prefix: str = "query: "

    # --- 外部 LLM (任意、2026-07-18 §4 改訂) ---
    # Anthropic Messages API の API キー。空なら外部 LLM は選択肢にすら出ない (ローカルのみ)。
    # 設定した上で、UI「モデル」タブでティアに anthropic:<model> を明示割当した場合のみ
    # 当該ティアの処理が外部 API に送られる (勝手に送らない)。
    anthropic_api_key: str = ""
    # Claude Code サブスク bridge (scripts/claude_code_bridge.py、ホスト側 127.0.0.1:8010)。
    # ティアに claudecode:<model> を割当てた場合のみ使用。bridge 未起動なら UI に選択肢が出ない。
    claude_code_bridge_url: str = "http://host.docker.internal:8010"

    # --- システム ---
    log_level: str = "INFO"
    timezone: str = "Asia/Tokyo"

    @property
    def discord_webhooks(self) -> dict[DiscordChannel, str]:
        """built-in チャンネルの channel id → webhook URL (.env 読込済の静的値)。

        C1 以降、投稿経路の webhook 解決は ``channel_registry.resolve_webhooks(
        config.discord_webhooks)`` がレジストリ (DB 正) 駆動で行う。この property は
        built-in の env 値ソースとして resolve_webhooks に渡す入力になった。
        fallback (japan_watch→alert 等) もレジストリの fallback_map() が SSoT。
        """
        return {
            "alert": self.discord_webhook_alert,
            "brief": self.discord_webhook_brief,
            "watch": self.discord_webhook_watch,
            "ops": self.discord_webhook_ops,
            "japan_watch": self.discord_webhook_japan_watch,
        }


# ---------- PipelineConfig (config/pipelines.yaml) ----------


class ScraperEntry(BaseModel):
    """Phase 5T-D: web_scraper_cluster の構成要素。

    cluster に含まれる個別 watcher の名前と有効/無効フラグ。
    yaml 例: ``- {name: project-zero, enabled: true}``
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    enabled: bool = True


class SourceConfig(BaseModel):
    """情報源 (RSS / Grok メール / web scraper) の設定。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: SourceType
    # 直接 RSS 用
    feeds: list[str] = Field(default_factory=list)
    # 1 回の取得で扱う最大記事数
    max_articles: int = Field(default=20, ge=1, le=200)

    # ---- Phase 2: Grok email source ----
    # 通知メールの差出人 / 件名フィルタ (ANY マッチ)
    grok_sender_filters: list[str] = Field(default_factory=list)
    grok_subject_filters: list[str] = Field(default_factory=list)
    # 取り込む経過時間 (分). None なら未読すべて
    grok_lookback_minutes: int | None = None
    # True (既定) なら UNSEEN のみ。False で既読も取り込む (テスト・初回キャッチアップ用)
    grok_unseen_only: bool = True
    # True (既定) なら Grok レポート抽出に成功したメールを処理後に既読化する
    # (インボックス肥大防止)。抽出失敗メールは未読のまま残し再試行可能にする。
    grok_mark_as_read: bool = True
    # メール本文から取り出した URL のホスト allowlist (boilerplate 除外)
    # default は grok.com 系のみ。サブドメインは自動許可
    grok_url_host_allowlist: list[str] = Field(default_factory=lambda: ["grok.com"])

    # ---- Phase 5R: web_scraper source ----
    # src/watchers/<scraper_name>.py を呼ぶ。現状: "project-zero" / "lac-watch"
    scraper_name: str | None = None

    # ---- direct RSS source (自前 fetcher、config/sources/feeds.yaml 駆動) ----
    # config/sources/feeds.yaml の path (None なら default config/sources/feeds.yaml)
    feeds_config_path: str | None = None
    rss_http_timeout: float | None = None  # default 30.0 sec
    rss_per_feed_limit: int | None = None  # default 50 entries/feed
    # 未見でもこれより古い published は取り込まない (P0 収集飢餓修正の鮮度ガード)
    rss_unseen_max_age_days: int | None = None  # default 14 days

    # ---- Phase 5T-C: web_scraper_cluster source ----
    # 複数 watcher を 1 pipeline で並列実行する集約 source。
    # Phase 5T-D: scrapers として {name, enabled} オブジェクトのリストに変更。
    # 個別 watcher の on/off を yaml レベルで切替可能。
    scrapers: list[ScraperEntry] = Field(default_factory=list)
    max_articles_per_scraper: int = Field(default=8, ge=1, le=50)

    # ---- Phase 5T-J: digest source ----
    # E1 (digest_research) / F1 (digest_weekly_recap) で参照する。
    # lookback_hours: DB から振り返る期間 (E1=24, F1=168)。
    # max_items_in_digest: digest 内に含める article 数の上限。
    digest_lookback_hours: int = Field(default=24, ge=1, le=720)
    digest_max_items: int = Field(default=10, ge=1, le=50)

    # ---- Phase 3 Synthesis (Phase 3+): status_synthesis source ----
    # 生成対象の period_type 集合。未指定なら ("weekly", "monthly")。
    # daily-status-synthesis pipeline では ("daily",) を指定して 24h window 用に絞る。
    synthesis_periods: tuple[str, ...] = Field(default_factory=tuple)


class ProcessorConfig(BaseModel):
    """本文抽出と LLM 処理の設定。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    extract_method: ExtractMethod = "trafilatura"
    extract_min_length: int = Field(default=200, ge=0)
    # summarize_model は削除済 (Phase 5H): 実 LLM モデルは能力ティア (model_tiers.py) で決定
    # (per-article 要約 = fast ティア)。yaml から指定する経路は未実装だった
    target_language: str = "ja"
    # Phase 3.1: 軽量 LLM による事前 triage (重要度判定 → フィルタ)
    # 100 件規模の fetch を高速に絞り込むため、本要約の前に title+概要だけで
    # importance を判定し、threshold 以上の記事のみフル要約に進める。
    triage_enabled: bool = False
    triage_keep_importance: tuple[str, ...] = ("high", "medium")
    triage_max_keep: int = Field(default=20, ge=1, le=200)
    # Phase 3 (収集の深掘り): thin feed (RSS が title + 短い description しか返さない feed)
    # は triage が薄い snippet だけで判定し、重要記事を誤って低評価しがち。triage 前に
    # 本文を先行抽出 (trafilatura、LLM 不要) して判定材料を厚くする。survivor は body_text を
    # 持ち越して本処理での再抽出を回避する。triage_enabled が前提 (triage しないなら無意味)。
    thin_feed_triage_enabled: bool = True
    # stripped summary_html がこの文字数未満なら thin と判定し先行抽出する。
    thin_feed_min_chars: int = Field(default=600, ge=0, le=100000)
    # Phase 3.1 拡張: 推論モード (thinking) の有効/無効
    # - False (既定): MoE / Dense ともに高速生成。要約や triage 等の圧縮タスク向け
    # - True: thinking モード ON。深い推論を要する複雑なタスク向け、ただし遅い
    # 適用先: direct-rss-fetch summarize、article triage の LLM 呼び出し。
    think_enabled: bool = False
    # Phase 5L-2: 意味的重複排除を 2 段階に分離。
    # - hard (再投稿防止): URL 違いの "ほぼ同一記事" を弾く高い閾値 + 長窓
    # - cluster (同事象クラスタ): 別ソースで同事象を扱う記事を統合する低い閾値 + 短窓
    # 既存 1 段階の similarity_threshold/dedup_window_hours は廃止し、両方の判定で
    # ヒットすれば重複扱いとする (どちらかでヒットすれば dedup される)。
    similarity_threshold_hard: float = Field(default=0.92, ge=0.0, le=1.0)
    similarity_threshold_cluster: float = Field(default=0.78, ge=0.0, le=1.0)
    # 比較対象の時間ウィンドウ (時間、0 で無制限)。
    # hard は長期 (7 日) で再投稿防止、cluster は短期 (48h) で続報の取りこぼし防止。
    dedup_window_hours_hard: int = Field(default=168, ge=0, le=8760)
    dedup_window_hours_cluster: int = Field(default=48, ge=0, le=8760)


# Phase 5H: SinkConfig は削除済。配信先は metadata['target_channel'] (Grok 経路の
# yaml SSoT) と ChannelRouting.importance_map (RSS 経路の重要度マップ) で決まる。
# pipeline.sink フィールドは src/ から参照されていなかったため、構造ごと撤去した。


class PipelineSchedule(BaseModel):
    """個別パイプラインのスケジュール定義 (Phase 2.7 + 3.1 拡張)。

    実行モード (排他):
      - **cron**: 毎日 ``hour:minute`` (TZ=Asia/Tokyo) に 1 回実行 (既定)
      - **interval**: ``interval_minutes`` ごとに繰り返し実行
        (RSS のような逐次取得の準リアルタイム配信向け)

    ``interval_minutes`` が指定されると interval モード優先。
    ``enabled=False`` なら scheduler に登録されない。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = True
    hour: int = Field(default=6, ge=0, le=23)
    minute: int = Field(default=30, ge=0, le=59)
    # Phase 3.1: 定期実行 (1 日 1 回ではなく N 分おき)
    interval_minutes: int | None = Field(default=None, ge=1, le=1440)
    # interval モードの clock-aligned 起点からのオフセット (分)。
    # 複数の interval pipeline が同時刻 (:00) に走る in-cycle race を避けるため、
    # 後発を数分ずらす用途 (例: scrapers を rss の 5 分後に)。0 = オフセットなし。
    interval_offset_minutes: int = Field(default=0, ge=0, le=59)
    # Phase 5T-J: 週次 / 平日のみ 等の cron day_of_week (APScheduler 互換)
    # 例: "sun" (日曜のみ), "mon-fri" (平日), "0-6" (毎日), "mon,wed,fri"
    # None / 空文字 → 毎日 (既存挙動)
    day_of_week: str | None = Field(default=None, max_length=32)
    # 段3 (真の月次): 月単位 cron 用 day-of-month (APScheduler CronTrigger day 互換)。
    # 例: "last" (月末), "1" (1 日)。None → 日次/週次 (既存挙動)。月次 synthesis 等で使う。
    day: str | None = Field(default=None, max_length=16)

    @property
    def is_interval(self) -> bool:
        return self.interval_minutes is not None

    @property
    def is_weekly_or_dow(self) -> bool:
        """day_of_week が指定されているか (cron 用)。"""
        return bool(self.day_of_week)


class PipelineConfig(BaseModel):
    """1 本のパイプライン定義 (source -> processor)。

    Phase 5H: sink フィールドは削除。配信先は ChannelRouting (yaml) と
    metadata['target_channel'] (Grok yaml SSoT) で決まる。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    source: SourceConfig
    processor: ProcessorConfig
    # Phase 2.7: 各 pipeline 単位の schedule。省略時は無効 (scheduler 未登録)。
    schedule: PipelineSchedule | None = None


# ---------- ChannelRouting (config/pipelines.yaml: channel_routing) ----------


# triage が付与する重要度レベルの正規集合 (SSoT)。表示 vocab (src/vocab) の canonical と
# articles_feed._ALLOWED_IMPORTANCE が参照する。"unknown" は triage 未実施の sentinel で
# ここには含めない (triage の出力ではないため)。
IMPORTANCE_LEVELS: tuple[str, ...] = ("high", "medium", "low")


def _default_importance_map() -> dict[Literal["high", "medium", "low"], DiscordChannel]:
    """ChannelRouting.importance_map の既定値生成 (mypy strict 対応)。"""
    return {
        "high": "alert",
        "medium": "brief",
        "low": "watch",
    }


def _ensure_known_channel(value: str, *, field: str) -> str:
    """channel 値をレジストリ既知 id に対して検証する (旧 Literal 検証の代替)。

    C1 で DiscordChannel を str に緩和したため、yaml の silent typo
    (例 'breif') が「publisher 不在 → 静かな投稿失敗」になるのを parse 時点で防ぐ。
    """
    from src.tools.channel_registry import known_channel_ids

    known = known_channel_ids()
    if value not in known:
        raise ValueError(f"{field}: 未知の Discord チャンネル '{value}' (可: {sorted(known)})")
    return value


# ---------- LlmEnrichment (Phase 5D: LLM 補助機能) ----------


class LlmEnrichment(BaseModel):
    """Grok セクションに LLM で付与する補助メタの ON/OFF 設定 (Phase 5D)。

    各機能は独立に切り替え可能。LLM 失敗時は機能を無視して既存挙動にフォールバック。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = True
    # L1: 各事象 heading にサブタイトルを生成
    generate_incident_heading: bool = True
    # L2: section の 1 文 TL;DR を生成
    generate_tldr: bool = True
    # L6: 各事象に 1 行アクション提示を生成
    generate_followup_action: bool = True
    # L3: section の関心度 0-10 を採点
    score_relevance: bool = True
    # L4: regex で IOC が拾えない事象に LLM 補助で IOC を抽出
    extract_iocs_with_context: bool = True
    # L5: cross-task incident の意味的重複を embedding で検知
    semantic_dedup_across_briefings: bool = True
    # Phase 5T-N: 抽出された IoC を LLM で再 verify して context-aware に
    # version 番号 / library 名 / git commit hash 等の誤抽出を drop
    verify_iocs_with_llm: bool = True
    # 並列実行の同時呼び出し上限 (Ollama サーバ負荷とのバランス)
    concurrency: int = Field(default=5, ge=1, le=20)


# EmptySectionPolicy (旧 Grok markdown 経路の「該当なし」セクション制御、Phase 5F) は
# Grok JSONL 化に伴い 2026-06-13 撤去。


class StixAttachPolicy(BaseModel):
    """STIX bundle 添付の許可条件 (Phase 5F / 問題 4)。

    モード:
        - ``off``: 一切添付しない
        - ``lenient``: IOC 1+ OR actor 1+ OR technique 1+ (Phase 5D 以前の挙動)
        - ``strict`` (既定): 具体的 IOC (CVE/IP/domain/hash) ≥ ``min_concrete_iocs``
            OR (``require_actor_and_technique=True`` のとき actor + technique 両方)
            「該当なし」briefing (metadata.grok_section_empty=True) は無条件で skip
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["off", "lenient", "strict"] = "strict"
    min_concrete_iocs: int = Field(default=1, ge=1)
    require_actor_and_technique: bool = True


class SourceQualityConfig(BaseModel):
    """Phase 5T-V: source-level quality signal の YAML スキーマ。

    config/sources/source_quality.yaml の内容を表す。Web UI から編集可能。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    # brief ch の 24h 上限。0 以下なら cap 無効
    brief_cap_24h: int = 15
    # Phase B-cal R3.5: importance=high の場合に style 降格より優先して brief に
    # 上げる「実 actionable な脅威」カテゴリ。Web UI から調整可能。
    high_threat_brief_categories: tuple[str, ...] = (
        "apt",
        "apt_leak",
        "vulnerability",
        "malware",
        "incident",
        "breach",
        "advisory",
    )


DEFAULT_SOURCE_QUALITY_PATH = Path("config/sources/source_quality.yaml")


class ChannelRouting(BaseModel):
    """重要度 → Discord チャンネル のマッピング (Phase 5C)。

    RSS 経路など「重要度しか持たないメッセージ」のルーティングに使う。
    Grok 経路は ``BriefingMessage.metadata['target_channel']`` が優先される。

    yaml 例 (``config/pipelines.yaml`` トップレベル)::

        channel_routing:
          importance_map:
            high: priority
            medium: daily
            low: research
          system_notify_enabled: true
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    importance_map: dict[Literal["high", "medium", "low"], DiscordChannel] = Field(
        default_factory=lambda: _default_importance_map(),
    )
    # system チャンネルへのパイプライン稼働状態通知 (成功 1 行 / 失敗 @here)
    system_notify_enabled: bool = True

    @field_validator("importance_map")
    @classmethod
    def _check_importance_map_channels(
        cls,
        v: dict[Literal["high", "medium", "low"], DiscordChannel],
    ) -> dict[Literal["high", "medium", "low"], DiscordChannel]:
        for importance, channel in v.items():
            _ensure_known_channel(channel, field=f"importance_map.{importance}")
        return v


# ---------- 公開関数 ----------

DEFAULT_PIPELINES_PATH = Path("config/pipelines.yaml")


def load_app_config() -> AppConfig:
    """``.env`` および環境変数から ``AppConfig`` をロードする。"""
    # 必須フィールドは ``.env`` / 環境変数から読み取られるため、引数なしで OK。
    # mypy は静的に env を解決できないので call-arg を抑制する。
    return AppConfig()


def _load_yaml(path: Path) -> object:
    if not path.exists():
        raise FileNotFoundError(f"設定ファイルが見つかりません: {path.resolve()}")
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ValueError(f"{path} の YAML パースに失敗: {e}") from e


def load_pipelines(path: str | Path = DEFAULT_PIPELINES_PATH) -> list[PipelineConfig]:
    """``config/pipelines.yaml`` から ``PipelineConfig`` のリストをロードする。"""
    p = Path(path)
    raw = _load_yaml(p)
    if not isinstance(raw, dict) or "pipelines" not in raw:
        kind = (
            f"トップレベルのキー: {list(raw.keys())}"
            if isinstance(raw, dict)
            else f"型: {type(raw).__name__}"
        )
        raise ValueError(f"{p} のトップレベルに `pipelines` キーが必要です ({kind})")
    pipelines_raw = raw["pipelines"]
    if not isinstance(pipelines_raw, list):
        raise ValueError(
            f"{p} の `pipelines` はリストである必要があります "
            f"(現在: {type(pipelines_raw).__name__})",
        )
    try:
        return [PipelineConfig(**item) for item in pipelines_raw]
    except ValidationError as e:
        raise ValueError(f"{p} のスキーマが不正です:\n{e}") from e


def load_source_quality(
    path: str | Path = DEFAULT_SOURCE_QUALITY_PATH,
) -> SourceQualityConfig:
    """Phase 5T-V: source_quality.yaml をロードする。

    ファイルが存在しない場合はデフォルト (cap 無効、blocklist 空) を返す。
    """
    p = Path(path)
    if not p.exists():
        return SourceQualityConfig()
    raw = _load_yaml(p)
    if not isinstance(raw, dict):
        return SourceQualityConfig()
    try:
        return SourceQualityConfig(**raw)
    except ValidationError as e:
        raise ValueError(f"{p} の source_quality スキーマが不正です:\n{e}") from e


# brief routing UI (ブリーフ設定) の checklist 用カテゴリ語彙。triage 出力の SSoT は
# prompts/briefing/summarizer.j2。geopolitical/research/other は非脅威系。
KNOWN_ARTICLE_CATEGORIES: tuple[str, ...] = (
    "apt",
    "apt_leak",
    "vulnerability",
    "malware",
    "incident",
    "breach",
    "advisory",
    "policy",
    "phishing",
    "research",
    "geopolitical",
    "other",
)

# UI 保存時に保持する doc コメント (code-owned header → Phase B-cal の経緯を失わない)。
_SOURCE_QUALITY_HEADER = """\
# source-level quality signal (Phase 5T-V → Phase B-cal で content-based 化)。
# **このファイルは Web UI (設定 → ブリーフ設定) の構造化フォームから編集する。**
# brief_cap_24h: brief ch の 24h 投稿上限 (importance=high は cap 無視)。
# high_threat_brief_categories: importance=high のうち style 降格より優先して brief へ上げる
#   「実 actionable な脅威」カテゴリ (Phase B-cal R3.5)。
"""


def load_channel_routing(
    path: str | Path = DEFAULT_PIPELINES_PATH,
) -> ChannelRouting:
    """``config/pipelines.yaml`` のトップレベル ``channel_routing`` をロードする。

    キーが無ければデフォルト (high→priority / medium→daily / low→research,
    system_notify_enabled=True) を返す。
    """
    p = Path(path)
    raw = _load_yaml(p)
    if not isinstance(raw, dict):
        return ChannelRouting()
    routing_raw = raw.get("channel_routing")
    if routing_raw is None:
        return ChannelRouting()
    if not isinstance(routing_raw, dict):
        raise ValueError(
            f"{p} の `channel_routing` は dict である必要があります "
            f"(現在: {type(routing_raw).__name__})",
        )
    try:
        return ChannelRouting(**routing_raw)
    except ValidationError as e:
        raise ValueError(f"{p} の channel_routing スキーマが不正です:\n{e}") from e


def load_stix_attach_policy(
    path: str | Path = DEFAULT_PIPELINES_PATH,
) -> StixAttachPolicy:
    """``config/pipelines.yaml`` のトップレベル ``stix_attach_policy`` をロードする。"""
    p = Path(path)
    raw = _load_yaml(p)
    if not isinstance(raw, dict):
        return StixAttachPolicy()
    policy_raw = raw.get("stix_attach_policy")
    if policy_raw is None:
        return StixAttachPolicy()
    if not isinstance(policy_raw, dict):
        raise ValueError(
            f"{p} の `stix_attach_policy` は dict である必要があります "
            f"(現在: {type(policy_raw).__name__})",
        )
    try:
        return StixAttachPolicy(**policy_raw)
    except ValidationError as e:
        raise ValueError(f"{p} の stix_attach_policy スキーマが不正です:\n{e}") from e


def load_llm_enrichment(
    path: str | Path = DEFAULT_PIPELINES_PATH,
) -> LlmEnrichment:
    """``config/pipelines.yaml`` のトップレベル ``llm_enrichment`` をロードする (Phase 5D)。

    キーが無ければデフォルト (全機能有効、concurrency=5) を返す。
    """
    p = Path(path)
    raw = _load_yaml(p)
    if not isinstance(raw, dict):
        return LlmEnrichment()
    enrich_raw = raw.get("llm_enrichment")
    if enrich_raw is None:
        return LlmEnrichment()
    if not isinstance(enrich_raw, dict):
        raise ValueError(
            f"{p} の `llm_enrichment` は dict である必要があります "
            f"(現在: {type(enrich_raw).__name__})",
        )
    try:
        return LlmEnrichment(**enrich_raw)
    except ValidationError as e:
        raise ValueError(f"{p} の llm_enrichment スキーマが不正です:\n{e}") from e
