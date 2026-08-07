"""runs / live_log / ops_notify_log のリポジトリメソッド (run_history 分割)。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.storage.records import (
    MAX_LOG_LINES_PER_RUN,
    LogLine,
    LogStream,
    RunRecord,
    RunStatus,
)
from src.storage.repo_base import RunHistoryRepositoryBase
from src.storage.row_mappers import (
    _from_iso,
    _row_to_log,
    _row_to_run,
    _to_iso,
    _truncate_line,
)


class RunsMixin(RunHistoryRepositoryBase):
    # ----- runs -----

    def start_run(self, run: RunRecord) -> int:
        """新規 run を作成し、付与された ID を返す。"""
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO runs
                  (started_at, pipeline, dry_run, triggered_by, status,
                   total_fetched, summarized, posted, marked_read,
                   error_count, note, log_line_count, log_truncated,
                   triage_error_count, partial_fetch, partial_fetch_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _to_iso(run.started_at),
                    run.pipeline,
                    int(run.dry_run),
                    run.triggered_by,
                    run.status,
                    run.total_fetched,
                    run.summarized,
                    run.posted,
                    run.marked_read,
                    run.error_count,
                    run.note,
                    run.log_line_count,
                    int(run.log_truncated),
                    run.triage_error_count,
                    int(run.partial_fetch),
                    run.partial_fetch_count,
                ),
            )
            assert cur.lastrowid is not None
            return int(cur.lastrowid)

    def finish_run(
        self,
        run_id: int,
        *,
        status: RunStatus,
        finished_at: datetime,
        total_fetched: int,
        summarized: int,
        posted: int,
        marked_read: int,
        error_count: int,
        note: str | None = None,
        triage_error_count: int = 0,
        partial_fetch: bool = False,
        partial_fetch_count: int = 0,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE runs
                SET finished_at = ?, status = ?,
                    total_fetched = ?, summarized = ?, posted = ?,
                    marked_read = ?, error_count = ?, note = ?,
                    triage_error_count = ?, partial_fetch = ?,
                    partial_fetch_count = ?
                WHERE id = ?
                """,
                (
                    _to_iso(finished_at),
                    status,
                    total_fetched,
                    summarized,
                    posted,
                    marked_read,
                    error_count,
                    note,
                    triage_error_count,
                    int(partial_fetch),
                    partial_fetch_count,
                    run_id,
                ),
            )

    def get_run(self, run_id: int) -> RunRecord | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return _row_to_run(row) if row else None

    def list_runs(
        self,
        limit: int = 50,
        offset: int = 0,
        *,
        status: RunStatus | None = None,
    ) -> list[RunRecord]:
        sql = "SELECT * FROM runs"
        params: list[object] = []
        if status is not None:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY started_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_run(r) for r in rows]

    def delete_run(self, run_id: int) -> bool:
        """指定 run を削除する (articles / run_logs は FK CASCADE で連動削除)。

        ``running`` 状態の run は削除不可 (進行中の subprocess が末尾に書き込む
        途中で消えると行不整合になる)。

        戻り値: 1 件削除なら True、対象なし or running なら False。
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT status FROM runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                return False
            if row["status"] == "running":
                return False
            conn.execute("DELETE FROM runs WHERE id = ?", (run_id,))
        return True

    def delete_runs_older_than(self, days: int) -> int:
        """N 日より古い run をまとめて削除し、件数を返す (running は除外)。

        ``articles`` / ``run_logs`` は FK CASCADE で連動削除される。
        ``dedup_seen_urls`` / ``article_embeddings`` は重複排除に必要なため
        残す (別途 ``purge_old_dedup_entries`` で管理)。
        """
        if days < 0:
            raise ValueError("days は 0 以上必須")
        with self._connect() as conn:
            cur = conn.execute(
                """
                DELETE FROM runs
                WHERE status != 'running'
                  AND started_at < datetime('now', ?)
                """,
                (f"-{days} days",),
            )
            return int(cur.rowcount or 0)

    def cleanup_orphan_article_entities(self) -> int:
        """article が存在しない ``article_entities`` (orphan) を削除し件数を返す (Phase 0 F3)。

        ``article_entities`` は FK を持たない設計のため、過去の run/article 削除で entity が
        orphan として残り、actor/IOC 等の集計が実在しない article 分を数えて skew する。
        起動時に掃除する。
        """
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM article_entities WHERE NOT EXISTS "
                "(SELECT 1 FROM articles WHERE articles.article_id = article_entities.article_id)",
            )
            return int(cur.rowcount or 0)

    def entities_for_articles(
        self, article_ids: list[str], *, entity_type: str
    ) -> dict[str, list[str]]:
        """指定 article 群の entity (指定 type) を ``{article_id: [value, ...]}`` で返す。

        Spotlight 等が候補記事の **抽出済 TTP / actor** を narrative 接地に使うため
        (Q1 2026-06-16: MITRE ID を記憶でなく実抽出値から引かせ ID ドリフトを防ぐ)。
        """
        if not article_ids:
            return {}
        placeholders = ",".join(["?"] * len(article_ids))
        sql = (  # noqa: S608 — placeholders は ? 固定数、値はパラメータバインド
            "SELECT article_id, value FROM article_entities "
            f"WHERE entity_type=? AND article_id IN ({placeholders})"
        )
        out: dict[str, list[str]] = {}
        with self._connect() as conn:
            rows = conn.execute(sql, (entity_type, *article_ids)).fetchall()
        for r in rows:
            out.setdefault(str(r["article_id"]), []).append(str(r["value"]))
        return out

    def entity_article_ids(self, entity_type: str, values: list[str]) -> set[str]:
        """``entity_type`` で値が ``values`` (LOWER 完全一致) のいずれかに該当する article_id 集合。

        検索 facet の AND 合成 (allowed-id 交差) に使う軽量 primitive
        (full record でなく id のみ → PIR 等の大量 match でも安い)。
        """
        vals = [v.lower() for v in values if v]
        if not entity_type or not vals:
            return set()
        ph = ", ".join("?" for _ in vals)
        sql = (  # noqa: S608 — placeholders は ? 固定数、値はパラメータバインド
            "SELECT DISTINCT article_id FROM article_entities "
            f"WHERE entity_type = ? AND LOWER(value) IN ({ph})"
        )
        with self._connect() as conn:
            rows = conn.execute(sql, (entity_type, *vals)).fetchall()
        return {str(r["article_id"]) for r in rows}

    def fail_dangling_runs(self) -> int:
        """起動時に ``status='running'`` のまま残っている run を ``failed`` に倒す。

        シングルワーカー前提で「起動時に走っている run = 前回が異常終了した」と等価。
        戻り値: 倒した件数。
        """
        now = _to_iso(datetime.now(UTC))
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE runs
                SET status = 'failed',
                    finished_at = COALESCE(finished_at, ?),
                    note = COALESCE(note, '') || '[recovered after restart]'
                WHERE status = 'running'
                """,
                (now,),
            )
            return int(cur.rowcount or 0)

    # ----- live_log (Phase 1.5b) -----

    def append_log_line(
        self,
        run_id: int,
        line: str,
        *,
        stream: LogStream = "stdout",
        ts: datetime | None = None,
    ) -> int | None:
        """run_logs に 1 行追加し、付与された seq を返す (上限超過時は None)。

        - 8KB 超は末尾切り詰め + ``... [truncated]`` 付与
        - run 単位で 5000 行を超えたら追加せず、初回超過時のみ
          ``log_truncated=1`` を立てて警告行を入れる
        - ``runs.log_line_count`` を毎回 +1
        """
        when = ts or datetime.now(UTC)
        truncated_line = _truncate_line(line)

        with self._connect() as conn:
            row = conn.execute(
                "SELECT log_line_count, log_truncated FROM runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"run_id={run_id} は存在しません")

            current_count = int(row["log_line_count"])
            already_truncated = bool(row["log_truncated"])

            if current_count >= MAX_LOG_LINES_PER_RUN:
                # 既に上限を超えている: 何もしない (フラグは初回超過時のみ設定済み)
                return None

            next_seq = current_count + 1

            # 上限到達時、最後の 1 行は警告メッセージに置き換える
            if next_seq == MAX_LOG_LINES_PER_RUN and not already_truncated:
                truncated_line = (
                    f"[log truncated: exceeded {MAX_LOG_LINES_PER_RUN} lines, "
                    "subsequent output dropped]"
                )

            conn.execute(
                """
                INSERT INTO run_logs (run_id, seq, ts, stream, line)
                VALUES (?, ?, ?, ?, ?)
                """,
                (run_id, next_seq, _to_iso(when), stream, truncated_line),
            )

            if next_seq == MAX_LOG_LINES_PER_RUN and not already_truncated:
                conn.execute(
                    "UPDATE runs SET log_line_count = ?, log_truncated = 1 WHERE id = ?",
                    (next_seq, run_id),
                )
            else:
                conn.execute(
                    "UPDATE runs SET log_line_count = ? WHERE id = ?",
                    (next_seq, run_id),
                )

            return next_seq

    def get_log_lines(
        self,
        run_id: int,
        *,
        from_seq: int = 0,
        limit: int | None = None,
    ) -> list[LogLine]:
        """run_logs から ``seq > from_seq`` の行を昇順で返す。"""
        sql = (
            "SELECT run_id, seq, ts, stream, line FROM run_logs "
            "WHERE run_id = ? AND seq > ? ORDER BY seq ASC"
        )
        params: list[object] = [run_id, from_seq]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_log(r) for r in rows]

    # ----- ops_notify_log (Phase 5L-1) -----

    def get_last_ops_notification(self, pipeline_name: str) -> tuple[datetime, str] | None:
        """指定 pipeline の最終 ops 通知時刻と status を返す (rate limit 用)。

        戻り値: ``(last_sent_at, status)`` または未送信なら ``None``。
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT last_sent_at, last_status FROM ops_notify_log WHERE pipeline_name = ?",
                (pipeline_name,),
            ).fetchone()
        if row is None:
            return None
        last_sent_at = _from_iso(row["last_sent_at"])
        if last_sent_at is None:
            return None
        return last_sent_at, str(row["last_status"])

    def record_ops_notification(
        self,
        *,
        pipeline_name: str,
        status: str,
        when: datetime | None = None,
    ) -> None:
        """ops 通知を送信した事実を記録する (rate limit 用)。"""
        ts = _to_iso(when or datetime.now(UTC))
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO ops_notify_log (pipeline_name, last_sent_at, last_status)
                VALUES (?, ?, ?)
                ON CONFLICT(pipeline_name) DO UPDATE SET
                  last_sent_at = excluded.last_sent_at,
                  last_status = excluded.last_status
                """,
                (pipeline_name, ts, status),
            )

    # ----- job_last_run (統一ジョブ制御 2026-07-06) -----

    def record_job_run(
        self, job_id: str, *, status: str, detail: str = "", when: datetime | None = None
    ) -> None:
        """bespoke/reactive ジョブの最終実行を記録する (1 行/job、upsert)。"""
        ts = _to_iso(when or datetime.now(UTC))
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO job_last_run (job_id, last_run_at, status, detail)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                  last_run_at = excluded.last_run_at,
                  status = excluded.status,
                  detail = excluded.detail
                """,
                (job_id, ts, status, detail[:500]),
            )
            # 履歴として append (最新 upsert とは別。詳細パネルの実行履歴表示用)。
            conn.execute(
                "INSERT INTO job_run_log (job_id, ran_at, status, detail) VALUES (?, ?, ?, ?)",
                (job_id, ts, status, detail[:500]),
            )

    def runs_for_job(self, job_id: str, *, limit: int = 20) -> list[dict[str, object]]:
        """ジョブの実行履歴を新しい順に返す (詳細パネル用)。

        まず runs テーブル (K1 pipeline のリッチ履歴: posted/fetched/error/note/所要) を
        引き、無ければ job_run_log (bespoke/reactive の append 履歴) に fallback する。
        job.kind ではなく実データで判定するため、runs と job_run_log の両方に出る
        ジョブ (ransomware 等) はリッチな runs 側を優先できる。共通 shape に正規化。
        """
        lim = max(1, min(100, limit))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT started_at, finished_at, status, posted, total_fetched,
                       error_count, note
                FROM runs WHERE pipeline = ?
                ORDER BY started_at DESC LIMIT ?
                """,
                (job_id, lim),
            ).fetchall()
            if rows:
                return [
                    {
                        "started_at": str(r["started_at"] or ""),
                        "finished_at": (str(r["finished_at"]) if r["finished_at"] else None),
                        "status": str(r["status"] or ""),
                        "posted": int(r["posted"] or 0),
                        "total_fetched": int(r["total_fetched"] or 0),
                        "error_count": int(r["error_count"] or 0),
                        "detail": str(r["note"] or ""),
                    }
                    for r in rows
                ]
            log_rows = conn.execute(
                """
                SELECT ran_at, status, detail FROM job_run_log
                WHERE job_id = ? ORDER BY ran_at DESC LIMIT ?
                """,
                (job_id, lim),
            ).fetchall()
            if log_rows:
                return [
                    {
                        "started_at": str(r["ran_at"] or ""),
                        "finished_at": None,
                        "status": str(r["status"] or ""),
                        "posted": None,
                        "total_fetched": None,
                        "error_count": None,
                        "detail": str(r["detail"] or ""),
                    }
                    for r in log_rows
                ]
            # 最後の砦: job_run_log 導入前に走った bespoke は job_last_run の最新 1 件を返す。
            last = conn.execute(
                "SELECT last_run_at, status, detail FROM job_last_run WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        if last is None:
            return []
        return [
            {
                "started_at": str(last["last_run_at"] or ""),
                "finished_at": None,
                "status": str(last["status"] or ""),
                "posted": None,
                "total_fetched": None,
                "error_count": None,
                "detail": str(last["detail"] or ""),
            }
        ]

    def purge_old_job_run_log(self, days: int = 30) -> int:
        """N 日より古い job_run_log を削除し件数を返す (起動時 purge)。

        ran_at は ISO 文字列 (TEXT) なので cutoff も ISO 文字列で渡し text<text 比較にする。
        ``datetime('now',?)`` 翻訳は PG で timestamp を生み text と比較できず crash するため不可
        (SQLite は loose 比較で通ってしまい test をすり抜ける — PG dialect の既知の罠)。
        """
        cutoff = _to_iso(datetime.now(UTC) - timedelta(days=days))
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM job_run_log WHERE ran_at < ?",
                (cutoff,),
            )
            return int(cur.rowcount or 0)

    def get_job_last_runs(self) -> dict[str, dict[str, str]]:
        """全ジョブの最終実行を ``{job_id: {last_run_at, status, detail}}`` で返す。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT job_id, last_run_at, status, detail FROM job_last_run"
            ).fetchall()
        return {
            str(r["job_id"]): {
                "last_run_at": str(r["last_run_at"]),
                "status": str(r["status"]),
                "detail": str(r["detail"] or ""),
            }
            for r in rows
        }

    def latest_runs_by_pipeline(self) -> dict[str, dict[str, str]]:
        """pipeline ごとの最終 run を ``{pipeline: {last_run_at, status}}`` で返す。

        統一ジョブ制御の運用コンソールで K1 pipeline の最終実行を表示する用途。
        """
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT r.pipeline AS pipeline, r.status AS status,
                       COALESCE(r.finished_at, r.started_at) AS last_run_at
                FROM runs r
                WHERE r.started_at = (
                    SELECT MAX(started_at) FROM runs WHERE pipeline = r.pipeline
                )
                """
            ).fetchall()
        out: dict[str, dict[str, str]] = {}
        for r in rows:
            pipeline = str(r["pipeline"] or "")
            if pipeline:
                out[pipeline] = {
                    "last_run_at": str(r["last_run_at"] or ""),
                    "status": str(r["status"] or ""),
                }
        return out
