"""dedup_seen_urls / article_embeddings / CVE dedup / brief cap のメソッド (run_history 分割)。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import numpy as np
from numpy.typing import NDArray

from src.storage.records import (
    DEFAULT_LOG_RETENTION_DAYS,
    ArticleRecord,
    SourceFetchHealth,
)
from src.storage.repo_base import RunHistoryRepositoryBase
from src.storage.row_mappers import _from_iso, _row_to_article, _to_iso


class DedupMixin(RunHistoryRepositoryBase):
    # ----- dedup_seen_urls (Phase 3a) -----

    def is_url_seen(self, url_hash: str) -> bool:
        """URL ハッシュが既に登録されているかを返す。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM dedup_seen_urls WHERE url_hash = ?",
                (url_hash,),
            ).fetchone()
        return row is not None

    def get_seen_url(self, url_hash: str) -> tuple[str, datetime] | None:
        """url_hash に対応する seen 記録 (title, first_seen) を返す。

        Phase B-cal: semantic 続報検出で「前報」の表示情報を引くのに使う。
        該当なし or first_seen 不正なら None。
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT title, first_seen FROM dedup_seen_urls WHERE url_hash = ?",
                (url_hash,),
            ).fetchone()
        if row is None:
            return None
        raw = row["first_seen"]
        if not raw:
            return None
        try:
            ts = raw if isinstance(raw, datetime) else datetime.fromisoformat(str(raw))
        except (ValueError, TypeError):
            return None
        return (str(row["title"] or ""), ts)

    def filter_unseen_hashes(self, url_hashes: list[str]) -> set[str]:
        """渡されたハッシュリストのうち、既に登録されているものを返す。

        新規/既知の判定をバルクで行うために提供 (1 件ずつ問い合わせると遅い)。
        """
        if not url_hashes:
            return set()
        placeholders = ",".join("?" for _ in url_hashes)
        sql = (
            f"SELECT url_hash FROM dedup_seen_urls "  # noqa: S608
            f"WHERE url_hash IN ({placeholders})"
        )
        with self._connect() as conn:
            rows = conn.execute(sql, url_hashes).fetchall()
        return {r["url_hash"] for r in rows}

    def filter_seen_and_touch(self, url_hashes: Sequence[str]) -> set[str]:
        """seen 済み hash 集合を返し、該当行の last_seen を現在時刻に touch する。

        P0 収集飢餓修正: source 層の未見選別が既知 URL を pipeline に流さなくなるため、
        「feed にまだ現れている」記録 (last_seen) はここで更新する。retention purge は
        last_seen 基準 (= feed から消えて N 日後に忘れる)。seen_count は pipeline 到達
        回数の指標なので増やさない。
        """
        if not url_hashes:
            return set()
        now_iso = _to_iso(datetime.now(UTC))
        seen: set[str] = set()
        chunk_size = 400  # SQLite の変数上限 (旧既定 999) を安全に下回るバルク幅
        with self._connect() as conn:
            for i in range(0, len(url_hashes), chunk_size):
                chunk = url_hashes[i : i + chunk_size]
                placeholders = ",".join("?" for _ in chunk)
                rows = conn.execute(
                    f"SELECT url_hash FROM dedup_seen_urls "  # noqa: S608
                    f"WHERE url_hash IN ({placeholders})",
                    chunk,
                ).fetchall()
                found = [str(r["url_hash"]) for r in rows]
                if found:
                    seen.update(found)
                    ph = ",".join("?" for _ in found)
                    conn.execute(
                        f"UPDATE dedup_seen_urls SET last_seen = ? "  # noqa: S608
                        f"WHERE url_hash IN ({ph})",
                        [now_iso, *found],
                    )
        return seen

    def upsert_source_fetch_health(
        self, outcomes: Sequence[tuple[str, str, bool, str, int]]
    ) -> None:
        """(url, name, ok, error, entry_count) を一括 upsert (監査 2026-07-05 P2)。

        ok=True で consecutive_failures をリセット、False で加算。
        last_error_at/last_error は上書きせず残す (いつから回復したかの手掛かり)。
        """
        if not outcomes:
            return
        now_iso = _to_iso(datetime.now(UTC))
        with self._connect() as conn:
            for url, name, ok, error, entry_count in outcomes:
                if ok:
                    conn.execute(
                        """
                        INSERT INTO source_fetch_health
                          (source_key, name, last_ok_at, consecutive_failures,
                           last_article_count, updated_at)
                        VALUES (?, ?, ?, 0, ?, ?)
                        ON CONFLICT(source_key) DO UPDATE SET
                          name = excluded.name,
                          last_ok_at = excluded.last_ok_at,
                          consecutive_failures = 0,
                          last_article_count = excluded.last_article_count,
                          updated_at = excluded.updated_at
                        """,
                        (url, name, now_iso, entry_count, now_iso),
                    )
                else:
                    conn.execute(
                        """
                        INSERT INTO source_fetch_health
                          (source_key, name, last_error_at, last_error,
                           consecutive_failures, last_article_count, updated_at)
                        VALUES (?, ?, ?, ?, 1, 0, ?)
                        ON CONFLICT(source_key) DO UPDATE SET
                          name = excluded.name,
                          last_error_at = excluded.last_error_at,
                          last_error = excluded.last_error,
                          consecutive_failures = source_fetch_health.consecutive_failures + 1,
                          updated_at = excluded.updated_at
                        """,
                        (url, name, now_iso, (error or "")[:300], now_iso),
                    )

    def list_source_fetch_health(self) -> list[SourceFetchHealth]:
        """全 feed の fetch 健全性を返す (feed 死活検知 / heartbeat 用)。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT source_key, name, last_ok_at, last_error_at, last_error, "
                "consecutive_failures, last_article_count, updated_at "
                "FROM source_fetch_health"
            ).fetchall()
        return [
            SourceFetchHealth(
                source_key=str(r["source_key"]),
                name=str(r["name"]),
                last_ok_at=_from_iso(r["last_ok_at"]),
                last_error_at=_from_iso(r["last_error_at"]),
                last_error=r["last_error"],
                consecutive_failures=int(r["consecutive_failures"] or 0),
                last_article_count=int(r["last_article_count"] or 0),
                updated_at=_from_iso(r["updated_at"]),
            )
            for r in rows
        ]

    def count_runs_by_status_since(self, *, hours: int = 24) -> dict[str, int]:
        """直近 N 時間の run 件数を status 別に返す (heartbeat 用)。"""
        cutoff = _to_iso(datetime.now(UTC) - timedelta(hours=hours))
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM runs WHERE started_at >= ? GROUP BY status",
                (cutoff,),
            ).fetchall()
        return {str(r["status"]): int(r["n"]) for r in rows}

    def latest_article_time_by_feed_url(self) -> dict[str, datetime]:
        """feed_url ごとの最新 article created_at (feed 死活検知の産出判定用)。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT feed_url, MAX(created_at) AS latest FROM articles "
                "WHERE feed_url IS NOT NULL AND feed_url <> '' GROUP BY feed_url"
            ).fetchall()
        out: dict[str, datetime] = {}
        for r in rows:
            ts = _from_iso(r["latest"])
            if ts is not None:
                out[str(r["feed_url"])] = ts
        return out

    def mark_url_seen(
        self,
        *,
        url_hash: str,
        url: str,
        article_id: str | None = None,
        title: str | None = None,
        when: datetime | None = None,
    ) -> None:
        """URL を seen として登録する (既存なら last_seen / seen_count を更新)。"""
        ts = _to_iso(when or datetime.now(UTC))
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO dedup_seen_urls
                  (url_hash, url, article_id, title, first_seen, last_seen, seen_count)
                VALUES (?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(url_hash) DO UPDATE SET
                  last_seen = excluded.last_seen,
                  seen_count = dedup_seen_urls.seen_count + 1
                """,
                (url_hash, url, article_id, title, ts, ts),
            )

    # ----- article_embeddings (Phase 3b) -----

    def add_article_embedding(
        self,
        *,
        url_hash: str,
        url: str,
        vector: Sequence[float],
        model: str,
        title: str | None = None,
        when: datetime | None = None,
    ) -> None:
        """記事 embedding を保存する (既存 url_hash は上書き)。"""
        ts = _to_iso(when or datetime.now(UTC))
        arr = np.asarray(vector, dtype=np.float32)
        if arr.ndim != 1:
            raise ValueError(f"embedding vector は 1 次元必須 (got {arr.ndim}d)")
        blob = arr.tobytes()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO article_embeddings
                  (url_hash, url, title, model, dim, vector, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(url_hash) DO UPDATE SET
                  url = excluded.url,
                  title = excluded.title,
                  model = excluded.model,
                  dim = excluded.dim,
                  vector = excluded.vector,
                  created_at = excluded.created_at
                """,
                (url_hash, url, title, model, len(arr), blob, ts),
            )

    def find_similar_embedding(
        self,
        vector: Sequence[float],
        *,
        model: str,
        threshold: float = 0.85,
        window_hours: int = 0,
    ) -> tuple[str, float] | None:
        """同一モデルで保存された embedding 群との最大コサイン類似度を返す。

        類似度が ``threshold`` 以上なら ``(url_hash, similarity)`` を返す。
        該当なし or 既存ゼロ件なら None。

        ``window_hours`` (Phase 3b 拡張): 過去 N 時間以内のベクトルのみを
        比較対象にする (続報・follow-up 記事を取りこぼさないため)。0 で全期間比較。

        個人運用規模 (年間数千件) では全件 cosine を numpy でやれば十分速い。
        将来 数万〜数十万件になったら近似最近傍 (FAISS, hnswlib) に移行する。
        """
        query = np.asarray(vector, dtype=np.float32)
        if query.ndim != 1:
            raise ValueError("query は 1 次元必須")
        q_norm = float(np.linalg.norm(query))
        if q_norm == 0.0:
            return None
        query = query / q_norm

        with self._connect() as conn:
            if window_hours > 0:
                cutoff = (datetime.now(UTC) - timedelta(hours=window_hours)).isoformat()
                rows = conn.execute(
                    "SELECT url_hash, dim, vector FROM article_embeddings "
                    "WHERE model = ? AND created_at >= ?",
                    (model, cutoff),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT url_hash, dim, vector FROM article_embeddings WHERE model = ?",
                    (model,),
                ).fetchall()
        if not rows:
            return None

        best_hash: str | None = None
        best_sim: float = -1.0
        for row in rows:
            stored = np.frombuffer(row["vector"], dtype=np.float32)
            if stored.shape[0] != query.shape[0]:
                continue
            stored_norm = float(np.linalg.norm(stored))
            if stored_norm == 0.0:
                continue
            sim = float(np.dot(query, stored / stored_norm))
            if sim > best_sim:
                best_sim = sim
                best_hash = row["url_hash"]

        if best_hash is None or best_sim < threshold:
            return None
        return best_hash, best_sim

    def find_similar_embeddings(
        self,
        vector: Sequence[float],
        *,
        model: str,
        top_k: int = 10,
        threshold: float = 0.0,
        window_hours: int = 0,
    ) -> list[tuple[str, str, float]]:
        """クエリ embedding に近い上位 K 件を ``(url_hash, url, similarity)`` で返す。

        Phase 2 K1 (セマンティック検索): 死蔵していた ``article_embeddings`` を
        read 経路で活用する。``find_similar_embedding`` (単一 best、dedup hot-path)
        と異なり、検索結果として複数件を類似度降順で返す。

        ``threshold`` 以上のものだけを最大 ``top_k`` 件返す。該当ゼロなら空リスト。
        ``window_hours`` > 0 で過去 N 時間以内に限定 (0=全期間)。

        個人運用規模 (年間数千件) では全件 cosine を numpy でやれば十分速い。
        """
        query = np.asarray(vector, dtype=np.float32)
        if query.ndim != 1:
            raise ValueError("query は 1 次元必須")
        q_norm = float(np.linalg.norm(query))
        if q_norm == 0.0:
            return []
        query = query / q_norm

        with self._connect() as conn:
            if window_hours > 0:
                cutoff = (datetime.now(UTC) - timedelta(hours=window_hours)).isoformat()
                rows = conn.execute(
                    "SELECT url_hash, url, vector FROM article_embeddings "
                    "WHERE model = ? AND created_at >= ?",
                    (model, cutoff),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT url_hash, url, vector FROM article_embeddings WHERE model = ?",
                    (model,),
                ).fetchall()
        if not rows:
            return []

        scored: list[tuple[str, str, float]] = []
        for row in rows:
            stored = np.frombuffer(row["vector"], dtype=np.float32)
            if stored.shape[0] != query.shape[0]:
                continue
            stored_norm = float(np.linalg.norm(stored))
            if stored_norm == 0.0:
                continue
            sim = float(np.dot(query, stored / stored_norm))
            if sim >= threshold:
                scored.append((str(row["url_hash"]), str(row["url"]), sim))

        scored.sort(key=lambda t: t[2], reverse=True)
        return scored[: max(0, top_k)]

    def get_articles_by_urls(self, urls: list[str]) -> dict[str, ArticleRecord]:
        """``url`` → 最新 ``ArticleRecord`` の辞書を返す (semantic search の結果整形用)。

        同一 URL に複数 article 行があれば created_at 最新のものを採用する。
        ``urls`` に対応する article が無いものは辞書に含めない。
        """
        if not urls:
            return {}
        # 重複を畳んでから IN 句に展開 (同 URL が複数回来る可能性に備える)。
        uniq = list(dict.fromkeys(urls))
        placeholders = ", ".join("?" for _ in uniq)
        sql = (
            f"SELECT * FROM articles WHERE url IN ({placeholders}) "  # noqa: S608
            "ORDER BY created_at ASC"
        )
        out: dict[str, ArticleRecord] = {}
        with self._connect() as conn:
            rows = conn.execute(sql, uniq).fetchall()
        # ASC 順に上書きするので、最終的に各 url は最新 (最後の) 行が残る。
        for r in rows:
            rec = _row_to_article(r)
            out[rec.url] = rec
        return out

    def get_embedding(self, url_hash: str) -> NDArray[np.float32] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT vector FROM article_embeddings WHERE url_hash = ?",
                (url_hash,),
            ).fetchone()
        if row is None:
            return None
        return np.frombuffer(row["vector"], dtype=np.float32).copy()

    def count_embeddings(self, model: str | None = None) -> int:
        with self._connect() as conn:
            if model is not None:
                row = conn.execute(
                    "SELECT COUNT(*) AS c FROM article_embeddings WHERE model = ?",
                    (model,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) AS c FROM article_embeddings",
                ).fetchone()
        return int(row["c"])

    def list_urls_missing_embedding(
        self,
        *,
        model: str,
        limit: int = 500,
        since_iso: str | None = None,
    ) -> list[tuple[str, str, str, datetime | None]]:
        """embedding 未生成の (url_hash, url, title, first_seen) を新しい順に返す。

        検索改善 A3 backfill 用。dedup_seen_urls にあり article_embeddings に (該当 model で)
        無く、かつ **articles に実体がある url** のみを対象 (FK 整合は url_hash の存在で保証)。
        記事化されなかった重複 URL を embed しても検索結果に解決しないため除外する
        (vector の top_k 枠を孤児 embedding が食う品質劣化を防ぐ)。
        ``first_seen`` は backfill 時に embedding.created_at へ刻む (now() だと過去記事が一斉に
        「最近」化し dedup 窓が歴史全体を見て過剰 dedup する事故を防ぐ)。
        """
        params: list[object] = [model]
        sql = (
            "SELECT d.url_hash, d.url, COALESCE(d.title, '') AS title, d.first_seen AS first_seen "
            "FROM dedup_seen_urls d "
            "LEFT JOIN article_embeddings e ON e.url_hash = d.url_hash AND e.model = ? "
            "WHERE e.url_hash IS NULL "
            "AND EXISTS (SELECT 1 FROM articles a WHERE a.url = d.url) "
        )
        if since_iso:
            sql += "AND d.first_seen >= ? "
            params.append(since_iso)
        sql += "ORDER BY d.first_seen DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            (str(r["url_hash"]), str(r["url"]), str(r["title"] or ""), _from_iso(r["first_seen"]))
            for r in rows
        ]

    def count_articles_missing_feed_url(self) -> tuple[int, int]:
        """(feed_url 未設定数, 総数) を返す (Stage 2 backfill の被覆確認用)。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(CASE WHEN feed_url IS NULL THEN 1 ELSE 0 END), 0) AS missing, "
                "COUNT(*) AS total FROM articles"
            ).fetchone()
        return int(row["missing"]), int(row["total"])

    def backfill_feed_url_by_title(self, *, feed_title: str, feed_url: str) -> int:
        """feed_url 未設定 (NULL) かつ feed_title 一致の記事に安定キーを後付けする。

        Source Identity Decoupling Stage 2。NULL 行のみ対象なので冪等 (再実行可)。
        返り値は更新行数。
        """
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE articles SET feed_url = ? WHERE feed_title = ? AND feed_url IS NULL",
                (feed_url, feed_title),
            )
            return int(cur.rowcount)

    def purge_old_dedup_entries(self, days: int = 365) -> int:
        """last_seen が N 日より古い dedup エントリを削除し、件数を返す。

        基準は last_seen (= feed から消えて N 日後に忘れる)。first_seen 基準だと
        恒常掲載 URL の記憶が N 日で消えて再投稿される (監査 2026-07-05 P5)。
        ``article_embeddings`` も外部キーカスケードで連動削除される。
        """
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM dedup_seen_urls "
                "WHERE COALESCE(last_seen, first_seen) < datetime('now', ?)",
                (f"-{days} days",),
            )
            return int(cur.rowcount or 0)

    # ---------- 認証の監査証跡 (2026-08-02) ----------

    def record_access_audit(
        self,
        *,
        event: str,
        subject_hash: str = "",
        method: str = "",
        path: str = "",
        client_ip: str = "",
        country: str = "",
        detail: str = "",
        when: datetime | None = None,
    ) -> None:
        """認証イベントを監査証跡へ追記する (append-only)。

        **email は保存しない** (§4)。識別は subject の SHA-256 先頭 12 桁のみ。
        呼び出し側 (middleware) は失敗を握って握り潰さず warning に落とす —
        監査の書き込み失敗で閲覧そのものを止めない (可用性 > 完全な監査、
        ただし stdout ログには必ず出るので二重に失われることはない)。
        """
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO access_audit
                   (at, event, subject_hash, method, path, client_ip, country, detail)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    _to_iso(when or datetime.now(UTC)),
                    event,
                    subject_hash,
                    method,
                    path[:200],
                    client_ip,
                    country,
                    detail[:200],
                ),
            )

    def list_access_audit(self, limit: int = 200) -> list[dict[str, str]]:
        """監査証跡を新しい順に返す (運用画面 / 調査用)。"""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT at, event, subject_hash, method, path, client_ip, country, detail
                   FROM access_audit ORDER BY at DESC LIMIT ?""",
                (int(limit),),
            ).fetchall()
        return [
            {
                "at": str(r["at"]),
                "event": str(r["event"]),
                "subject_hash": str(r["subject_hash"] or ""),
                "method": str(r["method"] or ""),
                "path": str(r["path"] or ""),
                "client_ip": str(r["client_ip"] or ""),
                "country": str(r["country"] or ""),
                "detail": str(r["detail"] or ""),
            }
            for r in rows
        ]

    def purge_old_access_audit(self, days: int = 180) -> int:
        """N 日より古い監査証跡を削除する。

        retention は run_logs (30 日) より長い 180 日 — 監査は「後から遡って
        調べる」ためのもので、事故の発覚が遅れる前提で保持する必要がある。
        """
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM access_audit WHERE at < datetime('now', ?)",
                (f"-{days} days",),
            )
            return int(cur.rowcount or 0)

    def purge_old_logs(self, days: int = DEFAULT_LOG_RETENTION_DAYS) -> int:
        """N 日より古い run_logs を削除し、件数を返す。"""
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM run_logs WHERE ts < datetime('now', ?)",
                (f"-{days} days",),
            )
            return int(cur.rowcount or 0)

    def purge_article_bodies_older_than(self, days: int = 90) -> int:
        """N 日より古い記事の body / body_ja を NULL 化し、件数を返す。

        監査 backlog 2026-07-05: schema コメントに「90 日 retention」と記載しながら
        未実装で articles.body (~3-5KB/件) が無限成長していた。基準は body_fetched_at
        (post 経路の取得時刻)、NULL の行 (旧経路) は created_at。title/summary は保持
        するため検索・digest・履歴表示には影響しない (本文再抽出系のみ対象外になる)。
        body_ja (オンデマンド日本語訳キャッシュ) も同時に消す — 原文なしで訳だけ
        残る不整合を作らないため (2026-07-25)。

        アクター辞書 D4 (2026-07-26): **GC-root 方式の永久保持免除**。永続記録から
        参照される記事の本文は purge しない —「推論の可視化が最終保証」の系 (判断の
        記録が残るのに証拠本文だけ消える検証不能を防ぐ)。root は 5 つ:
          ①主題アクターあり (行動史の構成証拠) ②状況台帳の証拠採用 (判断の検証可能性)
          ③日本標的 (防衛 CTI の任務) ④importance=high (triage 自身の永続価値判定)
          ⑤アクター言及/provisional (辞書の再分析基質)
        実測 (2026-07-26): 本文全体の 25.3%・年間増 ~250MB (和訳込み) で許容。
        判定は purge 時点 (取込後に付く root — 台帳採用・再帰属言及 — も保護される)。
        root 条件は記事単位 (run 横断の全行を見る) — 行単位だと同一記事の行間で
        body の有無が割れる。
        """
        with self._connect() as conn:
            cur = conn.execute(
                # body purge (retention)。body を消したら body_source も 'none' に整合させる
                # (本文完全性の不変条件を purge 後も保つ、2026-07-27)。
                "UPDATE articles SET body=NULL, body_ja=NULL,"
                " body_source='none', extraction_failure_reason=NULL"
                " WHERE (body IS NOT NULL OR body_ja IS NOT NULL) AND ("
                " (body_fetched_at IS NOT NULL"
                "  AND datetime(body_fetched_at) < datetime('now', ?))"
                " OR (body_fetched_at IS NULL AND datetime(created_at) < datetime('now', ?))"
                ")"
                # D4 root ①③④: 記事単位で判定 (同一 article_id の全行を見る)
                " AND NOT EXISTS (SELECT 1 FROM articles root"
                "   WHERE root.article_id = articles.article_id AND ("
                "     (root.subject_actor_ids IS NOT NULL AND root.subject_actor_ids <> '')"
                "     OR root.importance = 'high'"
                "     OR COALESCE(root.victim_country_iso,'') = 'JP'"
                "     OR COALESCE(root.posted_channel,'') = 'japan_watch'))"
                # D4 root ⑤: アクター言及・provisional 言及
                " AND NOT EXISTS (SELECT 1 FROM article_entities ae"
                "   WHERE ae.article_id = articles.article_id"
                "   AND ae.entity_type IN ('actor','actor_provisional'))"
                # D4 root ②: 状況台帳の証拠に採用された記事
                " AND NOT EXISTS (SELECT 1 FROM situation_evidence se"
                "   WHERE se.article_id = articles.article_id)",
                (f"-{days} days", f"-{days} days"),
            )
            return int(cur.rowcount or 0)

    # ----- CVE 正規化 dedup (Phase 5T-V-2) -----

    def find_recent_post_by_cve(
        self,
        cve_id: str,
        *,
        within_hours: int = 48,
        now: datetime | None = None,
    ) -> ArticleRecord | None:
        """直近 ``within_hours`` 以内に同 CVE で投稿された article を返す。

        LLM 生成 dedup_key が同事象でばらつく問題への対処 (Phase 5T-V-2)。
        正規化 CVE-ID で 48h 以内の重複を強制 skip する。
        48h 超えは「続報 (KEV 追加等)」として許容する仕様 (memory:
        [[dedup_window_48h_design_intent]] と整合)。

        Args:
            cve_id: 正規化 CVE-ID (``cve-YYYY-NNNN`` 形式)
            within_hours: 振り返り時間 (default 48h)
            now: 基準時刻 (テスト用)

        Returns:
            48h 以内の同 CVE post (最新 1 件) or None。
        """
        if not cve_id:
            return None
        base = now or datetime.now(UTC)
        since = base - timedelta(hours=within_hours)
        # dedup_key 内 substring match (cve-YYYY-NNNN は suffix ありでも先頭一致)
        # SQLite LIKE は escape 不要 (cve-YYYY-NNNN 部分のみ)
        pattern_exact = cve_id
        pattern_prefix = f"{cve_id}-%"
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM articles
                 WHERE created_at >= ?
                   AND status = 'posted'
                   AND (dedup_key = ? OR dedup_key LIKE ?)
                 ORDER BY created_at DESC
                 LIMIT 1
                """,
                (_to_iso(since), pattern_exact, pattern_prefix),
            ).fetchone()
        if row is None:
            return None
        return _row_to_article(row)

    # ----- brief cap (Phase 5T-V) -----

    def count_brief_in_window(
        self,
        *,
        hours: int = 24,
        now: datetime | None = None,
    ) -> int:
        """直近 ``hours`` 以内に brief ch へ投稿された article 件数を返す。

        R6.cap_demote (brief 24h cap) の判定に使う。重複排除済 (article_id 一意)。

        Args:
            hours: 振り返り時間 (例: 24 = 過去 24 時間)
            now: 基準時刻 (テスト用に注入可能)
        """
        base = now or datetime.now(UTC)
        since = base - timedelta(hours=hours)
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(DISTINCT article_id) AS n FROM articles
                 WHERE created_at >= ?
                   AND status = 'posted'
                   AND posted_channel = 'brief'
                """,
                (_to_iso(since),),
            ).fetchone()
        return int(row["n"]) if row and row["n"] is not None else 0
