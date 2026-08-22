"""taxonomy / MITRE / actor / F1 / maintenance / aggregations のメソッド (run_history 分割)。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from src.storage.records import (
    ActorUpdateProposalRecord,
    F1SelectionRecord,
    TaxonomyProposalRecord,
)
from src.storage.repo_base import RunHistoryRepositoryBase
from src.storage.row_mappers import (
    _from_iso,
    _row_to_actor_proposal,
    _row_to_daily_brief,
    _row_to_taxonomy_proposal,
    _to_iso,
)


class KnowledgeMixin(RunHistoryRepositoryBase):
    # ----- Phase H: taxonomy review proposals -----

    def insert_taxonomy_proposal(self, record: TaxonomyProposalRecord) -> int:
        """1 件の提案を挿入。merge 時は呼び出し側で update_or_insert を使う想定。"""
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO taxonomy_review_proposals
                  (run_id, proposal_type, tier, target_yaml, target_canonical,
                   proposed_change, rationale, confidence, evidence_count,
                   evidence_ids, status, created_at, reviewed_at, reviewed_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.run_id,
                    record.proposal_type,
                    record.tier,
                    record.target_yaml,
                    record.target_canonical,
                    record.proposed_change,
                    record.rationale,
                    record.confidence,
                    record.evidence_count,
                    record.evidence_ids,
                    record.status,
                    _to_iso(record.created_at),
                    _to_iso(record.reviewed_at) if record.reviewed_at else None,
                    record.reviewed_by,
                ),
            )
            assert cur.lastrowid is not None
            return int(cur.lastrowid)

    def list_taxonomy_proposals(
        self,
        *,
        status: str | None = None,
        tier: str | None = None,
        limit: int = 200,
    ) -> list[TaxonomyProposalRecord]:
        """提案を status / tier でフィルタして新しい順に取得。"""
        sql = "SELECT * FROM taxonomy_review_proposals"
        clauses: list[str] = []
        params: list[object] = []
        if status:
            clauses.append("status=?")
            params.append(status)
        if tier:
            clauses.append("tier=?")
            params.append(tier)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY datetime(created_at) DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_taxonomy_proposal(r) for r in rows]

    def get_taxonomy_proposal(self, proposal_id: int) -> TaxonomyProposalRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM taxonomy_review_proposals WHERE id=?",
                (proposal_id,),
            ).fetchone()
        return _row_to_taxonomy_proposal(row) if row else None

    def update_taxonomy_proposal_status(
        self,
        proposal_id: int,
        *,
        status: str,
        reviewed_by: str = "manual",
        reviewed_at: datetime | None = None,
    ) -> bool:
        """提案の status を accepted / rejected / deferred / expired に変更。"""
        ts = reviewed_at or datetime.now(UTC)
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE taxonomy_review_proposals
                   SET status=?, reviewed_at=?, reviewed_by=?
                 WHERE id=?
                """,
                (status, _to_iso(ts), reviewed_by, proposal_id),
            )
            return (cur.rowcount or 0) > 0

    def find_pending_proposal(
        self,
        *,
        proposal_type: str,
        target_yaml: str,
        target_canonical: str | None,
        proposed_change: str,
    ) -> TaxonomyProposalRecord | None:
        """同じ提案が既に pending で存在するか (merge 用)。"""
        # PG dialect fix (2026-07-06): 旧実装は `? IS NULL` で NULL 一致を判定していたが、
        # PostgreSQL は `$N IS NULL` の param 型を推論できず
        # "could not determine data type of parameter $N" で失敗する (SQLite は動くため
        # tests をすり抜け、本番 weekly-taxonomy-review が毎週 11 件失敗していた)。
        # target_canonical が NULL かを **整数フラグ (0/1)** で渡し param 型を確定させる。
        tc_is_null = 1 if target_canonical is None else 0
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM taxonomy_review_proposals
                 WHERE status='pending'
                   AND proposal_type=?
                   AND target_yaml=?
                   AND (target_canonical = ? OR (target_canonical IS NULL AND ? = 1))
                   AND proposed_change=?
                 LIMIT 1
                """,
                (proposal_type, target_yaml, target_canonical, tc_is_null, proposed_change),
            ).fetchone()
        return _row_to_taxonomy_proposal(row) if row else None

    def has_rejected_proposal(
        self,
        *,
        proposal_type: str,
        target_yaml: str,
        target_canonical: str | None,
        proposed_change: str,
    ) -> bool:
        """同一提案が過去に却下されているか (却下は再提案しない — MITRE sync と同原則)。

        従来は pending のみ照合していたため、却下した同一提案が翌週の生成で再 INSERT され
        レビューが学習しないループになっていた (2026-07-12 根治)。value が同じでも
        proposal_type/change が異なる提案 (例: typo 却下後の new_canonical) は別主張なので通す。
        """
        tc_is_null = 1 if target_canonical is None else 0
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM taxonomy_review_proposals
                 WHERE status='rejected'
                   AND proposal_type=?
                   AND target_yaml=?
                   AND (target_canonical = ? OR (target_canonical IS NULL AND ? = 1))
                   AND proposed_change=?
                 LIMIT 1
                """,
                (proposal_type, target_yaml, target_canonical, tc_is_null, proposed_change),
            ).fetchone()
        return row is not None

    def refresh_proposal_evidence(
        self,
        proposal_id: int,
        *,
        evidence_count: int,
        evidence_ids: str,
        rationale: str | None = None,
        confidence: str | None = None,
    ) -> bool:
        """既存 pending 提案の evidence 件数を update (週次再生成での merge)。"""
        sets = ["evidence_count=?", "evidence_ids=?"]
        params: list[object] = [evidence_count, evidence_ids]
        if rationale is not None:
            sets.append("rationale=?")
            params.append(rationale)
        if confidence is not None:
            sets.append("confidence=?")
            params.append(confidence)
        params.append(proposal_id)
        with self._connect() as conn:
            cur = conn.execute(
                f"UPDATE taxonomy_review_proposals SET {', '.join(sets)} WHERE id=?",
                params,
            )
            return (cur.rowcount or 0) > 0

    # ----- Actors Stage 4: MITRE 同期レビュー提案 -----

    def insert_actor_update_proposal(
        self,
        *,
        run_id: int | None,
        proposal_type: str,
        mitre_group: str,
        dedup_key: str,
        actor_id: str | None,
        payload: str,
        rationale: str,
    ) -> int:
        """MITRE 同期のレビュー提案を 1 件挿入する。"""
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO actor_update_proposals
                  (run_id, proposal_type, mitre_group, dedup_key, actor_id,
                   payload, rationale, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    run_id,
                    proposal_type,
                    mitre_group,
                    dedup_key,
                    actor_id,
                    payload,
                    rationale,
                    _to_iso(datetime.now(UTC)),
                ),
            )
            assert cur.lastrowid is not None
            return int(cur.lastrowid)

    def find_actor_update_proposal(
        self, *, proposal_type: str, dedup_key: str
    ) -> ActorUpdateProposalRecord | None:
        """同一提案が既に存在するか (status 問わず — rejected の再提案も防ぐ)。"""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM actor_update_proposals
                 WHERE proposal_type=? AND dedup_key=?
                 LIMIT 1
                """,
                (proposal_type, dedup_key),
            ).fetchone()
        return _row_to_actor_proposal(row) if row else None

    def list_actor_update_proposals(
        self, *, status: str | None = "pending", limit: int = 100
    ) -> list[ActorUpdateProposalRecord]:
        """提案を status でフィルタして新しい順に取得。"""
        sql = "SELECT * FROM actor_update_proposals"
        params: list[object] = []
        if status:
            sql += " WHERE status=?"
            params.append(status)
        sql += " ORDER BY datetime(created_at) DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_actor_proposal(r) for r in rows]

    def get_actor_update_proposal(self, proposal_id: int) -> ActorUpdateProposalRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM actor_update_proposals WHERE id=?",
                (proposal_id,),
            ).fetchone()
        return _row_to_actor_proposal(row) if row else None

    def decide_actor_update_proposal(self, proposal_id: int, *, status: str) -> bool:
        """提案を accepted / rejected に確定する。"""
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE actor_update_proposals
                   SET status=?, decided_at=?
                 WHERE id=? AND status='pending'
                """,
                (status, _to_iso(datetime.now(UTC)), proposal_id),
            )
            return (cur.rowcount or 0) > 0

    # ----- Actor Recall Layer: 新興アクター候補 (actor_provisional) 集計 / backfill -----

    def count_provisional_actor_candidates(
        self, *, min_articles: int = 3, since: datetime | None = None
    ) -> list[tuple[str, int]]:
        """暗定アクター候補を value 別に裏取り (distinct article 数 >= min) 集計。新しい順。

        ``since`` 指定で created_at 以降に絞る (現在進行形の新興アクターに focus)。
        """
        sql = (
            "SELECT value, COUNT(DISTINCT article_id) AS n FROM article_entities "
            "WHERE entity_type='actor_provisional'"
        )
        params: list[object] = []
        if since is not None:
            sql += " AND created_at >= ?"
            params.append(_to_iso(since))
        sql += " GROUP BY value HAVING COUNT(DISTINCT article_id) >= ? ORDER BY n DESC LIMIT 200"
        params.append(min_articles)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [(str(r[0]), int(r[1])) for r in rows]

    def list_provisional_with_article_context(
        self, *, since: datetime | None = None
    ) -> list[dict[str, Any]]:
        """暗定候補 × 記事の帰属文脈 (昇格の見込み利得を測る原資)。

        タイトル照合は呼び出し側 (Python) が行う — 候補名に LIKE のワイルドカードが
        含まれると SQL 側で過剰一致して利得を水増しするため。母集団は暗定 entity
        のみで小さく、全件を返しても問題にならない。
        """
        sql = (
            "SELECT ae.value AS value, a.article_id AS article_id, a.title AS title,"
            " a.subject_actor_ids AS subject_actor_ids,"
            " a.subject_actor_source AS subject_actor_source"
            " FROM article_entities ae"
            " JOIN articles a ON a.article_id = ae.article_id"
            " WHERE ae.entity_type='actor_provisional'"
        )
        params: list[object] = []
        if since is not None:
            sql += " AND ae.created_at >= ?"
            params.append(_to_iso(since))
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def sample_articles_for_provisional(self, key: str, *, limit: int = 3) -> list[tuple[str, str]]:
        """暗定候補 key を含む記事の (article_id, title) を新しい順に最大 limit 件 (提案の根拠)。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT a.article_id, a.title FROM article_entities ae "
                "JOIN articles a ON a.article_id = ae.article_id "
                "WHERE ae.entity_type='actor_provisional' AND ae.value=? "
                "ORDER BY ae.created_at DESC LIMIT ?",
                (key, limit),
            ).fetchall()
        return [(str(r[0]), str(r[1] or "")) for r in rows]

    def promote_provisional_actor(self, key: str, actor_id: str) -> int:
        """暗定 (actor_provisional=key) を確定 (actor=actor_id) に backfill し暗定行を削除。

        提案承認で辞書化したとき、歴史記事の帰属も確定 actor に昇格させる。新規確定行数を返す。
        """
        now_iso = _to_iso(datetime.now(UTC))
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT article_id FROM article_entities "
                "WHERE entity_type='actor_provisional' AND value=?",
                (key,),
            ).fetchall()
            promoted = 0
            for r in rows:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO article_entities "
                    "(article_id, entity_type, value, created_at) VALUES (?, 'actor', ?, ?)",
                    (str(r[0]), actor_id, now_iso),
                )
                promoted += int(cur.rowcount or 0)
            conn.execute(
                "DELETE FROM article_entities WHERE entity_type='actor_provisional' AND value=?",
                (key,),
            )
        return promoted

    # ----- F1 selections (Phase 5T-T1) -----

    def record_f1_selections(
        self,
        selections: Sequence[F1SelectionRecord],
    ) -> int:
        """F1 (weekly deep dive) 選定 article を記録し、挿入件数を返す。

        空 list 入力時は 0 を返す (no-op、深掘り 0 件配信 case を許容)。
        """
        if not selections:
            return 0
        rows = [
            (
                s.run_id,
                s.article_id,
                s.dedup_key,
                s.composite_score,
                s.pir,
                s.roi,
                s.timeliness,
                s.novelty,
                _to_iso(s.selected_at),
            )
            for s in selections
        ]
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO f1_selections
                  (run_id, article_id, dedup_key, composite_score,
                   pir, roi, timeliness, novelty, selected_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        return len(rows)

    def record_weekly_recap(
        self,
        *,
        run_id: int | None,
        period_label: str,
        recap_text: str,
        candidate_count: int,
        generated_at: datetime | None = None,
    ) -> None:
        """段5: 生成した weekly recap 本文を永続化する (Retrospect 連携)。"""
        ts = _to_iso(generated_at or datetime.now(UTC))
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO weekly_recaps
                  (run_id, period_label, recap_text, candidate_count, generated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (run_id, period_label, recap_text, candidate_count, ts),
            )

    def get_weekly_recap_in_window(
        self, *, start: datetime, end: datetime
    ) -> dict[str, Any] | None:
        """段5: generated_at が [start, end) の最新 recap を返す (Retrospect 用)。

        generated_at は ISO 文字列保存 (SQLite) / TIMESTAMPTZ (PG)。ISO 文字列での
        範囲比較は両 dialect で正しく動く (datetime() を避け dialect 非依存)。
        """
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT period_label, recap_text, candidate_count, generated_at
                  FROM weekly_recaps
                 WHERE generated_at >= ? AND generated_at < ?
                 ORDER BY generated_at DESC
                 LIMIT 1
                """,
                (_to_iso(start), _to_iso(end)),
            ).fetchone()
        if row is None:
            return None
        return {
            "period_label": row["period_label"],
            "recap_text": row["recap_text"],
            "candidate_count": int(row["candidate_count"]),
            "generated_at": str(row["generated_at"]),
        }

    def record_daily_brief(
        self,
        *,
        run_id: int | None,
        slot: str,
        period_label: str,
        title: str,
        bluf: str,
        summary: str,
        section_count: int,
        sources: list[dict[str, str]],
        payload: dict[str, Any] | None = None,
        generated_at: datetime | None = None,
    ) -> None:
        """W1: 合成した日次ブリーフ (朝刊/夕刊) 本文を永続化する (Web 日次ブリーフビュー)。

        sources は ``[{title, url}]`` を JSON 文字列で保存する (中身は SQL で問い合わせない)。
        payload は Web 構造描画用の構造化 JSON (brief_payload.build_brief_payload)。
        generated_at は ISO 文字列保存で dialect 非依存にする (weekly_recaps と同様)。
        """
        import json as _json

        ts = _to_iso(generated_at or datetime.now(UTC))
        sources_json = _json.dumps(sources, ensure_ascii=False)
        payload_json = _json.dumps(payload, ensure_ascii=False) if payload is not None else None
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO daily_briefs
                  (run_id, slot, period_label, title, bluf, summary,
                   section_count, sources, payload, generated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    slot,
                    period_label,
                    title,
                    bluf,
                    summary,
                    section_count,
                    sources_json,
                    payload_json,
                    ts,
                ),
            )

    def list_daily_briefs(
        self, *, limit: int = 30, meta_only: bool = False
    ) -> list[dict[str, Any]]:
        """W1: 最近の日次ブリーフを generated_at 降順で返す (Web 通読 + 履歴用)。

        meta_only=True は一覧サイドバー用の軽量メタのみ (summary/payload/bluf/sources を
        除外)。60 件で ~2MB になる本文全乗せの over-fetch を避け、本文は
        :meth:`get_daily_brief` で選択時に 1 件取得する (2026-07-31 表示遅延の根治)。
        generated_at は ISO 文字列に正規化して返す (SQLite=str / PG=datetime の両対応)。
        """
        if meta_only:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT id, slot, period_label, title, section_count, generated_at
                      FROM daily_briefs
                     ORDER BY generated_at DESC
                     LIMIT ?
                    """,
                    (int(limit),),
                ).fetchall()
            out: list[dict[str, Any]] = []
            for r in rows:
                gen = _from_iso(r["generated_at"])
                out.append(
                    {
                        "id": int(r["id"]),
                        "slot": str(r["slot"]),
                        "period_label": str(r["period_label"]),
                        "title": str(r["title"]),
                        "section_count": int(r["section_count"] or 0),
                        "generated_at": gen.isoformat() if gen else str(r["generated_at"]),
                    }
                )
            return out
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, run_id, slot, period_label, title, bluf, summary,
                       section_count, sources, payload, generated_at
                  FROM daily_briefs
                 ORDER BY generated_at DESC
                 LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        return [_row_to_daily_brief(r) for r in rows]

    def get_daily_brief(self, brief_id: int) -> dict[str, Any] | None:
        """1 件の日次ブリーフを本文込みで返す (選択時のオンデマンド取得)。"""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, run_id, slot, period_label, title, bluf, summary,
                       section_count, sources, payload, generated_at
                  FROM daily_briefs
                 WHERE id = ?
                """,
                (int(brief_id),),
            ).fetchone()
        return _row_to_daily_brief(row) if row is not None else None

    def find_recent_f1_dedup_keys(
        self,
        *,
        lookback_hours: int,
        now: datetime | None = None,
    ) -> set[str]:
        """直近 ``lookback_hours`` 以内に F1 が選定した dedup_key の set を返す。

        novelty 軸の prefilter (Stage 0) で使う。NULL dedup_key は除外。

        Args:
            lookback_hours: 振り返り時間 (例: 672 = 過去 4 週)
            now: 基準時刻 (テスト用に注入可能)
        """
        base = now or datetime.now(UTC)
        since = base - timedelta(hours=lookback_hours)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT dedup_key FROM f1_selections
                 WHERE selected_at >= ?
                   AND dedup_key IS NOT NULL
                """,
                (_to_iso(since),),
            ).fetchall()
        return {row["dedup_key"] for row in rows if row["dedup_key"]}

    # ----- DB maintenance (Phase 5B: 肥大化対策) -----

    def vacuum(self) -> None:
        """SQLite ``VACUUM`` を実行して削除済み領域を物理的に回収する。

        SQLite は DELETE 後に領域を返さないので、purge を効かせるためには
        定期的な VACUUM が必要。個人運用規模 (数百 MB) なら数秒で完了する。
        本番中は DB がロックされるので、起動時または cron で呼ぶこと。
        """
        with self._connect() as conn:
            conn.execute("VACUUM")

    def db_stats(self) -> dict[str, int]:
        """各テーブルの行数と DB ファイルサイズを返す (UI のストレージ表示用)。

        キー: ``runs / articles / run_logs / dedup_seen_urls /
        article_embeddings / file_size_bytes``。
        """
        with self._connect() as conn:
            stats = {
                "runs": int(conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]),
                "articles": int(
                    conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0],
                ),
                "run_logs": int(
                    conn.execute("SELECT COUNT(*) FROM run_logs").fetchone()[0],
                ),
                "dedup_seen_urls": int(
                    conn.execute("SELECT COUNT(*) FROM dedup_seen_urls").fetchone()[0],
                ),
                "article_embeddings": int(
                    conn.execute(
                        "SELECT COUNT(*) FROM article_embeddings",
                    ).fetchone()[0],
                ),
            }
        # WAL / SHM ファイルも含めた総物理サイズを返す (ユーザにわかりやすい単位)
        size = 0
        if self._db_path.exists():
            size += self._db_path.stat().st_size
        for ext in ("-wal", "-shm"):
            sidecar = self._db_path.with_name(self._db_path.name + ext)
            if sidecar.exists():
                size += sidecar.stat().st_size
        stats["file_size_bytes"] = size
        return stats

    # ----- aggregations (Web UI ダッシュボード) -----

    def daily_post_counts(self, days: int = 7) -> list[tuple[str, int]]:
        """直近 N 日の日次投稿件数 [(YYYY-MM-DD, count), ...]。

        日境界は **JST 暦**。created_at は UTC(timestamptz)なので JST に変換してから
        日付を取り出す (UTC 集計だと JST 深夜の投稿が前日に按分されるバグを防ぐ)。
        """
        # day バケットは backend 依存 (PG: AT TIME ZONE / SQLite: strftime の時差シフト)
        from src.storage.db_backend import is_pg_enabled

        if is_pg_enabled():
            day_expr = "to_char(created_at AT TIME ZONE 'Asia/Tokyo', 'YYYY-MM-DD')"
        else:
            day_expr = "strftime('%Y-%m-%d', created_at, '+9 hours')"
        sql = (
            f"SELECT {day_expr} AS day, COUNT(*) AS cnt "  # noqa: S608 (day_expr は定数)
            "FROM articles WHERE status = 'posted' "
            "AND created_at >= datetime('now', ?) "
            "GROUP BY day ORDER BY day"
        )
        with self._connect() as conn:
            rows = conn.execute(sql, (f"-{days} days",)).fetchall()
        return [(r["day"], int(r["cnt"])) for r in rows]

    # サイバー脅威カテゴリ (importance が "脅威 actionability" を意味する category)。
    # geopolitical/research/policy/other は importance の意味が異なる (or 文脈) ため
    # cyber_only=True の重要度分布から除外する。
    _CYBER_THREAT_CATEGORIES = (
        "apt",
        "apt_leak",
        "vulnerability",
        "malware",
        "incident",
        "breach",
        "advisory",
        "phishing",
    )

    def importance_breakdown(self, days: int = 7, *, cyber_only: bool = False) -> dict[str, int]:
        """直近 N 日の重要度別投稿件数。

        cyber_only=True で **サイバー脅威カテゴリのみ**に絞る (geopolitical 等の
        戦略 importance を混ぜず、脅威トリアージの分布を正しく表す)。
        """
        clauses = [
            "status = 'posted'",
            "created_at >= datetime('now', ?)",
            "importance IS NOT NULL",
        ]
        params: list[object] = [f"-{days} days"]
        if cyber_only:
            placeholders = ", ".join("?" for _ in self._CYBER_THREAT_CATEGORIES)
            clauses.append(f"category IN ({placeholders})")
            params.extend(self._CYBER_THREAT_CATEGORIES)
        where = " AND ".join(clauses)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT importance, COUNT(*) AS cnt FROM articles "  # noqa: S608
                f"WHERE {where} GROUP BY importance",
                params,
            ).fetchall()
        return {r["importance"]: int(r["cnt"]) for r in rows}

    def extract_failure_rate(self, days: int = 7) -> float:
        """直近 N 日の抽出失敗率 (0.0〜1.0)。"""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    SUM(CASE WHEN status = 'extract_failed' THEN 1 ELSE 0 END) AS failed,
                    COUNT(*) AS total
                FROM articles
                WHERE created_at >= datetime('now', ?)
                """,
                (f"-{days} days",),
            ).fetchone()
        total = int(row["total"] or 0)
        if total == 0:
            return 0.0
        return float(row["failed"] or 0) / total

    def stump_body_rate(self, days: int = 7) -> float:
        """直近 N 日の**切り株率** (0.0〜1.0) = body_source='feed_summary' / 本文あり全体。

        全文取得が失敗して feed 抜粋へ無音 fallback した記事の割合。extract_failure_rate
        (可視の extract_failed) が過小評価する「全文が取れていない」実態を測る第 2 系列
        (docs/body_extraction_and_entity_integrity_redesign.md §2.3)。
        """
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    SUM(CASE WHEN body_source = 'feed_summary' THEN 1 ELSE 0 END) AS stump,
                    COUNT(*) AS total
                FROM articles
                WHERE created_at >= datetime('now', ?)
                  AND body IS NOT NULL AND body <> ''
                """,
                (f"-{days} days",),
            ).fetchone()
        total = int(row["total"] or 0)
        if total == 0:
            return 0.0
        return float(row["stump"] or 0) / total
