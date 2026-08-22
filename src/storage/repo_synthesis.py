"""status synthesis / spotlight / forecast / article_notes のメソッド (run_history 分割)。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from src.cti.category_scopes import CYBER_ATTACK_EVENTS
from src.storage.records import (
    ArticleNoteRecord,
    ArticleRecord,
    StatusSynthesisRecord,
)
from src.storage.repo_base import RunHistoryRepositoryBase
from src.storage.row_mappers import (
    _from_iso,
    _row_to_article,
    _row_to_article_note,
    _row_to_forecast_indicator,
    _row_to_spotlight,
    _row_to_synthesis,
    _to_iso,
)


class SynthesisMixin(RunHistoryRepositoryBase):
    # ----- Phase 3: status synthesis -----

    def upsert_status_synthesis(self, record: StatusSynthesisRecord) -> int:
        """期間 (period_type, period_start) ごとに 1 行を UPSERT。

        再生成時 (LLM prompt 調整等) は既存レコードを上書き、id は維持。
        """
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO status_synthesis
                  (period_type, period_start, period_end, headline,
                   weight_section, chain_section, cog_section,
                   spillover_section, pir_section, axes_evidence, tradecraft,
                   article_count, llm_model, generated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(period_type, period_start) DO UPDATE SET
                  period_end=excluded.period_end,
                  headline=excluded.headline,
                  weight_section=excluded.weight_section,
                  chain_section=excluded.chain_section,
                  cog_section=excluded.cog_section,
                  spillover_section=excluded.spillover_section,
                  pir_section=excluded.pir_section,
                  axes_evidence=excluded.axes_evidence,
                  tradecraft=excluded.tradecraft,
                  article_count=excluded.article_count,
                  llm_model=excluded.llm_model,
                  generated_at=excluded.generated_at
                """,
                (
                    record.period_type,
                    _to_iso(record.period_start),
                    _to_iso(record.period_end),
                    record.headline,
                    record.weight_section,
                    record.chain_section,
                    record.cog_section,
                    record.spillover_section,
                    record.pir_section,
                    record.axes_evidence,
                    record.tradecraft or "{}",
                    record.article_count,
                    record.llm_model,
                    _to_iso(record.generated_at),
                ),
            )
            # ON CONFLICT で update された場合 lastrowid は 0、existing row を取得
            if cur.lastrowid:
                return int(cur.lastrowid)
            row = conn.execute(
                "SELECT id FROM status_synthesis WHERE period_type=? AND period_start=?",
                (record.period_type, _to_iso(record.period_start)),
            ).fetchone()
            return int(row["id"]) if row else 0

    def get_latest_synthesis(
        self,
        *,
        period_type: str,
    ) -> StatusSynthesisRecord | None:
        """最新の synthesis 1 件を取得 (UI の headline 帯 / summary page 用)。"""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM status_synthesis
                 WHERE period_type=?
                 ORDER BY datetime(period_start) DESC
                 LIMIT 1
                """,
                (period_type,),
            ).fetchone()
        return _row_to_synthesis(row) if row else None

    def list_synthesis(
        self,
        *,
        period_type: str,
        limit: int = 12,
    ) -> list[StatusSynthesisRecord]:
        """指定期間の synthesis を新しい順に列挙 (履歴閲覧用)。"""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM status_synthesis
                 WHERE period_type=?
                 ORDER BY datetime(period_start) DESC
                 LIMIT ?
                """,
                (period_type, limit),
            ).fetchall()
        return [_row_to_synthesis(r) for r in rows]

    def mark_synthesis_posted(
        self,
        *,
        period_type: str,
        period_start: datetime,
        when: datetime | None = None,
    ) -> int:
        """synthesis の Discord 配信済時刻を記録 (Phase 2 K6、再配信 dedup)。"""
        ts = _to_iso(when or datetime.now(UTC))
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE status_synthesis SET posted_at=? WHERE period_type=? AND period_start=?",
                (ts, period_type, _to_iso(period_start)),
            )
            return int(cur.rowcount or 0)

    # ----- Phase Diamond verify-spotlight: PIR Spotlight -----

    def upsert_pir_spotlight(self, record: Any) -> int:
        """SpotlightRecord を UPSERT (pir_id × period_type × period_start で一意)。"""
        import json as _json

        from src.spotlight.models import SpotlightRecord

        if not isinstance(record, SpotlightRecord):
            raise TypeError(f"expected SpotlightRecord, got {type(record).__name__}")
        key_events_json = _json.dumps(
            [ke.model_dump() for ke in record.key_events],
            ensure_ascii=False,
        )
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO pir_spotlight
                  (pir_id, pir_title, period_type, period_start, period_end,
                   headline, outlook, key_events, article_count,
                   llm_model, generated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(pir_id, period_type, period_start) DO UPDATE SET
                  pir_title       = excluded.pir_title,
                  period_end      = excluded.period_end,
                  headline        = excluded.headline,
                  outlook         = excluded.outlook,
                  key_events      = excluded.key_events,
                  article_count   = excluded.article_count,
                  llm_model       = excluded.llm_model,
                  generated_at    = excluded.generated_at
                """,
                (
                    record.pir_id,
                    record.pir_title,
                    record.period_type,
                    _to_iso(record.period_start),
                    _to_iso(record.period_end),
                    record.headline,
                    record.outlook,
                    key_events_json,
                    record.article_count,
                    record.llm_model,
                    _to_iso(record.generated_at),
                ),
            )
            conn.commit()
            row = conn.execute(
                "SELECT id FROM pir_spotlight WHERE pir_id=? AND period_type=? AND period_start=?",
                (record.pir_id, record.period_type, _to_iso(record.period_start)),
            ).fetchone()
        return int(row["id"]) if row else 0

    def get_latest_spotlight(self, *, pir_id: str, period_type: str = "weekly") -> Any:
        """最新の Spotlight 1 件を取得。"""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM pir_spotlight
                 WHERE pir_id=? AND period_type=?
                 ORDER BY datetime(period_start) DESC
                 LIMIT 1
                """,
                (pir_id, period_type),
            ).fetchone()
        return _row_to_spotlight(row) if row else None

    def get_previous_spotlight(self, *, pir_id: str, period_type: str, before: datetime) -> Any:
        """``before`` より前 (period_start < before) の最新 Spotlight 1 件 (前期継続性用)。

        synthesis の ``_load_previous_synthesis`` と同思想: 当期の再生成時に当期自身を
        「前期」と誤認しないよう period_start を厳密 < before で絞る (=「事象は過去・未来に
        連なる」を spotlight にも与える)。
        """
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM pir_spotlight
                 WHERE pir_id=? AND period_type=? AND datetime(period_start) < datetime(?)
                 ORDER BY datetime(period_start) DESC
                 LIMIT 1
                """,
                (pir_id, period_type, _to_iso(before)),
            ).fetchone()
        return _row_to_spotlight(row) if row else None

    def list_all_latest_spotlights(self, *, period_type: str = "weekly") -> list[Any]:
        """全 PIR の最新 Spotlight をリストアップ (UI 一覧用)。"""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT s1.* FROM pir_spotlight s1
                INNER JOIN (
                  SELECT pir_id, MAX(datetime(period_start)) AS max_ts
                    FROM pir_spotlight
                   WHERE period_type=?
                   GROUP BY pir_id
                ) s2 ON s1.pir_id = s2.pir_id
                    AND datetime(s1.period_start) = s2.max_ts
                    AND s1.period_type=?
                ORDER BY datetime(s1.generated_at) DESC
                """,
                (period_type, period_type),
            ).fetchall()
        return [_row_to_spotlight(row) for row in rows]

    def mark_spotlight_posted(
        self,
        *,
        pir_id: str,
        period_type: str,
        period_start: datetime,
        when: datetime | None = None,
    ) -> int:
        """spotlight の Discord 配信済時刻を記録 (Phase 2 K6、再配信 dedup)。"""
        ts = _to_iso(when or datetime.now(UTC))
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE pir_spotlight SET posted_at=? "
                "WHERE pir_id=? AND period_type=? AND period_start=?",
                (ts, pir_id, period_type, _to_iso(period_start)),
            )
            return int(cur.rowcount or 0)

    # ----- Phase 4 (将来予測): 時系列集計 + forecast_indicators -----

    def entity_event_times(
        self,
        entity_type: str,
        *,
        since: datetime,
        limit_values: int | None = None,
    ) -> dict[str, list[datetime]]:
        """article_entities (指定 type) の value ごとに出現時刻 (created_at) 群を返す。

        forecast の週次バケット集計の元データ (FC3/FC4/FC5)。``since`` 以降のみ。
        join せず article_entities.created_at を使うので fan-out しない。
        ``limit_values`` 指定時は件数上位のみ返す。
        """
        since_iso = _to_iso(since)
        out: dict[str, list[datetime]] = {}
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT value, created_at FROM article_entities "
                "WHERE entity_type = ? AND created_at >= ?",
                (entity_type, since_iso),
            ).fetchall()
        for r in rows:
            ts = _from_iso(r["created_at"])
            if ts is not None:
                out.setdefault(str(r["value"]), []).append(ts)
        if limit_values is not None and len(out) > limit_values:
            top = sorted(out.items(), key=lambda kv: len(kv[1]), reverse=True)[:limit_values]
            out = dict(top)
        return out

    def intent_event_times(self, *, since: datetime) -> dict[str, list[datetime]]:
        """socio_political_intent ごとに article の created_at 群を返す (FC3/FC4)。

        ``unknown`` / NULL は除外。``since`` 以降のみ。
        H3 (intent_confidence 配線): confidence=low の仮説 tag は時系列予測の母数に
        入れない (intent_confidence IS NULL = 旧レジーム確定 tag は残す)。
        """
        since_iso = _to_iso(since)
        out: dict[str, list[datetime]] = {}
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT socio_political_intent AS intent, created_at FROM articles "
                "WHERE socio_political_intent IS NOT NULL "
                "AND socio_political_intent != 'unknown' "
                "AND (intent_confidence IS NULL OR intent_confidence != 'low') "
                "AND created_at >= ?",
                (since_iso,),
            ).fetchall()
        for r in rows:
            ts = _from_iso(r["created_at"])
            if ts is not None:
                out.setdefault(str(r["intent"]), []).append(ts)
        return out

    def victim_sector_event_times(self, *, since: datetime) -> dict[str, list[datetime]]:
        """victim_sector_canonical ごとに cyber-attack 記事の created_at 群を返す。

        I&W 日次バースト検出 (burst.py) の sector scope 用。``entity_event_times`` /
        ``intent_event_times`` と同形の戻り値 (value → created_at 群)。
        status='posted' かつ category が ``CYBER_ATTACK_EVENTS`` (地図と同じ攻撃イベントレンズ、
        SSoT = category_scopes) の行のみ。sector が未分類 (NULL/空/uncategorized/multi_sector)
        の行はバーストの主体として意味を持たないため除外する。
        """
        cat_ph = ",".join(["?"] * len(CYBER_ATTACK_EVENTS))
        excluded = ("", "uncategorized", "multi_sector")
        excl_ph = ",".join(["?"] * len(excluded))
        sql = (
            "SELECT victim_sector_canonical AS v, created_at FROM articles "
            "WHERE status='posted' "
            f"AND category IN ({cat_ph}) "
            "AND victim_sector_canonical IS NOT NULL "
            f"AND victim_sector_canonical NOT IN ({excl_ph}) "
            "AND created_at >= ?"
        )
        params: list[Any] = [*sorted(CYBER_ATTACK_EVENTS), *excluded, _to_iso(since)]
        out: dict[str, list[datetime]] = {}
        with self._connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        for r in rows:
            ts = _from_iso(r["created_at"])
            if ts is not None:
                out.setdefault(str(r["v"]), []).append(ts)
        return out

    def victim_country_event_times(self, *, since: datetime) -> dict[str, list[datetime]]:
        """victim_country_iso ごとに cyber-attack 記事の created_at 群を返す。

        I&W 日次バースト検出 (burst.py) の country scope 用。国が未解決 (NULL/空) の
        行は除外する。ISO コードは大文字に正規化する (geo_cyber_map と同じ表記統一)。
        """
        cat_ph = ",".join(["?"] * len(CYBER_ATTACK_EVENTS))
        sql = (
            "SELECT victim_country_iso AS v, created_at FROM articles "
            "WHERE status='posted' "
            f"AND category IN ({cat_ph}) "
            "AND victim_country_iso IS NOT NULL AND victim_country_iso != '' "
            "AND created_at >= ?"
        )
        params: list[Any] = [*sorted(CYBER_ATTACK_EVENTS), _to_iso(since)]
        out: dict[str, list[datetime]] = {}
        with self._connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        for r in rows:
            ts = _from_iso(r["created_at"])
            if ts is not None:
                out.setdefault(str(r["v"]).upper(), []).append(ts)
        return out

    def recent_cve_values(self, *, days: int = 14, limit: int = 200) -> list[str]:
        """直近 N 日に出現した CVE を頻度降順で返す (Phase 2.5 K5b、NVD CVSS warm 用)。"""
        since_iso = _to_iso(datetime.now(UTC) - timedelta(days=days))
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT value, COUNT(*) AS n FROM article_entities "
                "WHERE entity_type='cve' AND created_at >= ? "
                "GROUP BY value ORDER BY n DESC LIMIT ?",
                (since_iso, limit),
            ).fetchall()
        return [str(r["value"]).upper() for r in rows]

    # ----- Phase 5 (学習・記憶): article_notes -----

    def upsert_article_note(self, record: ArticleNoteRecord) -> None:
        """1 article の memo/bookmark/tags/judgment を upsert (created_at は保持)。"""
        import json as _json

        tags_json = _json.dumps(record.tags, ensure_ascii=False)
        now_iso = _to_iso(datetime.now(UTC))
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO article_notes
                  (article_id, bookmarked, note, tags, judgment, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(article_id) DO UPDATE SET
                  bookmarked = excluded.bookmarked,
                  note       = excluded.note,
                  tags       = excluded.tags,
                  judgment   = excluded.judgment,
                  updated_at = excluded.updated_at
                """,
                (
                    record.article_id,
                    1 if record.bookmarked else 0,
                    record.note,
                    tags_json,
                    record.judgment,
                    _to_iso(record.created_at),
                    now_iso,
                ),
            )

    def get_article_note(self, article_id: str) -> ArticleNoteRecord | None:
        """1 article の note を取得 (無ければ None)。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM article_notes WHERE article_id=?",
                (article_id,),
            ).fetchone()
        return _row_to_article_note(row) if row else None

    def list_article_notes(
        self,
        *,
        bookmarked_only: bool = False,
        tag: str | None = None,
        limit: int = 200,
    ) -> list[ArticleNoteRecord]:
        """note を更新新しい順に列挙 (一覧ページ用)。"""
        clauses: list[str] = []
        params: list[object] = []
        if bookmarked_only:
            clauses.append("bookmarked=1")
        if tag and tag.strip():
            # tags は JSON array 文字列。"tag" 部分一致で絞る (個人規模で十分)。
            clauses.append("LOWER(tags) LIKE ?")
            params.append(f'%"{tag.strip().lower()}"%')
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM article_notes {where} "  # noqa: S608
                "ORDER BY updated_at DESC LIMIT ?",
                params,
            ).fetchall()
        return [_row_to_article_note(r) for r in rows]

    def delete_article_note(self, article_id: str) -> int:
        """note を削除 (件数を返す)。"""
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM article_notes WHERE article_id=?",
                (article_id,),
            )
            return int(cur.rowcount or 0)

    def get_articles_by_ids(self, article_ids: list[str]) -> dict[str, ArticleRecord]:
        """``article_id`` → 最新 ArticleRecord の辞書 (note 一覧の title 解決用)。"""
        if not article_ids:
            return {}
        uniq = list(dict.fromkeys(article_ids))
        placeholders = ", ".join("?" for _ in uniq)
        sql = (
            f"SELECT * FROM articles WHERE article_id IN ({placeholders}) "  # noqa: S608
            "ORDER BY created_at ASC"
        )
        out: dict[str, ArticleRecord] = {}
        with self._connect() as conn:
            rows = conn.execute(sql, uniq).fetchall()
        for r in rows:
            rec = _row_to_article(r)
            out[rec.article_id] = rec  # ASC 順なので最新が残る
        return out

    def upsert_forecast_indicator(self, record: Any) -> int:
        """ForecastIndicatorRecord を UPSERT (FC2)。

        (period_type, period_start, scope, target_value) で一意。再 snapshot 時は
        予測値 (z_score 等) を更新するが、検証フィールド (verified_at/hit/observed_count)
        は touch しない (一度検証したら結果を保持)。
        """
        from src.forecast.models import ForecastIndicatorRecord

        if not isinstance(record, ForecastIndicatorRecord):
            raise TypeError(f"expected ForecastIndicatorRecord, got {type(record).__name__}")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO forecast_indicators
                  (period_type, period_start, scope, target_value, direction,
                   z_score, baseline_avg, latest_count, rationale, created_at,
                   observed_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(period_type, period_start, scope, target_value) DO UPDATE SET
                  direction    = excluded.direction,
                  z_score      = excluded.z_score,
                  baseline_avg = excluded.baseline_avg,
                  latest_count = excluded.latest_count,
                  rationale    = excluded.rationale
                """,
                (
                    record.period_type,
                    _to_iso(record.period_start),
                    record.scope,
                    record.target_value,
                    record.direction,
                    record.z_score,
                    record.baseline_avg,
                    record.latest_count,
                    record.rationale,
                    _to_iso(record.created_at),
                    record.observed_count,
                ),
            )
            row = conn.execute(
                "SELECT id FROM forecast_indicators "
                "WHERE period_type=? AND period_start=? AND scope=? AND target_value=?",
                (
                    record.period_type,
                    _to_iso(record.period_start),
                    record.scope,
                    record.target_value,
                ),
            ).fetchone()
        return int(row["id"]) if row else 0

    def list_forecast_indicators(
        self,
        *,
        period_type: str,
        period_start: datetime | None = None,
        only_open: bool = False,
        limit: int = 200,
    ) -> list[Any]:
        """forecast_indicators を新しい順に列挙 (FC2)。

        ``period_start`` 指定でその期間のみ。``only_open=True`` で未検証のみ。
        """
        clauses = ["period_type = ?"]
        params: list[object] = [period_type]
        if period_start is not None:
            clauses.append("period_start = ?")
            params.append(_to_iso(period_start))
        if only_open:
            clauses.append("verified_at IS NULL")
        where = " AND ".join(clauses)
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM forecast_indicators WHERE {where} "  # noqa: S608
                "ORDER BY datetime(period_start) DESC, z_score DESC LIMIT ?",
                params,
            ).fetchall()
        return [_row_to_forecast_indicator(r) for r in rows]

    def list_open_forecast_indicators_before(
        self,
        *,
        period_type: str,
        before: datetime,
    ) -> list[Any]:
        """``before`` より前の period の未検証 indicator を返す (FC2 検証対象)。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM forecast_indicators "
                "WHERE period_type=? AND verified_at IS NULL AND period_start < ? "
                "ORDER BY datetime(period_start) DESC",
                (period_type, _to_iso(before)),
            ).fetchall()
        return [_row_to_forecast_indicator(r) for r in rows]

    def mark_forecast_indicator_verified(
        self,
        indicator_id: int,
        *,
        hit: bool,
        observed_count: int,
        when: datetime | None = None,
    ) -> int:
        """indicator の検証結果を記録 (FC2)。"""
        ts = _to_iso(when or datetime.now(UTC))
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE forecast_indicators SET verified_at=?, hit=?, observed_count=? WHERE id=?",
                (ts, 1 if hit else 0, observed_count, indicator_id),
            )
            return int(cur.rowcount or 0)

    def forecast_indicator_hit_stats(
        self,
        *,
        period_type: str,
        since: datetime,
    ) -> tuple[int, int]:
        """``since`` 以降に検証された indicator の (検証数, 的中数) を返す (FC2)。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS verified, "
                "SUM(CASE WHEN hit=1 THEN 1 ELSE 0 END) AS hits "
                "FROM forecast_indicators "
                "WHERE period_type=? AND verified_at IS NOT NULL AND verified_at >= ?",
                (period_type, _to_iso(since)),
            ).fetchone()
        verified = int(row["verified"] or 0)
        hits = int(row["hits"] or 0)
        return verified, hits
