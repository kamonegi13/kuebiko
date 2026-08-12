"""Pydantic レコードモデルと永続化まわりの定数 (run_history 分割の一部)。

元は ``src/storage/run_history.py`` の単一巨大モジュールに同居していたが、
800 行上限のためレコード定義と定数をここに切り出した。公開シンボルは
``src.storage.run_history`` から従来通り re-export されるため import 互換。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

DEFAULT_DB_PATH = Path("data/run_history.db")

# 1 行あたりの最大バイト数 (これを超えたら末尾を切り詰めてサフィックスを付ける)
MAX_LOG_LINE_BYTES = 8 * 1024
LOG_TRUNCATE_SUFFIX = "... [truncated]"

# 1 run あたりの最大行数。超過後は line を破棄し log_truncated=1 を立てる
MAX_LOG_LINES_PER_RUN = 5000

# 既定の log retention (日)
DEFAULT_LOG_RETENTION_DAYS = 30


# ---------- Models ----------


RunStatus = Literal["running", "succeeded", "partial_failure", "failed"]
ArticleStatus = Literal[
    "summarized",
    "posted",
    "marked_read",
    "extract_failed",
    "summarize_failed",
    "post_failed",
    # Phase 5L-8: cross-channel dedup_key 一致で skip された article。
    # main.py の cross_channel_dedup_skipped イベントと対応し、観測性のため
    # articles テーブルに永続化される (Phase 5L-4 で漏れていた)。
    "skipped_duplicate",
    # 被害状況コレクタ: Discord 投稿はしないが地図/Intel 用に DB へ取り込んだ構造化被害者
    # (ransomware.live global 等)。地図クエリのみ status IN ('posted','collected') で拾う。
    "collected",
    # collected のうち、同一被害組織を扱うニュース記事が既に存在する重複 (ニュースを正とする)。
    # 取り込みは保持しつつ地図/Discord から除外 (status IN ('posted','collected') に含めない)。
    "collected_duplicate",
]
LogStream = Literal["stdout", "stderr", "system"]
# パイプライン実行のきっかけ (表示 vocab `trigger` の canonical)。reactive/recovery は
# 表示上の追加種別で RunRecord には現れないため vocab 側で別途扱う (docs §2.7)。
TriggerSource = Literal["scheduler", "manual", "cli"]


class RunRecord(BaseModel):
    """1 回のパイプライン実行のメタデータ。"""

    model_config = ConfigDict(frozen=True, extra="ignore")

    id: int | None = None
    started_at: datetime
    finished_at: datetime | None = None
    pipeline: str
    dry_run: bool
    triggered_by: TriggerSource = "scheduler"
    status: RunStatus = "running"
    total_fetched: int = 0
    summarized: int = 0
    posted: int = 0
    marked_read: int = 0
    error_count: int = 0
    note: str | None = None
    log_line_count: int = 0
    log_truncated: bool = False
    # Phase 5P: triage 失敗 (LLM 障害で medium フォールバック) の件数。
    # > 0 のとき ops チャンネルへ即時通知し silent 失敗を表面化する。
    triage_error_count: int = 0
    # Phase 5P: 取得が中途エラーで打ち切られたか + 取得済み件数。
    # mid-paging の 429/5xx で fetch が破棄されたケースを可視化する。
    partial_fetch: bool = False
    partial_fetch_count: int = 0


class ArticleRecord(BaseModel):
    """1 記事の処理結果。"""

    model_config = ConfigDict(frozen=True, extra="ignore")

    id: int | None = None
    run_id: int
    article_id: str
    title: str
    url: str
    feed_title: str | None = None
    # 安定 source キー (= ManagedSource.url)。表示名 feed_title と分離した結合キー。
    feed_url: str | None = None
    importance: str | None = None
    category: str | None = None
    status: ArticleStatus
    failure_reason: str | None = None
    posted_channel: str | None = None
    duration_seconds: float | None = None
    # Phase 5L-4: 同事象クラスタリング用キー (None なら未設定)
    dedup_key: str | None = None
    # Phase 5T-K: Discord 投稿の (message_id, channel_id) — digest link 生成用
    discord_message_id: str | None = None
    discord_channel_id: str | None = None
    # Phase 5T-P: BriefingMessage.summary を永続化して digest 生成に活用
    summary: str | None = None
    # Phase H: PMESII-PT 8 軸 (multi-label、boolean field 8 個)
    pmesii_p: bool = False
    pmesii_m: bool = False
    pmesii_e: bool = False
    pmesii_s: bool = False
    pmesii_i_infra: bool = False
    pmesii_i_cyber: bool = False
    pmesii_p_env: bool = False
    pmesii_t: bool = False
    # Phase H: Diamond Model victim vertex (canonical + raw 二重持ち)
    victim_sector_canonical: str | None = None
    victim_sector_raw: str | None = None
    victim_country_iso: str | None = None
    victim_country_raw: str | None = None
    # 被害国スコープ (監査 2026-08-01 ⑥): ISO2 に解決できない "global"/"EU"/複数国
    # 列挙の受け皿 ("global" | "regional" | "multi" | None)。iso は単一国の意味を保つ。
    victim_country_scope: str | None = None
    # ランサム識別フラグ (category と直交)。ransomware.live 由来 / 攻撃者が ransom_group で true。
    is_ransomware: bool = False
    # Phase Diamond-Axes: Diamond Model meta-feature 軸。
    # socio_political_intent = Adversary⇄Victim の意図 (closed enum、None=未判定/unknown)、
    # technical_axis_summary = Capability⇄Infrastructure の技術的結線 (短い narrative)。
    socio_political_intent: str | None = None
    # intent の LLM 自己評価確度 (high/medium/low、None=未判定)
    intent_confidence: str | None = None
    # 主題アクター層 (2026-07-17、docs/subject_actor_attribution_design.md):
    # 言及 (article_entities actor) と分離した「記事の主語」。PIR 照合の主題ゲート用。
    # subject_actor_ids = comma 連結の辞書 id ('' = 評価済み・主題なし)
    # subject_actor_source = 'title' | 'llm' | 'none' (None = 未評価 → legacy 照合)
    subject_actor_ids: str | None = None
    subject_actor_source: str | None = None
    subject_actor_confidence: str | None = None
    # アクター辞書 D1 (2026-07-26): 主題判定 LLM 層の生入力 (summarizer の
    # routing_flags.primary_actor_id / confidence、辞書解決前の生値)。判定を
    # 「入力からの再導出可能な射影」にするための一級観測 — 新アクター承認時に
    # 全期間の LLM 出力へ遡及帰属できる (docs/actor_observed_history_design.md)。
    llm_primary_actor_raw: str | None = None
    llm_primary_confidence: str | None = None
    # 主題判定の根拠文 (2026-08-13 可視化)。特に subject_actor_source='none' の理由
    # (候補アクターは背景言及、等) を記事詳細 UI に表示する。
    subject_actor_rationale: str | None = None
    # P4: 本文に明示された対処 (パッチ/回避策) の 1 文。None=記載なし
    remediation: str | None = None
    socio_political_rationale: str | None = None
    technical_axis_summary: str | None = None
    # Phase B-R5b 観察: LLM が判定した editorial_stance (None=未判定)
    editorial_stance: str | None = None
    # flow Phase 3: 投稿先決定の監査情報。「なぜこのチャンネルか」を記事単位で説明する。
    # routing_rule_id = マッチした routing rule の ID、routing_reason = 人向け短文。
    # rule 定義は DB 版管理で変わりうるため、判定時点の snapshot として両方を永続化する
    # (rule_id から後から reason を再導出すると過去の説明が崩れる)。
    # route() を経由しない経路 (grok metadata 直指定等) では None。
    routing_rule_id: str | None = None
    routing_reason: str | None = None
    # 記事の公開時刻 (RSS pubDate 等)。created_at(取得/処理時刻) と区別して表示に使う。
    # 既存行 / 公開時刻不明は None → 表示側で created_at にフォールバック。
    published_at: datetime | None = None
    # 時間軸レイヤ b/c (2026-06-27): 事象の実発生(または検知/公表)日 (YYYY-MM-DD)。
    # published_at(報道時刻) と分離。明示時のみ、推測しない。basis = date が何を指すか。
    # compromise_date = 初期侵害開始日 → dwell = event_date - compromise_date。
    event_date: str | None = None
    event_date_basis: str | None = None
    compromise_date: str | None = None
    # 本文完全性 (2026-07-27, docs/body_extraction_and_entity_integrity_redesign.md):
    # body_source = body の由来 (full_extract/playwright_extract/prefetch/scraper/grok/
    # feed_summary/none)。extraction_failure_reason = 全文取得失敗時のみ (feed 抜粋 fallback の
    # 原因)。全文失敗の無音 fallback を可視化し、切り株再取得の対象判定に使う。
    body_source: str | None = None
    extraction_failure_reason: str | None = None
    # 記事タイプ (breaking/advisory/recap/tutorial/research/press/opinion)。judgment_classifier
    # 由来 (None=未分類/旧行)。記事詳細で category/stance と別軸として表示する。
    article_type: str | None = None
    # body_source 状態機械化 (2026-07-29, docs/body_source_state_machine_design.md B1):
    # 再取得試行回数。N 回失敗で blocked 昇格 (B1 は列のみ・消費は B3)。DEFAULT 0 NOT NULL。
    refetch_attempts: int = 0
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class LogLine(BaseModel):
    """1 行のライブログ。SSE で配信する単位でもある。"""

    model_config = ConfigDict(frozen=True, extra="ignore")

    run_id: int
    seq: int
    ts: datetime
    stream: LogStream = "stdout"
    line: str


class StatusSynthesisRecord(BaseModel):
    """Phase 3 (Synthesis): 期間ごとの状況総括レコード。

    PMESII-PT 軸を関係性で総括した narrative + 軸別 evidence を含む。
    再生成時は (period_type, period_start) で UPSERT。
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    id: int | None = None
    period_type: str  # 'weekly' / 'monthly'
    period_start: datetime
    period_end: datetime
    headline: str
    weight_section: str
    chain_section: str
    cog_section: str
    spillover_section: str
    pir_section: str
    axes_evidence: str  # JSON serialized
    # S2 (analytic tradecraft, ICD 203): 主見立て + 対立仮説 + 前提 + 覆る指標。JSON 文字列。
    tradecraft: str = ""
    article_count: int = 0
    llm_model: str | None = None
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    # Phase 2 K6: Discord (brief) 配信済時刻。None=未配信 (再配信 dedup の判定に使う)。
    posted_at: datetime | None = None


