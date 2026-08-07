"""パイプライン実行結果モデル (src.main から分割)。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class PipelineRunResult(BaseModel):
    """1 回のパイプライン実行結果のサマリ。"""

    model_config = ConfigDict(frozen=True)

    total_fetched: int = 0
    skipped_dup: int = 0
    summarized: int = 0
    posted: int = 0
    marked_read: int = 0
    errors: list[str] = Field(default_factory=list)
    dry_run: bool = False
    # Phase 5P: triage 失敗 (LLM 障害で medium fail-open) の件数。
    triage_error_count: int = 0
    # Phase 5P: 取得が中途エラーで打ち切られたか + 取得済み件数。
    partial_fetch: bool = False
    partial_fetch_count: int = 0
    # 監査 backlog 2026-07-05: 子プロセス自身が ops へ稼働通知を投稿できたか。
    # 親 (PipelineRunner) は False のときのみ partial_failure / failed を通知する
    # (run_pipeline 経路の自前通知と親側通知の二重投稿を構造的に防ぐ)。
    ops_notified: bool = False
    # 2026-08-01: 時間予算 (soft deadline) で処理を打ち切り、次 run へ繰り越した記事数。
    # 繰越記事は既読化されないため次 run の RSS 窓で自然に再取得される (成果全損の防止)。
    deferred_count: int = 0
