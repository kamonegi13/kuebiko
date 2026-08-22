"""遅延正解ラベル (tuning_labels) の読み書き — 較正格子 P1 (run_history 分割の一部)。

設計は docs/self_evolving_tuning_design.md §4 C1。producer (src/tuning/label_harvest.py と
mitre_sync の hook) が「当時の判定 vs 後日確定した事実」の突合結果を書き、消費者
(P1 では運用タブの件数表示、P3 以降で較正 C3 / few-shot C8) が読む。

- 冪等性: ``dedup_key`` (producer が組む決定論キー) の UNIQUE で再収穫の重複を遮断する
  (指示でなく関門で止める — deterministic_gates 2026-08-19)。
- 置換: 同一 (article_id, field, source) の現行ラベルは ``superseded_by`` で新ラベルに
  置き換える (編集訂正のやり直し等)。source を跨いだ置換はしない — E3 が E1 を上書き
  しない (層の強度は消費者側で扱う)。
- ``provenance`` は JSON のみ。自由文 (コメント等) は入れない (§4 マスク方針を書込側で
  満たす — register_nav 2026-08-21 の教訓)。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from src.storage.repo_base import RunHistoryRepositoryBase
from src.storage.row_mappers import _to_iso


class TuningLabelsMixin(RunHistoryRepositoryBase):
    """tuning_labels テーブルの読み書き。"""

    def record_tuning_label(
        self,
        *,
        dedup_key: str,
        field: str,
        label_value: str,
        source: str,
        strength: str,
        provenance: str,
        article_id: str | None = None,
        arrived_at: datetime | None = None,
        supersede_same: bool = False,
        snapshot: str | None = None,
    ) -> int | None:
        """ラベルを 1 行追記する。挿入できたら id、dedup_key 既出なら None を返す。

        ``supersede_same=True`` のとき、同一 (article_id, field, source) の現行ラベル
        (superseded_by IS NULL) を新 id で置換済みにする。article_id が無い識別系
        ラベルには適用しない (記事という置換単位が無いため)。
        ``snapshot`` は学習テキストの凍結 (JSON — 本文 90 日 purge 後も教材が残る §13-3)。
        """
        ts = _to_iso(arrived_at or datetime.now(UTC))
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO tuning_labels"
                " (dedup_key, article_id, field, label_value, source, strength,"
                "  arrived_at, provenance, snapshot)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    dedup_key,
                    article_id,
                    field,
                    label_value,
                    source,
                    strength,
                    ts,
                    provenance,
                    snapshot,
                ),
            )
            if (cur.rowcount or 0) == 0:
                return None  # dedup_key 既出 (冪等)
            row = conn.execute(
                "SELECT id FROM tuning_labels WHERE dedup_key = ?",
                (dedup_key,),
            ).fetchone()
            new_id = int(row["id"])
            if supersede_same and article_id is not None:
                conn.execute(
                    "UPDATE tuning_labels SET superseded_by = ?"
                    " WHERE article_id = ? AND field = ? AND source = ?"
                    "   AND id <> ? AND superseded_by IS NULL",
                    (new_id, article_id, field, source, new_id),
                )
            return new_id

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

    def list_e1_subject_labels_not_self_source(self) -> list[dict[str, Any]]:
        """自己突合の再分類走査対象 (strength <> 'self_source' の E1 主体ラベル全件)。

        ``strength = 'self_source'`` を既に除外しているため、この一覧を基に更新する
        処理は自然に冪等 (2 回目は対象が残っていない)。記事が消えている行は
        (INNER JOIN のため) 対象外 — host が判定できない行は保守的に据え置く。
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT tl.id, tl.provenance, a.url"
                " FROM tuning_labels tl JOIN articles a ON a.article_id = tl.article_id"
                " WHERE tl.field = 'subject_actor' AND tl.source = 'E1'"
                "   AND tl.strength <> 'self_source'",
            ).fetchall()
        return [
            {
                "id": int(r["id"]),
                "provenance": str(r["provenance"]),
                "url": str(r["url"] or ""),
            }
            for r in rows
        ]

    def fetch_article_url(self, article_id: str) -> str | None:
        """記事の URL を 1 件引く (自己突合再分類で claim 側 host を引くため)。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT url FROM articles WHERE article_id = ?",
                (article_id,),
            ).fetchone()
        return str(row["url"]) if row is not None and row["url"] is not None else None

    def fetch_claim_feed_urls(self) -> list[str]:
        """claim 収集器由来の記事 (subject_actor_source='feed') の URL 全件。

        不変条件 14 の「同一**収集器**」判定用: claim 行そのものの host は
        リークサイトの .onion 等で、収集器の**配信面** (www.ransomware.live の RSS 記事)
        とは host が一致しない。news 側の host がこの集合の host に含まれるなら、
        その記事は収集器の配信面であり独立ソースではない。host 正規化は呼び出し側。
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT url FROM articles"
                " WHERE subject_actor_source = 'feed' AND url IS NOT NULL AND url <> ''",
            ).fetchall()
        return [str(r["url"]) for r in rows]

    def mark_label_self_source(self, label_id: int) -> None:
        """ラベルを削除せず strength='self_source' に更新する (非破壊隔離、冪等)。"""
        with self._connect() as conn:
            conn.execute(
                "UPDATE tuning_labels SET strength = 'self_source'"
                " WHERE id = ? AND strength <> 'self_source'",
                (label_id,),
            )

    def list_labels_missing_snapshot(self) -> list[dict[str, Any]]:
        """snapshot 未凍結で本文がまだ残っているラベル (backfill 対象、§13-3)。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT tl.id, a.title, a.body, a.category"
                " FROM tuning_labels tl JOIN articles a ON a.article_id = tl.article_id"
                " WHERE tl.snapshot IS NULL AND tl.article_id IS NOT NULL"
                "   AND LENGTH(COALESCE(a.body, '')) >= 200",
            ).fetchall()
        return [
            {
                "id": int(r["id"]),
                "title": str(r["title"] or ""),
                "body": str(r["body"] or ""),
                "category": str(r["category"] or ""),
            }
            for r in rows
        ]

    def set_label_snapshot(self, label_id: int, snapshot: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE tuning_labels SET snapshot = ? WHERE id = ? AND snapshot IS NULL",
                (snapshot, label_id),
            )

    # ---------- tuning_evals (P2: goldset 評価 + C7 rollback 裁定) ----------

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

    def supersede_active_labels(
        self,
        *,
        article_id: str,
        field: str,
        source: str,
        superseded_by: int,
    ) -> int:
        """同一 (article, field, source) の現行ラベルを置換済みにする (裁定の終端処理用)。

        ``superseded_by=0`` は「後継なしの隔離」sentinel (TTL 失効 §12 用 — 実ラベル id は
        1 始まりなので衝突しない)。active 判定は superseded_by IS NULL なので 0 でも外れる。
        """
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE tuning_labels SET superseded_by = ?"
                " WHERE article_id = ? AND field = ? AND source = ?"
                "   AND id <> ? AND superseded_by IS NULL",
                (superseded_by, article_id, field, source, superseded_by),
            )
            return int(cur.rowcount or 0)

    def taxonomy_tier_agreement(self) -> list[dict[str, Any]]:
        """taxonomy 提案の区分別 人間同意率 (§11-C — 区分1 が ~100% なら自動化候補)。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT tier,"
                " SUM(CASE WHEN status = 'accepted' THEN 1 ELSE 0 END) AS accepted,"
                " SUM(CASE WHEN status = 'rejected' THEN 1 ELSE 0 END) AS rejected"
                " FROM taxonomy_review_proposals"
                " WHERE status IN ('accepted', 'rejected') GROUP BY tier ORDER BY tier",
            ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            accepted = int(r["accepted"] or 0)
            rejected = int(r["rejected"] or 0)
            total = accepted + rejected
            out.append(
                {
                    "tier": str(r["tier"]),
                    "accepted": accepted,
                    "rejected": rejected,
                    "agreement_rate": round(accepted / total, 3) if total else None,
                }
            )
        return out

    def list_editorial_stance_reviews(self) -> list[dict[str, Any]]:
        """editorial 訂正の全件 (収穫③ E3 producer 用。UNIQUE(article_id) で最新のみ)。

        title/body は学習テキスト凍結 (snapshot §13-3) 用 (LEFT JOIN — 記事が消えていても
        訂正自体はラベル化する)。
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT r.article_id, r.original_stance, r.corrected_stance, r.created_at,"
                " a.title, a.body, a.category"
                " FROM editorial_stance_reviews r"
                " LEFT JOIN articles a ON a.article_id = r.article_id",
            ).fetchall()
        return [
            {
                "article_id": str(r["article_id"]),
                "original_stance": (
                    str(r["original_stance"]) if r["original_stance"] is not None else None
                ),
                "corrected_stance": str(r["corrected_stance"]),
                "created_at": r["created_at"],
                "title": str(r["title"] or ""),
                "body": str(r["body"] or ""),
                "category": str(r["category"] or ""),
            }
            for r in rows
        ]
