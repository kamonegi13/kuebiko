"""古典 ML 蒸留ヘッドのシャドー推論記録 (head_shadow テーブル)。

較正格子 ロードマップ C (docs/self_evolving_tuning_design.md、2026-08-22): ヘッドの
予測を同時点の triage LLM 判定と突き合わせて記録する。**シャドー専用テーブル —
本番パイプラインは読まない** (panel_verdicts §10.1 と同じ「観測」原則。ヘッド予測は
本番配信の重要度・カテゴリ決定に一切影響しない)。

url_hash (記事の dedup url_hash) を冪等キーにして再実行の重複挿入を遮断する。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from src.storage.repo_base import RunHistoryRepositoryBase


@dataclass(frozen=True)
class HeadShadowRecord:
    """1 記事に対するヘッド予測と triage 判定の突合 1 行分。

    ``created_at`` は含めない (DB 既定 = 挿入時刻に任せる)。
    """

    url_hash: str
    article_id: str
    head_importance: str
    head_importance_probs: str  # JSON: {"high":0.12,...} 較正確率
    triage_kept: bool
    artifact_version: str
    embedding_model: str
    run_id: str | None = None
    head_category: str | None = None
    head_category_prob: float | None = None
    triage_importance: str | None = None
    triage_error: bool = False
    disagree_cutoff: bool = False


@dataclass(frozen=True)
class HeadShadowStats:
    """直近 N 日の head_shadow 集計 (運用タブ表示用)。"""

    total: int
    agree_with_triage: int
    disagree_cutoff: int
    triage_error: int
    by_head_importance: dict[str, int]


class HeadShadowMixin(RunHistoryRepositoryBase):
    """head_shadow テーブルの読み書き。"""

    def record_head_shadow(self, records: Sequence[HeadShadowRecord]) -> int:
        """シャドー推論記録をバッチ挿入する。url_hash 衝突 (再実行) は無視 (冪等)。

        戻り値は挿入試行件数 (``len(records)``)。実際に受理された件数と衝突で
        スキップされた件数は区別しない — 呼び出し側は「再実行しても安全」だけ
        知ればよい (record_tuning_label 等と異なり、シャドー計測は個別行の
        成否を追跡する必要がないバッチ producer のため)。
        """
        if not records:
            return 0
        rows = [
            (
                r.url_hash,
                r.article_id,
                r.run_id,
                r.head_importance,
                r.head_importance_probs,
                r.head_category,
                r.head_category_prob,
                r.triage_importance,
                1 if r.triage_error else 0,
                1 if r.triage_kept else 0,
                1 if r.disagree_cutoff else 0,
                r.artifact_version,
                r.embedding_model,
            )
            for r in records
        ]
        with self._connect() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO head_shadow"
                " (url_hash, article_id, run_id, head_importance, head_importance_probs,"
                "  head_category, head_category_prob, triage_importance, triage_error,"
                "  triage_kept, disagree_cutoff, artifact_version, embedding_model)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        return len(rows)

    def head_shadow_stats(self, days: int = 7) -> HeadShadowStats:
        """直近 N 日の集計 (total / triage 完全一致 / disagree_cutoff / importance 別件数 / error)。

        ⚠ SQLite の保存値は ``datetime('now')`` 形式・PG は TIMESTAMPTZ のため、
        比較は ``datetime(created_at) >= datetime('now', ?)`` で正規化する
        (db_backend.translate_sql が PG では column 側の datetime() を剥がして
        TIMESTAMPTZ 直接比較に翻訳する)。
        """
        since = (f"-{int(days)} days",)
        with self._connect() as conn:
            totals = conn.execute(
                "SELECT"
                " COUNT(*) AS total,"
                " SUM(CASE WHEN head_importance = triage_importance THEN 1 ELSE 0 END) AS agree,"
                " SUM(CASE WHEN disagree_cutoff = 1 THEN 1 ELSE 0 END) AS disagree,"
                " SUM(CASE WHEN triage_error = 1 THEN 1 ELSE 0 END) AS errors"
                " FROM head_shadow WHERE datetime(created_at) >= datetime('now', ?)",
                since,
            ).fetchone()
            by_importance_rows = conn.execute(
                "SELECT head_importance, COUNT(*) AS n FROM head_shadow"
                " WHERE datetime(created_at) >= datetime('now', ?)"
                " GROUP BY head_importance",
                since,
            ).fetchall()
        by_importance = {str(r["head_importance"]): int(r["n"]) for r in by_importance_rows}
        return HeadShadowStats(
            total=int((totals["total"] if totals else 0) or 0),
            agree_with_triage=int((totals["agree"] if totals else 0) or 0),
            disagree_cutoff=int((totals["disagree"] if totals else 0) or 0),
            triage_error=int((totals["errors"] if totals else 0) or 0),
            by_head_importance=by_importance,
        )

    def purge_head_shadow(self, days: int = 180) -> int:
        """N 日より古いシャドー記録を削除する (retention、ops_notices と同水準の流儀)。"""
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM head_shadow WHERE datetime(created_at) < datetime('now', ?)",
                (f"-{int(days)} days",),
            )
            return int(cur.rowcount or 0)