class TaxonomyProposalRecord(BaseModel):
    """Phase H: taxonomy review pipeline が生成する 1 件の提案。

    UI で user が accept/reject/defer する単位。Tier 1 (typo) は
    複数まとめて 1-click 承認、Tier 2/3 は個別 review。
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    id: int | None = None
    run_id: int | None = None
    proposal_type: str  # 'pattern_1'..'pattern_8'
    tier: str  # 'tier_1_auto' / 'tier_2_review' / 'tier_3_strategic'
    target_yaml: str  # 'victim_sectors' / 'countries' / 'actor_aliases' / 'pmesii_default_mapping'
    target_canonical: str | None = None
    proposed_change: str  # JSON: { "kind": "add_alias", "alias": "...", ... }
    rationale: str
    confidence: str  # 'high' / 'medium' / 'low'
    evidence_count: int = 0
    evidence_ids: str | None = None  # JSON array
    status: str = "pending"  # 'pending' / 'accepted' / 'rejected' / 'deferred' / 'expired'
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    reviewed_at: datetime | None = None
    reviewed_by: str = "manual"


class ActorUpdateProposalRecord(BaseModel):
    """Actors Stage 4: MITRE 同期が生成するレビュー提案 1 件。

    新規 actor 追加 (nation 推定の妥当性確認) / alias 衝突 (帰属の付け替え判断) のみ。
    安全な追加系差分はレビューを経ず自動適用されるためここには入らない。
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    id: int | None = None
    run_id: int | None = None
    proposal_type: str  # 'mitre_new_actor' / 'mitre_alias_conflict'
    mitre_group: str
    dedup_key: str
    actor_id: str | None = None
    payload: str  # JSON
    rationale: str
    status: str = "pending"  # 'pending' / 'accepted' / 'rejected'
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    decided_at: datetime | None = None


