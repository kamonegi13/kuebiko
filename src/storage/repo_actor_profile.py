"""actor_observed_profile (アクター行動史 月次期間行) の repo mixin。

行は決定論射影のため、月単位の**全置換** (replace) を正とする — 再蒸留で消えた
アクターの stale 行を残さない。詳細な設計コメントは schema_sql.py と
src/cti/actor_observed_history.py を参照。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from src.cti.actor_observed_history import ActorMonthProfile
from src.storage.repo_base import RunHistoryRepositoryBase
from src.storage.row_mappers import _normalize_jsonb, _to_iso

# SQLite の変数上限 (999) を踏まえた IN 句のチャンクサイズ
_IN_CHUNK = 400


def _load_counts(raw: object) -> dict[str, int]:
    """JSONB (PG は dict、SQLite は TEXT) を {str: int} に正規化。"""
    data = json.loads(_normalize_jsonb(raw))
    if not isinstance(data, dict):
        return {}
    return {str(k): int(v) for k, v in data.items() if isinstance(v, int | float)}


def _row_to_profile(row: Any) -> ActorMonthProfile:
    return ActorMonthProfile(
        actor_id=str(row["actor_id"]),
        month=str(row["month"]),
        subject_articles=int(row["subject_articles"]),
        distinct_sources=int(row["distinct_sources"]),
        sectors=_load_counts(row["sectors"]),
        countries=_load_counts(row["countries"]),
        malware=_load_counts(row["malware"]),
        ttps=_load_counts(row["ttps"]),
        campaigns=_load_counts(row["campaigns"]),
        japan_targeted=int(row["japan_targeted"]),
        kev_hits=int(row["kev_hits"]),
    )


class ActorProfileMixin(RunHistoryRepositoryBase):
    """アクター行動史 (actor_observed_profile) の読み書き。"""

    def count_actor_profile_rows(self) -> int:
        """全行数 (0 なら初回 backfill が必要)。"""
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM actor_observed_profile").fetchone()
        return int(row["n"]) if row else 0

    def replace_actor_month_profiles(
        self, month: str, profiles: Sequence[ActorMonthProfile]
    ) -> int:
        """指定月の行を全置換する (決定論射影の再蒸留に対応)。返り値は書込行数。"""
        now_iso = _to_iso(datetime.now(UTC))
        with self._connect() as conn:
            conn.execute("DELETE FROM actor_observed_profile WHERE month = ?", (month,))
            for p in profiles:
                conn.execute(
                    "INSERT INTO actor_observed_profile "
                    "(actor_id, month, subject_articles, distinct_sources, sectors, countries, "
                    " malware, ttps, campaigns, japan_targeted, kev_hits, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        p.actor_id,
                        month,
                        p.subject_articles,
                        p.distinct_sources,
                        json.dumps(p.sectors, ensure_ascii=False),
                        json.dumps(p.countries, ensure_ascii=False),
                        json.dumps(p.malware, ensure_ascii=False),
                        json.dumps(p.ttps, ensure_ascii=False),
                        json.dumps(p.campaigns, ensure_ascii=False),
                        p.japan_targeted,
                        p.kev_hits,
                        now_iso,
                    ),
                )
        return len(profiles)

    def list_actor_month_profiles(self, actor_ids: Sequence[str]) -> list[ActorMonthProfile]:
        """指定 actor id 群 (canonical + merge 旧 id) の月次行を月昇順で返す。"""
        ids = [a for a in actor_ids if a]
        if not ids:
            return []
        ph = ",".join("?" for _ in ids)
        sql = (  # noqa: S608 — placeholder は固定生成、値はパラメータバインド
            f"SELECT * FROM actor_observed_profile WHERE actor_id IN ({ph}) "
            "ORDER BY month ASC, actor_id ASC"
        )
        with self._connect() as conn:
            rows = conn.execute(sql, ids).fetchall()
        return [_row_to_profile(r) for r in rows]

    def list_subject_article_rows(self, since: datetime, before: datetime) -> list[dict[str, Any]]:
        """時間窓 [since, before) の subject 記事の生行 (蒸留入力 + D5 drill-down)。

        run 横断で同一 article_id が複数行あり得る — dedup は蒸留側
        (actor_observed_history._dedupe_by_article) が行う。
        """
        sql = (
            "SELECT article_id, created_at, subject_actor_ids, victim_sector_canonical, "
            "victim_country_iso, posted_channel, feed_url, title, url, feed_title, importance "
            "FROM articles "
            "WHERE subject_actor_ids IS NOT NULL AND subject_actor_ids != '' "
            "AND created_at >= ? AND created_at < ?"
        )
        with self._connect() as conn:
            rows = conn.execute(sql, (_to_iso(since), _to_iso(before))).fetchall()
        return [
            {
                "article_id": str(r["article_id"]),
                "created_at": str(r["created_at"]),
                "subject_actor_ids": str(r["subject_actor_ids"]),
                "victim_sector_canonical": r["victim_sector_canonical"],
                "victim_country_iso": r["victim_country_iso"],
                "posted_channel": r["posted_channel"],
                "feed_url": r["feed_url"],
                "title": str(r["title"] or ""),
                "url": str(r["url"] or ""),
                "feed_title": r["feed_title"],
                "importance": r["importance"],
            }
            for r in rows
        ]

    def list_unevaluated_titles(self, since: datetime) -> list[dict[str, Any]]:
        """subject 未評価 (source IS NULL) の記事の title/category (週次 title 層スイープ用)。

        ransomware.live 等、briefing 永続化を経ない取込経路は取込時の主題判定を
        通らない — 週次で決定論の title 層を適用し行動史の取りこぼしを防ぐ。
        """
        sql = (
            "SELECT DISTINCT article_id, title, category FROM articles "
            "WHERE subject_actor_source IS NULL AND created_at >= ?"
        )
        with self._connect() as conn:
            rows = conn.execute(sql, (_to_iso(since),)).fetchall()
        return [dict(r) for r in rows]

    def search_titles_by_names(self, names: Sequence[str]) -> list[dict[str, Any]]:
        """名前群のいずれかを **title** に含む記事 (全期間 — D3 title 全期間パス)。

        title は永久メタのため retention に依存せず全史を走査できる。LIKE は粗い
        prefilter — word-boundary 照合は呼び出し側 (determine_subject_actors)。
        """
        cleaned = [n.strip().lower() for n in names if n and n.strip()]
        if not cleaned:
            return []
        like_clause = " OR ".join("lower(title) LIKE ?" for _ in cleaned)
        params: list[object] = [f"%{n}%" for n in cleaned]
        sql = (  # noqa: S608 — clause は固定生成、値はパラメータバインド
            "SELECT article_id, created_at, title, category, "
            "subject_actor_ids, subject_actor_source, subject_actor_confidence "
            f"FROM articles WHERE ({like_clause}) ORDER BY created_at DESC"
        )
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def search_llm_primary_by_names(self, names: Sequence[str]) -> list[dict[str, Any]]:
        """保存済み LLM 生出力 (llm_primary_actor_raw) が名前群に近い記事 (全期間 — D3)。

        raw は slug 形 ("volt-typhoon" 等) があり得るため、空白⇄ハイフン/アンダースコア
        の変形も prefilter に含める。厳密解決は呼び出し側 (resolve_actor_by_name)。
        """
        cleaned = {n.strip().lower() for n in names if n and n.strip()}
        variants: set[str] = set()
        for n in cleaned:
            variants.update({n, n.replace(" ", "-"), n.replace(" ", "_")})
        if not variants:
            return []
        vlist = sorted(variants)
        like_clause = " OR ".join("lower(llm_primary_actor_raw) LIKE ?" for _ in vlist)
        params: list[object] = [f"%{v}%" for v in vlist]
        sql = (  # noqa: S608 — clause は固定生成、値はパラメータバインド
            "SELECT article_id, created_at, title, category, body, "
            "subject_actor_ids, subject_actor_source, subject_actor_confidence, "
            "llm_primary_actor_raw, llm_primary_confidence "
            "FROM articles "
            "WHERE llm_primary_actor_raw IS NOT NULL AND llm_primary_actor_raw <> '' "
            f"AND ({like_clause}) ORDER BY created_at DESC"
        )
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def search_recent_articles_by_names(
        self, names: Sequence[str], since: datetime
    ) -> list[dict[str, Any]]:
        """名前群のいずれかを title/body に含む body 現存記事 (承認時有界再帰属の候補)。

        LIKE は粗い prefilter — word-boundary の厳密照合は呼び出し側 (registry) が行う。
        % / _ を escape しない誤差は候補が広がる方向のみで、偽陰性は生まない。
        run 横断の重複行はそのまま返す (新しい順) — dedup は呼び出し側。
        """
        cleaned = [n.strip().lower() for n in names if n and n.strip()]
        if not cleaned:
            return []
        like_clause = " OR ".join("(lower(title) LIKE ? OR lower(body) LIKE ?)" for _ in cleaned)
        params: list[object] = [_to_iso(since)]
        for n in cleaned:
            pat = f"%{n}%"
            params.extend((pat, pat))
        sql = (  # noqa: S608 — clause は固定生成、値はパラメータバインド
            "SELECT article_id, created_at, title, category, body, "
            "subject_actor_ids, subject_actor_source, subject_actor_confidence "
            "FROM articles "
            "WHERE created_at >= ? AND body IS NOT NULL AND body != '' "
            f"AND ({like_clause}) ORDER BY created_at DESC"
        )
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            {
                "article_id": str(r["article_id"]),
                "created_at": str(r["created_at"]),
                "title": str(r["title"] or ""),
                "category": r["category"],
                "body": str(r["body"] or ""),
                "subject_actor_ids": r["subject_actor_ids"],
                "subject_actor_source": r["subject_actor_source"],
                "subject_actor_confidence": r["subject_actor_confidence"],
            }
            for r in rows
        ]

    def update_subject_actor_fields(
        self,
        article_id: str,
        *,
        ids_csv: str,
        source: str,
        confidence: str | None,
    ) -> int:
        """記事の subject 列を更新する (run 横断の**全行**を更新 — GROUP BY 教訓)。"""
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE articles SET subject_actor_ids = ?, subject_actor_source = ?, "
                "subject_actor_confidence = ? WHERE article_id = ?",
                (ids_csv, source, confidence, article_id),
            )
        return int(cur.rowcount if cur.rowcount is not None else 0)

    def list_entity_pairs_for_articles(
        self, article_ids: Sequence[str], types: Sequence[str]
    ) -> dict[str, set[tuple[str, str]]]:
        """article_id → {(entity_type, value)} を返す (蒸留入力、IN 句はチャンク)。"""
        ids = [a for a in article_ids if a]
        tlist = [t for t in types if t]
        if not ids or not tlist:
            return {}
        tph = ",".join("?" for _ in tlist)
        out: dict[str, set[tuple[str, str]]] = {}
        with self._connect() as conn:
            for i in range(0, len(ids), _IN_CHUNK):
                chunk = ids[i : i + _IN_CHUNK]
                aph = ",".join("?" for _ in chunk)
                sql = (  # noqa: S608 — placeholder は固定生成、値はパラメータバインド
                    "SELECT article_id, entity_type, value FROM article_entities "
                    f"WHERE article_id IN ({aph}) AND entity_type IN ({tph})"
                )
                for r in conn.execute(sql, [*chunk, *tlist]).fetchall():
                    out.setdefault(str(r["article_id"]), set()).add(
                        (str(r["entity_type"]), str(r["value"]))
                    )
        return out

    # ---------- F5: alias 使用統計 ----------

    def record_alias_usage(self, article_id: str, pairs: Sequence[tuple[str, str]]) -> int:
        """本文照合でヒットした (actor_id, 名前) を記事単位で記録する (重複は無視)。"""
        from src.cti.actor_observed_history import month_label

        cleaned = [(a.strip(), n.strip()) for a, n in pairs if a.strip() and n.strip()]
        if not cleaned:
            return 0
        now = datetime.now(UTC)
        month = month_label(now)
        now_iso = _to_iso(now)
        with self._connect() as conn:
            for actor_id, name in cleaned:
                conn.execute(
                    "INSERT OR IGNORE INTO actor_alias_usage "
                    "(article_id, actor_id, name, month, created_at) VALUES (?, ?, ?, ?, ?)",
                    (article_id, actor_id, name, month, now_iso),
                )
        return len(cleaned)

    def actor_profile_summaries(self) -> dict[str, dict[str, Any]]:
        """actor_id → {last_month, subject_total} (一覧の鮮度列用バッチ、P2-S8)。"""
        sql = (
            "SELECT actor_id, MAX(month) AS last_month, SUM(subject_articles) AS total "
            "FROM actor_observed_profile GROUP BY actor_id"
        )
        with self._connect() as conn:
            rows = conn.execute(sql).fetchall()
        return {
            str(r["actor_id"]): {
                "last_month": str(r["last_month"]),
                "subject_total": int(r["total"] or 0),
            }
            for r in rows
        }

    def alias_usage_names_by_actor(self) -> dict[str, list[str]]:
        """actor_id → 照合実績のある名前一覧 (一覧の保守列用バッチ、P2-S8)。"""
        sql = "SELECT DISTINCT actor_id, name FROM actor_alias_usage"
        out: dict[str, list[str]] = {}
        with self._connect() as conn:
            for r in conn.execute(sql).fetchall():
                out.setdefault(str(r["actor_id"]), []).append(str(r["name"]))
        return out

    def subject_coverage_by_pipeline(self, since: datetime) -> list[dict[str, Any]]:
        """取込経路 (pipeline) 別の主題被覆 (R3 供給監査、2026-07-26)。

        アクター言及 (actor/actor_provisional entity) を持つ記事が存在するのに主題被覆が
        極端に低い経路を検出する — briefing 非経由取込の「生まれつき 0%」沈黙欠落を
        急落でなく絶対欠落として捕らえる (subject_actor 監査が鳴らなかった穴)。
        """
        sql = (
            "SELECT r.pipeline AS pipeline, "
            "COUNT(DISTINCT a.article_id) AS mentioned, "
            "COUNT(DISTINCT CASE WHEN a.subject_actor_ids IS NOT NULL "
            "  AND a.subject_actor_ids <> '' THEN a.article_id END) AS with_subject "
            "FROM articles a "
            "JOIN runs r ON r.id = a.run_id "
            "JOIN article_entities ae ON ae.article_id = a.article_id "
            "  AND ae.entity_type IN ('actor','actor_provisional') "
            "WHERE a.created_at >= ? "
            "GROUP BY r.pipeline"
        )
        with self._connect() as conn:
            rows = conn.execute(sql, (_to_iso(since),)).fetchall()
        return [
            {
                "pipeline": str(r["pipeline"] or "?"),
                "mentioned": int(r["mentioned"]),
                "with_subject": int(r["with_subject"]),
            }
            for r in rows
        ]

    def alias_usage_totals(self, actor_ids: Sequence[str]) -> dict[str, int]:
        """名前ごとの累計ヒット記事数 (辞書 UI の「どの別名が発火しているか」表示用)。"""
        ids = [a for a in actor_ids if a]
        if not ids:
            return {}
        ph = ",".join("?" for _ in ids)
        sql = (  # noqa: S608 — placeholder は固定生成、値はパラメータバインド
            f"SELECT name, COUNT(*) AS n FROM actor_alias_usage WHERE actor_id IN ({ph}) "
            "GROUP BY name ORDER BY n DESC"
        )
        with self._connect() as conn:
            rows = conn.execute(sql, ids).fetchall()
        return {str(r["name"]): int(r["n"]) for r in rows}
