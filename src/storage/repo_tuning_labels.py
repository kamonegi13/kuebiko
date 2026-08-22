"""遅延正解ラベル (tuning_labels) と goldset 切替評価 (tuning_evals) の読み書き。

**tuning_labels は凍結資産**: 2026-08-22 の較正格子 (自己進化チューニング) 撤収に伴い
producer (週次収穫) を停止した。蓄積済みの 78 件は将来の評価母集団として残し、
読み取り API (運用タブ表示 / asset_export の日次退避) のみ維持する。撤収の経緯は
docs/self_evolving_tuning_design.md。

**tuning_evals は稼働中**: weekly-goldset-eval が rubric 版の切替評価を記録し続ける。

同居する主体補完の突合クエリ (fetch_feed_subject_claims /
fetch_victim_org_news_candidates) は src/cti/subject_backfill.py の決定論補完が使う。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from src.storage.repo_base import RunHistoryRepositoryBase
from src.storage.row_mappers import _to_iso


class TuningLabelsMixin(RunHistoryRepositoryBase):
    """tuning_labels テーブルの読み書き。"""

    def summarize_tuning_labels(self) -> list[dict[str, Any]]:
        """(field, source) ごとの現行/総件数と最終到着時刻 (運用タブの件数表示用)。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT field, source,"
                " COUNT(*) AS total,"
                " SUM(CASE WHEN superseded_by IS NULL THEN 1 ELSE 0 END) AS active,"
                " MAX(arrived_at) AS last_arrived_at"
                " FROM tuning_labels GROUP BY field, source"
                " ORDER BY field, source",
            ).fetchall()
        return [
            {
                "field": str(r["field"]),
                "source": str(r["source"]),
                "total": int(r["total"]),
                "active": int(r["active"] or 0),
                "last_arrived_at": str(r["last_arrived_at"]),
            }
            for r in rows
        ]

    def list_tuning_labels(
        self,
        *,
        field: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """ラベルを新しい順に返す (field でフィルタ可)。"""
        sql = (
            "SELECT id, dedup_key, article_id, field, label_value, source, strength,"
            " arrived_at, provenance, superseded_by FROM tuning_labels"
        )
        params: list[object] = []
        if field:
            sql += " WHERE field = ?"
            params.append(field)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            {
                "id": int(r["id"]),
                "dedup_key": str(r["dedup_key"]),
                "article_id": str(r["article_id"]) if r["article_id"] is not None else None,
                "field": str(r["field"]),
                "label_value": str(r["label_value"]),
                "source": str(r["source"]),
                "strength": str(r["strength"]),
                "arrived_at": str(r["arrived_at"]),
                "provenance": str(r["provenance"]),
                "superseded_by": (
                    int(r["superseded_by"]) if r["superseded_by"] is not None else None
                ),
            }
            for r in rows
        ]

    # ---------- 収穫 producer 用の読み出し (SQL は storage 層に集約) ----------

    def fetch_feed_subject_claims(self, since_iso: str) -> list[dict[str, Any]]:
        """犯行声明 (feed 帰属) 記事 × victim_org を返す (収穫①の突合元)。

        時刻錨は **発覚日** COALESCE(published_at, created_at) — 掲載日 (created_at) を
        錨にすると、数か月前の事件を遅れて掲載した声明が同一組織の別インシデント報道と
        誤結合する (2026-08-22 に LexisNexis で実発生: 発覚 05-01 の声明が 08-11 掲載され、
        08-10 の別事件報道へ誤ラベル)。reconcile の「実効日」と同じ既定。

        ``url`` は不変条件14 (§5 ソース独立性、2026-08-22) の突合元 host 判定用。
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT a.article_id, a.subject_actor_ids AS gt,"
                " LOWER(TRIM(e.value)) AS org,"
                " COALESCE(a.published_at, a.created_at) AS created_at,"
                " a.url"
                " FROM articles a JOIN article_entities e"
                "   ON e.article_id = a.article_id AND e.entity_type = 'victim_org'"
                " WHERE a.subject_actor_source = 'feed'"
                "   AND COALESCE(a.subject_actor_ids, '') <> ''"
                "   AND a.created_at >= ?",
                (since_iso,),
            ).fetchall()
        return [
            {
                "article_id": str(r["article_id"]),
                "gt": str(r["gt"]),
                "org": str(r["org"]),
                "created_at": r["created_at"],
                "url": str(r["url"] or ""),
            }
            for r in rows
        ]

    def fetch_victim_org_news_candidates(self, since_iso: str) -> list[dict[str, Any]]:
        """feed 帰属でないニュース記事 × victim_org を返す (収穫①の突合先)。

        除外: 散文 body を持たない構造化レコード / recap / **ダイジェスト** (多数事件の
        まとめには単一主題が存在しない — eval script の除外を移植し忘れて Grok ダイジェスト
        に誤ラベルが付いた 2026-08-22 の教訓。生 % は psycopg 罠 → パラメータで渡す)。
        title/body/category はラベルの学習テキスト凍結 (snapshot §13-3) 用。
        subject_actor_ids/subject_actor_source は既存主体の有無判定用 (subject backfill、
        2026-08-22 §13 対処 A — 既存の主体は上書きせず判定材料として持ち帰る)。
        ``url`` は不変条件14 (§5 ソース独立性、2026-08-22) の突合先 host 判定用。
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT a.article_id, LOWER(TRIM(e.value)) AS org,"
                " COALESCE(a.published_at, a.created_at) AS created_at,"
                " a.title, a.body, a.category, a.url,"
                " COALESCE(a.subject_actor_ids, '') AS subject_actor_ids,"
                " COALESCE(a.subject_actor_source, '') AS subject_actor_source"
                " FROM articles a JOIN article_entities e"
                "   ON e.article_id = a.article_id AND e.entity_type = 'victim_org'"
                " WHERE COALESCE(a.subject_actor_source, '') <> 'feed'"
                "   AND LENGTH(COALESCE(a.body, '')) >= 500"
                "   AND COALESCE(a.article_type, '') <> 'recap'"
                "   AND a.title NOT LIKE ?"
                "   AND a.created_at >= ?",
                ("%ダイジェスト%", since_iso),
            ).fetchall()
        return [
            {
                "article_id": str(r["article_id"]),
                "org": str(r["org"]),
                "created_at": r["created_at"],
                "title": str(r["title"] or ""),
                "body": str(r["body"] or ""),
                "category": str(r["category"] or ""),
                "url": str(r["url"] or ""),
                "subject_actor_ids": str(r["subject_actor_ids"]),
                "subject_actor_source": str(r["subject_actor_source"]),
            }
            for r in rows
        ]

    # ---------- 不変条件14 (§5 ソース独立性、2026-08-22): 自己突合の非破壊隔離 ----------

    def record_tuning_eval(
        self,
        *,
        prompt_id: str,
        kind: str,
        verdict: str,
        mode: str,
        detail: str,
        from_version: int | None = None,
        to_version: int | None = None,
        when: datetime | None = None,
    ) -> int:
        """評価・裁定を 1 行追記する (append-only)。detail は JSON、自由文は入れない。"""
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO tuning_evals"
                " (prompt_id, kind, from_version, to_version, verdict, mode, detail, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    prompt_id,
                    kind,
                    from_version,
                    to_version,
                    verdict,
                    mode,
                    detail,
                    _to_iso(when or datetime.now(UTC)),
                ),
            )
            assert cur.lastrowid is not None
            return int(cur.lastrowid)

    def find_tuning_eval(
        self,
        *,
        prompt_id: str,
        kind: str,
        to_version: int,
    ) -> dict[str, Any] | None:
        """同一 (prompt, kind, to_version) の既存裁定 (冪等性と flip-flop 防止の状態)。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, verdict, mode, created_at FROM tuning_evals"
                " WHERE prompt_id = ? AND kind = ? AND to_version = ?"
                " ORDER BY id DESC LIMIT 1",
                (prompt_id, kind, to_version),
            ).fetchone()
        if row is None:
            return None
        return {
            "id": int(row["id"]),
            "verdict": str(row["verdict"]),
            "mode": str(row["mode"]),
            "created_at": str(row["created_at"]),
        }

    def list_tuning_evals(self, limit: int = 20) -> list[dict[str, Any]]:
        """評価・裁定を新しい順に返す (運用タブの表示用)。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, prompt_id, kind, from_version, to_version, verdict, mode,"
                " detail, created_at FROM tuning_evals ORDER BY id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [
            {
                "id": int(r["id"]),
                "prompt_id": str(r["prompt_id"]),
                "kind": str(r["kind"]),
                "from_version": int(r["from_version"]) if r["from_version"] is not None else None,
                "to_version": int(r["to_version"]) if r["to_version"] is not None else None,
                "verdict": str(r["verdict"]),
                "mode": str(r["mode"]),
                "detail": str(r["detail"]),
                "created_at": str(r["created_at"]),
            }
            for r in rows
        ]

    def export_tuning_labels(self) -> list[dict[str, Any]]:
        """全ラベルの schema 非依存 dump (恒久資産エクスポート §13-3 用)。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, dedup_key, article_id, field, label_value, source, strength,"
                " arrived_at, provenance, superseded_by, snapshot FROM tuning_labels ORDER BY id",
            ).fetchall()
        return [dict(r) for r in rows]