class F1SelectionRecord(BaseModel):
    """Phase 5T-T1: F1 (weekly deep dive) が選定した 1 article の履歴。

    novelty 判定 (過去 N 時間以内に同 dedup_key を選定したか) と、
    将来の feedback loop (T4 以降) で composite score の妥当性検証に使う。
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    id: int | None = None
    run_id: int
    article_id: str
    dedup_key: str | None = None
    composite_score: float
    pir: float
    roi: float
    timeliness: float
    novelty: float
    selected_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ArticleNoteRecord(BaseModel):
    """Phase 5 (学習・記憶): 1 article への個人 memo / bookmark / tag / judgment。

    1 article = 1 行 (article_id で upsert)。アナリストの判断・気づきを蓄積し、
    将来の過去参照 (Phase 6) / 学習の基盤にする。
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    article_id: str
    bookmarked: bool = False
    note: str = ""
    tags: list[str] = Field(default_factory=list)
    judgment: str = ""  # アナリストの所見 (例: 誤検知 / 重要先例 / 要追跡)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class SourceFetchHealth(BaseModel):
    """監査 2026-07-05 P2: 1 feed の直近 fetch 健全性 (feed 死活検知用)。"""

    model_config = ConfigDict(frozen=True, extra="ignore")

    source_key: str  # feed URL (名前より安定)
    name: str
    last_ok_at: datetime | None = None
    last_error_at: datetime | None = None
    last_error: str | None = None
    consecutive_failures: int = 0
    last_article_count: int = 0
    updated_at: datetime | None = None
