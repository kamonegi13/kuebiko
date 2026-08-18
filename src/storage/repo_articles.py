"""articles / article_entities / editorial_stance のリポジトリメソッド (run_history 分割)。"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

from src.storage.records import ArticleRecord
from src.storage.repo_base import RunHistoryRepositoryBase
from src.storage.row_mappers import _row_to_article, _to_iso

# 表示行の選択順 (2026-08-06 幽霊アクター監査の併発是正): 同一 article_id の複数 run 行
# から UI/翻訳/再エンリッチが読むべき 1 行を決める。毎時 dedup の再観測行
# (status='skipped_duplicate'、summary NULL・タイトルは毎時再翻訳で揺れる) を避け、
# 実取込行 (posted 等) の最新を優先する。CASE 式は SQLite/PG 両方言で可搬。
_DISPLAY_ROW_ORDER = "CASE WHEN status='skipped_duplicate' THEN 1 ELSE 0 END ASC, created_at DESC"


class ArticlesMixin(RunHistoryRepositoryBase):
    # ----- articles -----

    def add_article(self, record: ArticleRecord) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO articles
                  (run_id, article_id, title, url, feed_title, feed_url,
                   importance, category, status, failure_reason,
                   posted_channel, duration_seconds, dedup_key,
                   discord_message_id, discord_channel_id, summary,
                   pmesii_p, pmesii_m, pmesii_e, pmesii_s,
                   pmesii_i_infra, pmesii_i_cyber, pmesii_p_env, pmesii_t,
                   victim_sector_canonical, victim_sector_raw,
                   victim_country_iso, victim_country_raw, victim_country_scope,
                   is_ransomware,
                   socio_political_intent, intent_confidence, remediation, analyst_note,
                   socio_political_rationale,
                   technical_axis_summary,
                   editorial_stance, routing_rule_id, routing_reason,
                   published_at, event_date, event_date_basis, compromise_date,
                   subject_actor_ids, subject_actor_source, subject_actor_confidence,
                   llm_primary_actor_raw, llm_primary_confidence,
                   subject_actor_rationale,
                   article_type, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.run_id,
                    record.article_id,
                    record.title,
                    record.url,
                    record.feed_title,
                    record.feed_url,
                    record.importance,
                    record.category,
                    record.status,
                    record.failure_reason,
                    record.posted_channel,
                    record.duration_seconds,
                    record.dedup_key,
                    record.discord_message_id,
                    record.discord_channel_id,
                    record.summary,
                    int(record.pmesii_p),
                    int(record.pmesii_m),
                    int(record.pmesii_e),
                    int(record.pmesii_s),
                    int(record.pmesii_i_infra),
                    int(record.pmesii_i_cyber),
                    int(record.pmesii_p_env),
                    int(record.pmesii_t),
                    record.victim_sector_canonical,
                    record.victim_sector_raw,
                    record.victim_country_iso,
                    record.victim_country_raw,
                    record.victim_country_scope,
                    int(record.is_ransomware),
                    record.socio_political_intent,
                    record.intent_confidence,
                    record.remediation,
                    record.analyst_note,
                    record.socio_political_rationale,
                    record.technical_axis_summary,
                    record.editorial_stance,
                    record.routing_rule_id,
                    record.routing_reason,
                    _to_iso(record.published_at) if record.published_at else None,
                    record.event_date,
                    record.event_date_basis,
                    record.compromise_date,
                    record.subject_actor_ids,
                    record.subject_actor_source,
                    record.subject_actor_confidence,
                    record.llm_primary_actor_raw,
                    record.llm_primary_confidence,
                    record.subject_actor_rationale,
                    record.article_type,
                    _to_iso(record.created_at),
                ),
            )
            assert cur.lastrowid is not None
            return int(cur.lastrowid)

    # ---------- Phase Diamond L2: article body 永続化 ----------

    def update_article_body(
        self,
        article_id: str,
        body: str,
        *,
        source: str | None = None,
        failure_reason: str | None = None,
        fetched_at: datetime | None = None,
    ) -> int:
        """article の body カラムを更新 (backfill / post-time / 再取得 共通の唯一の seam)。

        ``source`` = body の由来 (full_extract/playwright_extract/prefetch/scraper/grok/
        feed_summary/none)。**production の全 body 書込は source を明示すること** —
        body 非 NULL ⇒ body_source 非 NULL の不変条件を保つ (test_article_body_source_seam)。
        ``source=None`` の場合は body_source 列を触らない (旧テスト互換)。
        ``failure_reason`` = feed 抜粋へ fallback した理由 (source 指定時のみ反映)。
        """
        ts = _to_iso(fetched_at or datetime.now(UTC))
        with self._connect() as conn:
            if source is None:
                cur = conn.execute(
                    """UPDATE articles SET body=?, body_fetched_at=?
                       WHERE article_id=?""",
                    (body, ts, article_id),
                )
            else:
                cur = conn.execute(
                    """UPDATE articles
                       SET body=?, body_fetched_at=?,
                           body_source=?, extraction_failure_reason=?
                       WHERE article_id=?""",
                    (body, ts, source, failure_reason, article_id),
                )
            return int(cur.rowcount or 0)

    def mark_extraction_state(
        self,
        article_id: str,
        *,
        body_source: str,
        failure_reason: str | None = None,
    ) -> int:
        """body を持たない記事に body_source (失敗状態) + reason を記録する (B3a)。

        抽出失敗 (block_page / body_too_short) で body が NULL のまま INSERT された行を
        可視化する seam。body は触らない (NULL のまま) ため body 非 NULL ⇒ body_source 非 NULL
        の不変条件に抵触しない。``WHERE body IS NULL`` ガードで既存本文の source は上書きしない。
        """
        with self._connect() as conn:
            cur = conn.execute(
                """UPDATE articles
                   SET body_source=?, extraction_failure_reason=?
                   WHERE article_id=? AND body IS NULL""",
                (body_source, failure_reason, article_id),
            )
            return int(cur.rowcount or 0)

    def record_refetch_failure(self, article_id: str, *, reason: str | None = None) -> int:
        """再取得失敗時に試行回数を +1 し失敗理由を記録する (B3b)。

        body は触らない (切り株/NULL のまま)。refetch_attempts が上限
        (list_articles_needing_refetch の max_attempts) に達すると再取得キューから除外され、
        reason 一致では救えない WAF/timeout の無限リトライを止める。再取得キュー対象
        (body NULL or feed_summary) のみ更新し、full_extract を誤って触らない。
        """
        with self._connect() as conn:
            cur = conn.execute(
                """UPDATE articles
                   SET refetch_attempts = refetch_attempts + 1,
                       extraction_failure_reason = ?
                   WHERE article_id = ?
                     AND (body IS NULL OR body_source = 'feed_summary')""",
                (reason, article_id),
            )
            return int(cur.rowcount or 0)

    def list_articles_missing_body(
        self,
        limit: int = 100,
        since_iso: str | None = None,
    ) -> list[tuple[str, str]]:
        """body が NULL の article (id, url) を返す (backfill loop 用)。

        ``since_iso`` を渡せば該当日時以降の article のみ対象 (古すぎる URL は除外)。
        """
        params: list[object] = []
        sql = "SELECT article_id, url FROM articles WHERE body IS NULL"
        if since_iso:
            sql += " AND created_at >= ?"
            params.append(since_iso)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(int(limit))
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [(str(r["article_id"]), str(r["url"])) for r in rows]

    def get_article_body(self, article_id: str) -> str | None:
        """1 article の body を返す (entity 再抽出用)。表示行 (_DISPLAY_ROW_ORDER) 基準。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT body FROM articles WHERE article_id=?"
                f" ORDER BY {_DISPLAY_ROW_ORDER} LIMIT 1",
                (article_id,),
            ).fetchone()
        if row is None or row["body"] is None:
            return None
        return str(row["body"])

    # ---------- 記事詳細 UI: 本文オンデマンド日本語訳キャッシュ (2026-07-25) ----------

    def get_article_body_ja(self, article_id: str) -> str | None:
        """1 article の日本語訳キャッシュ (body_ja) を返す。未訳なら None。

        body_ja は article_id 全行に UPDATE されるため通常は行間で一致するが、
        読み取りも表示行 (_DISPLAY_ROW_ORDER) に固定して非決定性を排除する。
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT body_ja FROM articles WHERE article_id=?"
                f" ORDER BY {_DISPLAY_ROW_ORDER} LIMIT 1",
                (article_id,),
            ).fetchone()
        if row is None or row["body_ja"] is None:
            return None
        return str(row["body_ja"])

    def update_article_body_ja(self, article_id: str, body_ja: str) -> int:
        """日本語訳キャッシュを保存する (再翻訳時は上書き)。"""
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE articles SET body_ja=? WHERE article_id=?",
                (body_ja, article_id),
            )
            return int(cur.rowcount or 0)

    def clear_article_body_ja(self, article_id: str) -> int:
        """日本語訳キャッシュを無効化 (body 差し替え後の再翻訳のため、2026-07-27)。

        NULL に戻すと毎時バックログ翻訳ジョブ (list_articles_untranslated) が再訳する。
        """
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE articles SET body_ja=NULL WHERE article_id=?",
                (article_id,),
            )
            return int(cur.rowcount or 0)

    # 再取得後の再処理で更新してよい分析列の allowlist (2026-07-27)。任意文字列を SQL に
    # 流さないため、更新対象カラムはこの集合でホワイトリスト検証する。
    _REPROCESS_UPDATABLE_COLUMNS: frozenset[str] = frozenset(
        {
            "summary",
            "editorial_stance",
            "socio_political_intent",
            "intent_confidence",
            "socio_political_rationale",
            "technical_axis_summary",
            "remediation",
            "analyst_note",
            "event_date",
            "event_date_basis",
            "compromise_date",
            "is_ransomware",
            "article_type",
            "llm_primary_actor_raw",
            "llm_primary_confidence",
            "pmesii_p",
            "pmesii_m",
            "pmesii_e",
            "pmesii_s",
            "pmesii_i_infra",
            "pmesii_i_cyber",
            "pmesii_p_env",
            "pmesii_t",
            "victim_sector_canonical",
            "victim_sector_raw",
            "victim_country_iso",
            "victim_country_raw",
            "victim_country_scope",
        }
    )

    def update_article_enrichment(self, article_id: str, fields: dict[str, object]) -> int:
        """再取得後の再処理で分析列を上書きする (run 横断の全行、2026-07-27)。

        ``fields`` のキーは ``_REPROCESS_UPDATABLE_COLUMNS`` の allowlist で検証する
        (subject 列は update_subject_actor_fields、body は update_article_body が担当)。
        """
        cols = [c for c in fields if c in self._REPROCESS_UPDATABLE_COLUMNS]
        if not cols:
            return 0
        set_clause = ", ".join(f"{c}=?" for c in cols)
        params = [fields[c] for c in cols]
        params.append(article_id)
        with self._connect() as conn:
            cur = conn.execute(
                f"UPDATE articles SET {set_clause} WHERE article_id=?",  # noqa: S608 — cols allowlisted
                params,
            )
            return int(cur.rowcount or 0)

    def list_articles_untranslated(self, limit: int = 40) -> list[str]:
        """body ありで未訳の article_id を新しい順に返す (バックログ翻訳ジョブ用)。

        新しい順 = 新規流入を優先して当日中に訳し切り、過去分は後ろから漸進消化する。
        body_ja: NULL=未処理 / ''=処理済・訳不要 (原文が日本語) / それ以外=訳キャッシュ。
        同一 article_id は run 横断で複数行あり得るため GROUP BY で重複排除する
        (重複を許すと 1 バッチ内で同じ記事を二重翻訳する)。
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT article_id, MAX(created_at) AS latest FROM articles"
                " WHERE body IS NOT NULL AND body != '' AND body_ja IS NULL"
                " GROUP BY article_id ORDER BY latest DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [str(r["article_id"]) for r in rows]

    def entity_keys_for_articles(
        self, article_ids: list[str], *, types: tuple[str, ...] | None = None
    ) -> dict[str, set[str]]:
        """複数 article の entity を ``{article_id: {"type:value", ...}}`` で一括取得。

        grounded synthesis のクラスタリング (各 claim の裏取りをプールから拡張) 用。
        ``types`` 指定で entity_type を絞る。空入力は空 dict。
        """
        if not article_ids:
            return {}
        aph = ",".join("?" for _ in article_ids)
        sql = f"SELECT article_id, entity_type, value FROM article_entities WHERE article_id IN ({aph})"  # noqa: E501
        params: list[object] = list(article_ids)
        if types:
            tph = ",".join("?" for _ in types)
            sql += f" AND entity_type IN ({tph})"
            params.extend(types)
        out: dict[str, set[str]] = {}
        with self._connect() as conn:
            for r in conn.execute(sql, params).fetchall():
                out.setdefault(str(r["article_id"]), set()).add(f"{r['entity_type']}:{r['value']}")
        return out

    def articles_for_entity_keys(
        self,
        entity_keys: set[str],
        *,
        since: datetime,
        before: datetime,
        limit: int = 10,
    ) -> list[str]:
        """``"type:value"`` entity を共有する記事 id を時間窓 [since, before) で新しい順に返す。

        grounded synthesis の**過去文脈 retrieval** 用: 現在の判定の anchor entity
        (actor/CVE/malware/被害組織) を持つ**対象期間より前**の記事を ACH 証拠に引く
        (パターン/前例/新規性/再報道の判定)。before に対象期間の開始を渡せば現在プールと重複しない。
        """
        split = [k.split(":", 1) for k in entity_keys if ":" in k]
        pairs: list[tuple[str, str]] = [(p[0], p[1]) for p in split if p[0] and p[1]]
        if not pairs:
            return []
        clause = " OR ".join("(ae.entity_type=? AND ae.value=?)" for _ in pairs)
        flat: list[object] = []
        for t, v in pairs:
            flat.extend((t, v))
        sql = (  # noqa: S608 — clause は ? 固定、値はパラメータバインド
            "SELECT ae.article_id AS aid, MAX(a.created_at) AS ts "
            "FROM article_entities ae JOIN articles a ON a.article_id = ae.article_id "
            "WHERE a.created_at >= ? AND a.created_at < ? AND (" + clause + ") "
            "GROUP BY ae.article_id ORDER BY ts DESC LIMIT ?"
        )
        params = [_to_iso(since), _to_iso(before), *flat, limit]
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [str(r["aid"]) for r in rows]

    def get_article_grounding(self, article_id: str) -> dict[str, Any] | None:
        """grounded synthesis の証拠接地用に 1 article の本文系を返す。

        body 優先、無ければ summary を fallback (grounded フィールドで区別)。feed メタは
        source tier 判定 (classify_source_tier) に使う。created_at 最新を採る。
        """
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT title, feed_title, feed_url, summary, body,
                       created_at, event_date, event_date_basis
                  FROM articles WHERE article_id=? ORDER BY created_at DESC LIMIT 1
                """,
                (article_id,),
            ).fetchone()
        if row is None:
            return None
        body = row["body"]
        summary = row["summary"]
        return {
            "article_id": article_id,
            "title": str(row["title"] or ""),
            "feed_title": str(row["feed_title"] or ""),
            "feed_url": str(row["feed_url"] or ""),
            "text": str(body or summary or ""),
            "grounded": bool(body),  # True=本文、False=summary fallback
            # 時系列 (発生日時の前後関係) を接地 ACH に供給 (因果は断定しない)。
            "created_at": str(row["created_at"] or ""),
            "event_date": str(row["event_date"] or ""),
            "event_date_basis": str(row["event_date_basis"] or ""),
        }

    def get_article(self, article_id: str) -> ArticleRecord | None:
        """``article_id`` で 1 article を取得 (deep-view 用、Phase 2 K4)。

        同一 article_id が複数 run にまたがる場合は表示行 (_DISPLAY_ROW_ORDER =
        skipped_duplicate 以外を優先 → 最新) を返す。旧実装の「無条件に最新」は
        毎時 dedup の再観測行 (summary NULL・再翻訳タイトル) を表示してしまい、
        680 記事で要約欄が消える実害があった (幽霊アクター監査 2026-08-06)。
        """
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT * FROM articles WHERE article_id=? ORDER BY {_DISPLAY_ROW_ORDER} LIMIT 1",
                (article_id,),
            ).fetchone()
        return _row_to_article(row) if row else None

    def iter_articles_with_body(
        self,
        batch_size: int = 200,
        since_iso: str | None = None,
    ) -> Iterator[tuple[str, str, str | None, str | None]]:
        """body 付きの article を ``(article_id, title, summary, body)`` で yield。

        entity 再抽出 batch 用。``since_iso`` を渡せば該当日時以降のみ。
        """
        params: list[object] = []
        sql = "SELECT article_id, title, summary, body FROM articles WHERE body IS NOT NULL"
        if since_iso:
            sql += " AND created_at >= ?"
            params.append(since_iso)
        sql += " ORDER BY created_at DESC"
        with self._connect() as conn:
            cur = conn.execute(sql, params)
            while True:
                rows = cur.fetchmany(batch_size)
                if not rows:
                    break
                for r in rows:
                    yield (
                        str(r["article_id"]),
                        str(r["title"] or ""),
                        str(r["summary"]) if r["summary"] else None,
                        str(r["body"]) if r["body"] else None,
                    )

    # ---------- Phase Diamond: article_entities (IoC / TTP / CVE / malware / tool) ----------

    def add_article_entities(
        self,
        article_id: str,
        entities: list[tuple[str, str]],
        *,
        when: datetime | None = None,
    ) -> int:
        """1 article に紐づく entity 群 (type, value) を一括 upsert。

        UNIQUE 制約 (article_id, entity_type, value) で同一エントリは無視 (冪等)。
        戻り値は新規追加された行数。``when`` で created_at を上書き可 (backfill / test 用)。
        """
        if not entities:
            return 0
        now_iso = _to_iso(when or datetime.now(UTC))
        rows = [(article_id, str(t), str(v), now_iso) for t, v in entities if v]
        if not rows:
            return 0
        with self._connect() as conn:
            cur = conn.executemany(
                """INSERT OR IGNORE INTO article_entities
                     (article_id, entity_type, value, created_at)
                   VALUES (?, ?, ?, ?)""",
                rows,
            )
            return int(cur.rowcount or 0)

    def delete_article_entities(self, article_id: str, entity_types: list[str]) -> int:
        """指定 type の entity を全削除する (再取得後の再処理で replace するため、2026-07-27)。

        add-only の add_article_entities では過去の誤 mention (切り株由来) が残り続けるため、
        再抽出前に対象 type を消してから入れ直す (承認時再帰属の add-only 残存問題の反省)。
        """
        if not entity_types:
            return 0
        ph = ",".join("?" for _ in entity_types)
        with self._connect() as conn:
            cur = conn.execute(
                f"DELETE FROM article_entities WHERE article_id=? AND entity_type IN ({ph})",  # noqa: S608
                (article_id, *entity_types),
            )
            return int(cur.rowcount or 0)

    def list_articles_needing_refetch(
        self,
        limit: int = 40,
        *,
        permanent_reasons: tuple[str, ...] = (
            "paywall_suspected",
            "content_too_short",
            "http_error_404",
            "http_error_401",
            "http_error_410",
            "unsafe_url",
        ),
        max_attempts: int = 3,
        retention_days: int = 90,
    ) -> list[tuple[str, str]]:
        """全文再取得すべき記事 (article_id, url) を返す (2026-07-27, A4)。

        対象 = ①body NULL (未取得) または ②body_source='feed_summary' (切り株) で失敗理由が
        **恒久系でない** もの。恒久 blacklist 方式にすることで、①UA 修正前の既存切り株
        (heuristic 分類で failure_reason=NULL) と ②新規切り株 (403/timeout 等) の両方を拾い、
        paywall/404 等の再取得しても無駄な恒久失敗のみ除外する。高 importance・新しい順。
        同一 article_id は run 横断で複数行あるため GROUP BY で重複排除する。

        ⚠ ``body_source='none'`` は **2 つの状態を兼ねている** (2026-08-15 に判明):
        「一度も本文が取れなかった (extract_failed)」と「90 日 retention で purge した」。
        purge も body_source を 'none' に整合させるため、一律除外すると **抽出失敗した
        記事が永久に再取得されない** (実測 158 件が滞留)。retention 期間内 = purge され
        得ない、を使って前者だけを対象に戻す。
        """
        perm_ph = ",".join("?" for _ in permanent_reasons)
        sql = (
            # MAX(CASE ...) で高 importance 優先 (PG は MAX(boolean) 不可のため CASE で整数化)。
            "SELECT article_id, MIN(url) AS url, "
            "MAX(CASE WHEN importance='high' THEN 1 ELSE 0 END) AS is_high, "
            "MAX(created_at) AS latest FROM articles "
            "WHERE url IS NOT NULL AND url <> '' "
            # grok/ransomware.live は URL 直取得しない (grok=JSONL / ransomware=構造化 API)。
            # B3b: feed_title 'ransomware.live'(小文字)が旧 'Ransomware.live' と case 不一致で
            # 除外が効かず 1400+ 件が毎時リトライされていた → LOWER() で修正。
            "AND (feed_title IS NULL OR LOWER(feed_title) NOT IN ('grok','ransomware.live')) "
            # B3b: 試行回数上限に達した恒久失敗を除外 (WAF/timeout の無限 churn 停止)。
            "AND refetch_attempts < ? "
            "AND ("
            # NULL body = 未取得。blocked (WAF/bot 壁) は再取得しない。
            "  (body IS NULL AND (body_source IS NULL OR body_source <> 'blocked') "
            # 'none' は「未取得」と「purge 済」の両方を指すため、purge され得ない
            # retention 期間内のものだけ再取得する (古い purge 済を掘り起こさない)。
            "   AND (body_source <> 'none' OR created_at >= datetime('now', ?))) "
            "  OR (body_source='feed_summary' AND ("
            "       extraction_failure_reason IS NULL "
            f"       OR extraction_failure_reason NOT IN ({perm_ph})"  # noqa: S608
            "  ))"
            ") "
            "GROUP BY article_id ORDER BY is_high DESC, latest DESC LIMIT ?"
        )
        with self._connect() as conn:
            rows = conn.execute(
                sql,
                (
                    int(max_attempts),
                    f"-{int(retention_days)} days",
                    *permanent_reasons,
                    int(limit),
                ),
            ).fetchall()
        return [(str(r["article_id"]), str(r["url"])) for r in rows]

    def get_entities_by_article(self, article_id: str) -> list[tuple[str, str]]:
        """article に紐づく (entity_type, value) のリスト。"""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT entity_type, value FROM article_entities
                   WHERE article_id=? ORDER BY entity_type, value""",
                (article_id,),
            ).fetchall()
        return [(str(r["entity_type"]), str(r["value"])) for r in rows]

    def count_entities_for_articles(
        self,
        article_ids: list[str],
        entity_types: list[str] | None = None,
    ) -> dict[str, dict[str, int]]:
        """与えた article_ids にまたがる entity の出現回数を ``{type: {value: count}}`` で返す。

        threat_operations から actor 別 top_iocs / top_ttps 等を集計するのに使う。
        """
        if not article_ids:
            return {}
        placeholders = ",".join("?" * len(article_ids))
        sql = (
            "SELECT entity_type, value, COUNT(*) AS n FROM article_entities "
            f"WHERE article_id IN ({placeholders})"
        )
        params: list[str] = list(article_ids)
        if entity_types:
            sql += f" AND entity_type IN ({','.join('?' * len(entity_types))})"
            params.extend(entity_types)
        sql += " GROUP BY entity_type, value"
        result: dict[str, dict[str, int]] = {}
        with self._connect() as conn:
            for row in conn.execute(sql, params):
                t, v, n = str(row["entity_type"]), str(row["value"]), int(row["n"])
                result.setdefault(t, {})[v] = n
        return result

    def find_articles_by_entity(
        self,
        entity_type: str,
        value: str,
        *,
        since_iso: str | None = None,
        limit: int = 500,
    ) -> list[str]:
        """与えた (entity_type, value) を持つ article_id のリストを返す (逆引き Phase 4)。"""
        sql = "SELECT article_id FROM article_entities WHERE entity_type=? AND value=?"
        params: list[object] = [entity_type, value]
        if since_iso:
            sql += " AND created_at >= ?"
            params.append(since_iso)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(int(limit))
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [str(r["article_id"]) for r in rows]

    def find_recent_article_by_dedup_key(
        self,
        dedup_key: str,
        *,
        within_hours: int,
    ) -> ArticleRecord | None:
        """直近 ``within_hours`` 以内に同 dedup_key で投稿成功した article を返す。

        ch 横断 dedup の判定用 (Phase 5L-4)。投稿失敗 / dedup 自体での skip は
        対象外 (失敗側はリトライ余地、skip 側は重ねて抑制不要)。
        """
        if not dedup_key:
            return None
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM articles
                 WHERE dedup_key = ?
                   AND status = 'posted'
                   AND created_at >= datetime('now', ?)
                 ORDER BY created_at DESC
                 LIMIT 1
                """,
                (dedup_key, f"-{within_hours} hours"),
            ).fetchone()
        if row is None:
            return None
        return _row_to_article(row)

    def dedup_key_exists(self, dedup_key: str) -> bool:
        """``dedup_key`` を持つ article が status を問わず既存か。

        被害状況コレクタ (ransomware.live 等) の取込冪等性に使う。
        ``find_recent_article_by_dedup_key`` と違い ``status='posted'`` に限定せず、
        'collected' 等も含めて全期間で照合する。
        """
        if not dedup_key:
            return False
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM articles WHERE dedup_key = ? LIMIT 1",
                (dedup_key,),
            ).fetchone()
        return row is not None

    def news_victim_org_dates(self) -> list[tuple[str, Any]]:
        """ニュース等 (ransomware.live 以外) の posted 記事の (victim_org小文字, 実効日) を返す。

        被害状況コレクタのクロスソース重複判定 (ニュースが同一被害組織を扱っていれば
        ransomware.live 側を重複扱い) に使う。実効日 = COALESCE(published_at, created_at)。
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT LOWER(ae.value) AS org, "
                "COALESCE(a.published_at, a.created_at) AS d "
                "FROM article_entities ae JOIN articles a ON a.article_id = ae.article_id "
                "WHERE ae.entity_type='victim_org' AND a.status='posted' "
                "AND (a.feed_title IS NULL OR a.feed_title <> 'ransomware.live')"
            ).fetchall()
        return [(str(r["org"]), r["d"]) for r in rows]

    def collected_ransomware_for_reconcile(self) -> list[tuple[int, str, Any]]:
        """ransomware.live の status='collected' 記事の (id, victim_org小文字, 実効日) を返す。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT a.id AS id, LOWER(ae.value) AS org, "
                "COALESCE(a.published_at, a.created_at) AS d "
                "FROM articles a JOIN article_entities ae ON a.article_id = ae.article_id "
                "WHERE a.feed_title='ransomware.live' AND a.status='collected' "
                "AND ae.entity_type='victim_org'"
            ).fetchall()
        return [(int(r["id"]), str(r["org"]), r["d"]) for r in rows]

    def set_article_status(self, article_ids: list[int], status: str) -> int:
        """指定 id 群の status を一括更新する (重複マーク等)。更新件数を返す。"""
        if not article_ids:
            return 0
        with self._connect() as conn:
            for aid in article_ids:
                conn.execute("UPDATE articles SET status = ? WHERE id = ?", (status, aid))
        return len(article_ids)

    def ransomware_live_group_names(self) -> list[str]:
        """ransomware.live 由来記事の攻撃グループの**散文名**を返す。自動ランサム辞書の素。

        2026-07-27: 保存値 (canonical id) を辞書の **canonical 表示名** に展開して返す。
        entity value は canonical id (`akira_ransom` 等アンダースコア形) で、これは news 散文
        "Akira" に語境界一致しない — 保存値をそのまま find_mention に渡すと単トークン系の自動
        タグが沈黙する (R2 以降の既存バグ + mention 正規化 backfill で顕在化)。辞書の canonical
        表示名 (prose) に開くことで散文照合を回復する。未解決の値は生値のまま (後方互換)。
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT LOWER(ae.value) AS grp "
                "FROM article_entities ae JOIN articles a ON a.article_id = ae.article_id "
                "WHERE ae.entity_type='actor' AND a.feed_title='ransomware.live'"
            ).fetchall()
        raw_values = [str(r["grp"]) for r in rows if r["grp"]]

        from src.cti.actor_normalizer import load_actor_aliases

        registry = load_actor_aliases()
        names: set[str] = set()
        for value in raw_values:
            entry = registry.resolve_source_slug(value) or registry.by_id(
                registry.resolve_actor_id(value)
            )
            if entry is not None:
                names.add(entry.canonical)  # 散文名 (例 "Akira" / "The Gentlemen")
            names.add(value)  # 生値も残す (辞書外グループ・後方互換)
        return sorted(names)

    def sample_healthy_source_urls(self, limit: int = 8) -> list[str]:
        """直近 full 取得できたドメインを代表する URL を返す (UA 自己修復の canary、2026-07-27)。

        body_source が全文系 (full_extract/playwright_extract) の直近記事から、feed_title
        (=source) ごとに最新 1 件の URL を採る。UA 健全性を「取れていたサイトで今も取れるか」で
        判定するための標本。RSS 収集経路のみ (ransomware.live/grok は URL 直取得しないため除外。
        feed_title は取込元により大小混在するため LOWER 比較で除外する)。

        「グループごとに最新 1 行」は ROW_NUMBER 窓関数で表現する。``SELECT url, MAX(created_at)
        … GROUP BY feed_title`` は SQLite の bare column 拡張でしか通らず、PG では
        GroupingError で落ちる (production 全滅の実績あり、2026-08-04 修正)。
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT url FROM ("
                " SELECT url, created_at,"
                " ROW_NUMBER() OVER (PARTITION BY feed_title ORDER BY created_at DESC) AS rn"
                " FROM articles"
                " WHERE body_source IN ('full_extract','playwright_extract')"
                " AND url IS NOT NULL AND url <> ''"
                " AND feed_title IS NOT NULL"
                " AND LOWER(feed_title) NOT IN ('grok','ransomware.live')"
                ") t WHERE rn = 1 ORDER BY created_at DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [str(r["url"]) for r in rows if r["url"]]

    def untagged_cyber_news(self, *, since_iso: str | None) -> list[tuple[int, str, str]]:
        """is_ransomware=0 の cyber ニュース (ransomware.live 以外) の (id, title, summary)。

        自動ランサム辞書で title+summary を照合し is_ransomware を後付けするための候補。
        cyber category に限定し geopolitical/policy 等での group 名誤爆を避ける。
        """
        sql = (
            "SELECT id, title, COALESCE(summary, '') AS summary FROM articles "
            "WHERE is_ransomware=0 AND status IN ('posted', 'collected') "
            "AND (feed_title IS NULL OR feed_title <> 'ransomware.live') "
            "AND category IN ('breach','incident','apt','apt_leak','malware','phishing')"
        )
        params: list[Any] = []
        if since_iso is not None:
            sql += " AND COALESCE(datetime(published_at), datetime(created_at)) >= datetime(?)"
            params.append(since_iso)
        with self._connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [(int(r["id"]), str(r["title"] or ""), str(r["summary"] or "")) for r in rows]

    def set_is_ransomware(self, article_ids: list[int]) -> int:
        """指定 id 群の is_ransomware を 1 に一括更新する。更新件数を返す。"""
        if not article_ids:
            return 0
        with self._connect() as conn:
            for aid in article_ids:
                conn.execute("UPDATE articles SET is_ransomware = 1 WHERE id = ?", (aid,))
        return len(article_ids)

    # ----- Phase B-R5b 観察: editorial_stance reviews -----

    def upsert_editorial_stance_review(
        self,
        *,
        article_id: str,
        original_stance: str | None,
        corrected_stance: str,
        reviewer: str = "ui",
        comment: str = "",
    ) -> None:
        """analyst による editorial_stance 誤分類フラグを記録。

        同 article_id を後から訂正できるよう UNIQUE(article_id) で upsert。
        """
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO editorial_stance_reviews
                  (article_id, original_stance, corrected_stance, reviewer, comment)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(article_id) DO UPDATE SET
                  original_stance=excluded.original_stance,
                  corrected_stance=excluded.corrected_stance,
                  reviewer=excluded.reviewer,
                  comment=excluded.comment,
                  created_at=datetime('now')
                """,
                (article_id, original_stance, corrected_stance, reviewer, comment),
            )

    def update_article_editorial_stance(self, article_id: str, stance: str) -> int:
        """analyst 訂正を articles 本体へ還流する (2026-07-31 運用レビュー完全調査)。

        訂正がレビュー表示専用テーブルに留まると、同じ画面のクロス集計 (articles 集計)
        と矛盾したまま = write-only になるため、訂正保存時に本体列も更新する。
        """
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE articles SET editorial_stance=? WHERE article_id=?",
                (stance, article_id),
            )
            return int(cur.rowcount or 0)

    def count_editorial_stance_by_feed(
        self,
        *,
        lookback_days: int = 30,
    ) -> list[dict[str, object]]:
        """feed_title × editorial_stance のクロス集計 (lookback_days 内 posted)。

        UI 観察ページで feed ごとの判定分布を表示するため。
        """
        with self._connect() as conn:
            rows = conn.execute(
                """
                -- Source Identity Decoupling #2: 安定キー feed_url で集計 (改名で割れない)。
                -- 表示は代表 feed_title (url↔title は実質 1:1)。旧記事は feed_title fallback。
                SELECT MAX(feed_title) AS feed_title,
                       COALESCE(editorial_stance, 'unknown') AS stance,
                       COUNT(*) AS n
                  FROM articles
                 WHERE status='posted'
                   AND datetime(created_at) >= datetime('now', ?)
                 GROUP BY COALESCE(NULLIF(feed_url, ''), feed_title), stance
                 ORDER BY MAX(feed_title), stance
                """,
                (f"-{lookback_days} days",),
            ).fetchall()
        return [
            {"feed_title": r["feed_title"] or "", "stance": r["stance"], "n": int(r["n"])}
            for r in rows
        ]

    def list_recent_articles_with_stance(
        self,
        *,
        stance_filter: str | None = None,
        feed_filter: str | None = None,
        lookback_days: int = 14,
        limit: int = 200,
    ) -> list[ArticleRecord]:
        """analyst レビュー用に最近の posted article を返す (filter 可能)。"""
        clauses = [
            "status='posted'",
            "datetime(created_at) >= datetime('now', ?)",
        ]
        params: list[object] = [f"-{lookback_days} days"]
        if stance_filter:
            clauses.append("editorial_stance = ?")
            params.append(stance_filter)
        if feed_filter:
            clauses.append("feed_title = ?")
            params.append(feed_filter)
        where = " AND ".join(clauses)
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM articles WHERE {where} "  # noqa: S608
                "ORDER BY datetime(created_at) DESC LIMIT ?",
                params,
            ).fetchall()
        return [_row_to_article(r) for r in rows]

    def get_editorial_stance_review(
        self,
        article_id: str,
    ) -> dict[str, object] | None:
        """指定 article_id の analyst レビュー情報を返す。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM editorial_stance_reviews WHERE article_id = ?",
                (article_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "article_id": row["article_id"],
            "original_stance": row["original_stance"],
            "corrected_stance": row["corrected_stance"],
            "reviewer": row["reviewer"],
            "comment": row["comment"],
            "created_at": row["created_at"],
        }

    def list_recent_posted_articles(
        self,
        *,
        lookback_hours: int = 24,
        limit: int = 500,
    ) -> list[ArticleRecord]:
        """直近 ``lookback_hours`` 内に status='posted' で記録された article を返す。

        Phase B content-dedup: cross-source content similarity 比較で対象 article
        集合として使う。
        """
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM articles
                 WHERE status='posted'
                   AND datetime(created_at) >= datetime('now', ?)
                 ORDER BY datetime(created_at) DESC
                 LIMIT ?
                """,
                (f"-{lookback_hours} hours", limit),
            ).fetchall()
        return [_row_to_article(r) for r in rows]

    def count_prior_posts_by_dedup_key(
        self,
        dedup_key: str,
        *,
        lookback_hours: int = 168,
        exclude_within_hours: int = 0,
    ) -> tuple[int, ArticleRecord | None]:
        """Phase B-续报: 同 dedup_key の過去 post 数と最新の前報 record を返す。

        Args:
            dedup_key: 対象 dedup_key (空なら (0, None))
            lookback_hours: この時間以内の post を「続報候補」と見なす (default 7 日)
            exclude_within_hours: 直近この時間は除外。通常は 0 (本投稿自身が
                articles に書かれる前に呼ばれるため除外不要)。dry-run や
                back-fill では使う余地あり。

        Returns:
            (prior_count, latest_prior_record): 前報なしなら (0, None)。
        """
        if not dedup_key:
            return (0, None)
        clauses = [
            "dedup_key = ?",
            "status = 'posted'",
            "datetime(created_at) >= datetime('now', ?)",
        ]
        params: list[object] = [dedup_key, f"-{lookback_hours} hours"]
        if exclude_within_hours > 0:
            clauses.append("datetime(created_at) <= datetime('now', ?)")
            params.append(f"-{exclude_within_hours} hours")
        where = " AND ".join(clauses)
        with self._connect() as conn:
            count_row = conn.execute(
                f"SELECT COUNT(*) AS n FROM articles WHERE {where}",  # noqa: S608
                params,
            ).fetchone()
            count = int(count_row["n"]) if count_row else 0
            if count == 0:
                return (0, None)
            latest = conn.execute(
                f"SELECT * FROM articles WHERE {where} "  # noqa: S608
                "ORDER BY datetime(created_at) DESC LIMIT 1",
                params,
            ).fetchone()
        return (count, _row_to_article(latest) if latest else None)

    def count_posted_articles_since(self, since: datetime) -> int:
        """指定 datetime 以降に status='posted' で記録された article 数を返す。

        Phase 3 (daily synthesis 自動 trigger) で「前回 synthesis 後の新着 article
        delta」を staleness check するために使う。
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM articles "
                "WHERE status='posted' AND datetime(created_at) > datetime(?)",
                (_to_iso(since),),
            ).fetchone()
        return int(row["n"]) if row else 0

    def list_articles(
        self,
        *,
        run_id: int | None = None,
        importance: str | None = None,
        importance_in: list[str] | None = None,
        status: str | None = None,
        category: str | None = None,
        category_in: list[str] | None = None,
        feed_title: str | None = None,
        posted_channel: str | None = None,
        body_source: str | None = None,
        search: str | None = None,
        entity_type: str | None = None,
        entity_value: str | None = None,
        entity_filters: list[tuple[str, list[str]]] | None = None,
        socio_political_intent: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ArticleRecord]:
        clauses: list[str] = []
        params: list[object] = []
        if run_id is not None:
            clauses.append("run_id = ?")
            params.append(run_id)
        if importance is not None:
            clauses.append("importance = ?")
            params.append(importance)
        if importance_in:
            placeholders = ", ".join("?" for _ in importance_in)
            clauses.append(f"importance IN ({placeholders})")
            params.extend(importance_in)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if category is not None:
            clauses.append("category = ?")
            params.append(category)
        if category_in:
            placeholders = ", ".join("?" for _ in category_in)
            clauses.append(f"category IN ({placeholders})")
            params.extend(category_in)
        if feed_title is not None:
            clauses.append("feed_title = ?")
            params.append(feed_title)
        if posted_channel is not None:
            clauses.append("posted_channel = ?")
            params.append(posted_channel)
        # 本文由来 facet (2026-07-27): "stump"=切り株(全文未取得) / "full"=全文取得済。
        # 値は API 側で allowlist 済 (任意文字列は来ない) のため literal を直接埋める。
        if body_source == "stump":
            clauses.append("body_source = 'feed_summary'")
        elif body_source == "full":
            clauses.append(
                "body_source IN ('full_extract','playwright_extract','prefetch','scraper')"
            )
        if search:
            # 大小無視の部分一致 (ILIKE は PG 限定なので LOWER + LIKE で dialect 共通)。
            # Phase 2 K3: title / summary に加え body (全文) も対象にし、過去参照の
            # 取りこぼしを無くす。body は他フィルタ (since/category 等) で絞った後の
            # 部分集合に対する LIKE なので個人運用規模では実用上十分速い。
            like = f"%{search.lower()}%"
            clauses.append(
                "(LOWER(title) LIKE ? OR LOWER(COALESCE(summary, '')) LIKE ? "
                "OR LOWER(COALESCE(body, '')) LIKE ?)"
            )
            params.extend([like, like, like])
        # entity タグ絞り込み: 単一 (entity_type/entity_value, 後方互換) + 複数 (entity_filters)。
        # 各 filter = (entity_type, [values]): **filter 間は AND / values 内は OR** で
        # 1 filter = 1 subquery。これで cve+malware+pir+affected_vendor を同時合成できる
        # (affected_vendor は vendor→CVE 群を ('cve', [cve...]) として渡す)。
        ent_filters: list[tuple[str, list[str]]] = list(entity_filters or [])
        if entity_type and entity_value:
            ent_filters.append((entity_type, [entity_value]))
        for et, evals in ent_filters:
            vals = [v.lower() for v in evals if v]
            if not et or not vals:
                continue
            ph = ", ".join("?" for _ in vals)
            clauses.append(
                f"article_id IN (SELECT article_id FROM article_entities "  # noqa: S608
                f"WHERE entity_type = ? AND LOWER(value) IN ({ph}))"
            )
            params.append(et)
            params.extend(vals)
        if socio_political_intent:
            # Diamond socio-political 軸 (intent) での絞り込み (chip / dropdown 起点)。
            clauses.append("socio_political_intent = ?")
            params.append(socio_political_intent)
        if since is not None:
            clauses.append("created_at >= ?")
            params.append(_to_iso(since))
        if until is not None:
            clauses.append("created_at < ?")
            params.append(_to_iso(until))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = (
            f"SELECT * FROM articles {where} "  # noqa: S608 (clauses are param placeholders)
            "ORDER BY created_at DESC LIMIT ? OFFSET ?"
        )
        params.extend([limit, offset])
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_article(r) for r in rows]

    def entity_values_by_article(
        self, article_ids: list[str], entity_type: str, *, per_article_limit: int = 4
    ) -> dict[str, list[str]]:
        """指定 article_ids の entity 値を {article_id: [value,...]} で一括取得。

        News カードの malware/tool 等チップ表示用 (N+1 回避の batch fetch)。
        """
        if not article_ids:
            return {}
        placeholders = ", ".join("?" for _ in article_ids)
        sql = (
            f"SELECT article_id, value FROM article_entities "  # noqa: S608
            f"WHERE entity_type = ? AND article_id IN ({placeholders}) "
            "ORDER BY article_id, value"
        )
        out: dict[str, list[str]] = {}
        with self._connect() as conn:
            rows = conn.execute(sql, [entity_type, *article_ids]).fetchall()
        for r in rows:
            aid = r["article_id"]
            bucket = out.setdefault(aid, [])
            if len(bucket) < per_article_limit and r["value"] not in bucket:
                bucket.append(r["value"])
        return out
