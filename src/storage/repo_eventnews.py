"""事象単位ニュース (event news) v1 の永続化 (run_history 分割の一部)。

設計 SSoT: docs/event_news_design.md (v2)、interface pin: src/eventnews/models.py。
このモジュールは群化アイテム (event_items) / 版履歴 (event_item_versions) /
構成記事 (event_item_members) / 意味 dedup の skip 記録 (dedup_semantic_skips) の
読み書きを担う。v1 は shadow — 本番配信・スケジューラには接続しない (§12)。

錨時刻は使わない: event_items の first_reported_at/last_reported_at は
grouping モジュールが articles から算出済みの値を渡す (このモジュール自身は
event_time.py の錨式を再計算しない)。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from src.eventnews.models import VERSION_CAP, ItemState
from src.storage.records import EventNoteRecord
from src.storage.repo_base import RunHistoryRepositoryBase
from src.storage.row_mappers import _from_iso, _to_iso

# event_items の部分更新で許可するカラム (allowlist。update_article_enrichment と同型)。
_EVENT_ITEM_UPDATABLE_COLUMNS = frozenset(
    {
        "status",
        "change_kind",
        "current_version",
        "importance",
        "best_source_tier",
        "independent_sources",
        "state_media_count",
        "unclassified_sources",
        "last_reported_at",
        "updated_at",
        "related_to",
        # 遡及統合 (scripts/retro_merge_events.py) が立てる。読む側 (一覧・詳細・
        # 公開面・写し) は既に全経路が merged_into を見て除外している。
        "merged_into",
    }
)


@dataclass(frozen=True)
class ImportanceRule:
    """重要度ごとに追加で課す掲載条件。

    重要度によって「出す資格」が違うときに使う。公開面では high は単独報でも
    出す (重要だから 1 媒体でも知らせる、という判断が既に働いている) 一方、
    medium にはその判断が無いぶん **突き合わせ (複数媒体)** を求める。

    ⚠ 条件を呼び手が取得後の filter で掛けると **LIMIT より後**になり
    「新着 N 件のうち該当するもの」しか出ない。クエリの中で表現すること。
    """

    #: 独立媒体数の下限 (0 = 課さない)
    min_independent_sources: int = 0
    #: 統合記事が生成済みであること (current_version > 0)
    requires_news: bool = False

    @property
    def is_open(self) -> bool:
        """追加条件が無い (= 重要度だけで通る)。"""
        return self.min_independent_sources <= 0 and not self.requires_news

    def allows(self, *, independent_sources: int, has_news: bool) -> bool:
        """1 件が条件を満たすか。**一覧 (SQL) と同じ判定を Python でも行う**。

        一覧と詳細で条件が分かれると、一覧に出ないものが直リンクでは読める
        状態になる。両方をこの 1 つの述語から導くこと。
        """
        if independent_sources < self.min_independent_sources:
            return False
        return has_news if self.requires_news else True


@dataclass(frozen=True)
class EventItemRecord:
    """event_items の 1 行。判定ロジックが読む最小状態は ``state`` (ItemState)、
    残りは表示・監査用の付加メタ。
    """

    state: ItemState
    origin: str
    change_kind: str | None
    merged_into: str | None
    related_to: str | None
    best_source_tier: str
    independent_sources: int
    state_media_count: int
    unclassified_sources: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class EventMemberRecord:
    """event_item_members の 1 行。"""

    article_id: str
    joined_at: datetime
    contributed_new_facts: int
    join_signal: str


@dataclass(frozen=True)
class EventUpdateMark:
    """事象が初報のままか、続報で動いたか (表示バッジの材料)。"""

    #: 新事実で本文を作り直した最後の時刻 (無ければ None = 本文は初報のまま)
    rewritten_at: datetime | None
    #: 作り直した回数
    rewrite_count: int
    #: 内容は変えずに後から報じた媒体数 (裏取りが増えただけ)
    follow_up_sources: int


@dataclass(frozen=True)
class EventVersionRecord:
    """event_item_versions の 1 行。"""

    item_id: str
    version: int
    generated_at: datetime
    model: str
    prompt_version: str
    headline: str
    body_json: str
    new_facts_json: str
    verified_at: datetime | None
    dropped_lines: int
    repaired_ids: int


def _row_to_event_item(row: Any, member_ids: tuple[str, ...]) -> EventItemRecord:
    first = _from_iso(row["first_reported_at"])
    last = _from_iso(row["last_reported_at"])
    created = _from_iso(row["created_at"])
    updated = _from_iso(row["updated_at"])
    assert first is not None
    assert last is not None
    assert created is not None
    assert updated is not None
    state = ItemState(
        item_id=str(row["id"]),
        first_reported_at=first,
        last_reported_at=last,
        status=str(row["status"]),
        importance=str(row["importance"] or ""),
        current_version=int(row["current_version"] or 0),
        member_ids=member_ids,
    )
    return EventItemRecord(
        state=state,
        origin=str(row["origin"]),
        change_kind=(str(row["change_kind"]) if row["change_kind"] is not None else None),
        merged_into=(str(row["merged_into"]) if row["merged_into"] is not None else None),
        related_to=(str(row["related_to"]) if row["related_to"] is not None else None),
        best_source_tier=str(row["best_source_tier"] or ""),
        independent_sources=int(row["independent_sources"] or 0),
        state_media_count=int(row["state_media_count"] or 0),
        unclassified_sources=int(row["unclassified_sources"] or 0),
        created_at=created,
        updated_at=updated,
    )


def _row_to_event_version(row: Any) -> EventVersionRecord:
    generated = _from_iso(row["generated_at"])
    assert generated is not None
    return EventVersionRecord(
        item_id=str(row["item_id"]),
        version=int(row["version"]),
        generated_at=generated,
        model=str(row["model"]),
        prompt_version=str(row["prompt_version"]),
        headline=str(row["headline"]),
        body_json=str(row["body_json"]),
        new_facts_json=str(row["new_facts_json"]),
        verified_at=_from_iso(row["verified_at"]),
        dropped_lines=int(row["dropped_lines"] or 0),
        repaired_ids=int(row["repaired_ids"] or 0),
    )


class EventNewsMixin(RunHistoryRepositoryBase):
    """event_items / event_item_versions / event_item_members / dedup_semantic_skips。"""

    # ---------- event_items ----------

    def create_event_item(
        self,
        *,
        item_id: str,
        origin: str,
        first_reported_at: datetime,
        last_reported_at: datetime,
        importance: str,
        best_source_tier: str = "",
        independent_sources: int = 0,
        state_media_count: int = 0,
        unclassified_sources: int = 0,
        related_to: str | None = None,
        when: datetime | None = None,
    ) -> str:
        """新規 event_item を作成する (id は呼び手 = grouping モジュール指定)。

        ``origin`` は 'live' | 'replay' 必須 (§2 — replay 行の本番混入防止)。
        初期状態は status='new' / current_version=0 (§7: 0 は無条件再生成対象)。
        """
        now = _to_iso(when or datetime.now(UTC))
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO event_items"
                " (id, origin, first_reported_at, last_reported_at, status, change_kind,"
                "  current_version, merged_into, related_to, importance, best_source_tier,"
                "  independent_sources, state_media_count, unclassified_sources,"
                "  created_at, updated_at)"
                " VALUES (?, ?, ?, ?, 'new', NULL, 0, NULL, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item_id,
                    origin,
                    _to_iso(first_reported_at),
                    _to_iso(last_reported_at),
                    related_to,
                    importance,
                    best_source_tier,
                    independent_sources,
                    state_media_count,
                    unclassified_sources,
                    now,
                    now,
                ),
            )
        return item_id

    def get_event_item(self, item_id: str) -> EventItemRecord | None:
        """event_item 1 件を member_ids 込みで取得する (無ければ None)。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM event_items WHERE id=?",
                (item_id,),
            ).fetchone()
            if row is None:
                return None
            member_rows = conn.execute(
                "SELECT article_id FROM event_item_members WHERE item_id=? ORDER BY joined_at ASC",
                (item_id,),
            ).fetchall()
        member_ids = tuple(str(r["article_id"]) for r in member_rows)
        return _row_to_event_item(row, member_ids)

    def upsert_event_note(self, record: EventNoteRecord) -> None:
        """1 事象の memo/bookmark/tags/judgment を upsert (created_at は保持)。

        記事単位の ``upsert_article_note`` と同形。事象は記事の集合なので、
        「この事象を継続監視する」判断は記事に付けるものとは粒度が違う。
        """
        import json as _json

        now_iso = _to_iso(datetime.now(UTC))
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO event_item_notes
                  (item_id, bookmarked, note, tags, judgment, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(item_id) DO UPDATE SET
                  bookmarked = excluded.bookmarked,
                  note       = excluded.note,
                  tags       = excluded.tags,
                  judgment   = excluded.judgment,
                  updated_at = excluded.updated_at
                """,
                (
                    record.item_id,
                    1 if record.bookmarked else 0,
                    record.note,
                    _json.dumps(record.tags, ensure_ascii=False),
                    record.judgment,
                    _to_iso(record.created_at),
                    now_iso,
                ),
            )

    def get_event_note(self, item_id: str) -> EventNoteRecord | None:
        """1 事象の note を取得 (無ければ None)。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM event_item_notes WHERE item_id=?", (item_id,)
            ).fetchone()
        return _row_to_event_note(row) if row else None

    def list_event_notes(
        self, *, bookmarked_only: bool = False, limit: int = 200
    ) -> list[EventNoteRecord]:
        """note を更新新しい順に列挙 (一覧ページ用)。"""
        where = "WHERE bookmarked=1 " if bookmarked_only else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM event_item_notes {where}"  # noqa: S608 — 定数のみ
                "ORDER BY datetime(updated_at) DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [_row_to_event_note(r) for r in rows]

    def search_event_versions(self, term: str, *, limit: int = 500) -> list[str]:
        """生成本文 (見出し + 本体) に語を含む事象の item_id を返す。

        **なぜ構成記事の検索だけでは足りないか**: 生成本文は日本語で、原記事は英語の
        ことが多い。標本 60 事象のうち 57 件が「生成本文にしか無い語」を含んでいた
        (バッファオーバーフロー / リークサイト / 未認証 / 安定版 等)。読み手が一覧で
        見ているのは生成された見出しなので、そこに見えている語で引けないのは事故。
        """
        needle = term.strip().lower()
        if not needle:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT item_id AS item_id FROM event_item_versions"
                " WHERE LOWER(headline) LIKE ? OR LOWER(body_json) LIKE ?"
                " LIMIT ?",
                (f"%{needle}%", f"%{needle}%", int(limit)),
            ).fetchall()
        return [str(r["item_id"]) for r in rows]

    def existing_member_article_ids(self, article_ids: Sequence[str]) -> set[str]:
        """指定記事のうち、既にどこかのアイテムのメンバーになっているものを返す。

        候補の重複投入を防ぐ **最後の関門**。``run_hourly`` は復元したアイテムの
        メンバーしか除外できないため、復元窓 (dormant 期限) より古いアイテムの
        メンバーが再び候補に入ると、決定論の item_id が衝突して落ちる
        (2026-08-24: 遡及構築で `event_items_pkey` の UniqueViolation)。
        """
        if not article_ids:
            return set()
        placeholders = ",".join("?" for _ in article_ids)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT article_id AS article_id FROM event_item_members"  # noqa: S608
                f" WHERE article_id IN ({placeholders})",
                list(article_ids),
            ).fetchall()
        return {str(r["article_id"]) for r in rows}

    def list_event_items(
        self,
        *,
        origin: str | None = None,
        statuses: Sequence[str] | None = None,
        importances: Sequence[str] | None = None,
        importance_rules: Mapping[str, ImportanceRule] | None = None,
        exclude_merged: bool = False,
        min_independent_sources: int = 0,
        has_news: bool | None = None,
        exclude_duplicate_only: bool = False,
        member_categories: Sequence[str] | None = None,
        member_country: str | None = None,
        since: datetime | None = None,
        order_by: str = "recency",
        member_article_ids: Sequence[str] | None = None,
        search_item_ids: Sequence[str] | None = None,
        search_member_article_ids: Sequence[str] | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[EventItemRecord]:
        """event_items を新着順 (last_reported_at DESC) で列挙する (member_ids 込み)。

        ``origin`` / ``statuses`` / ``importances`` で絞り込み可能。N+1 を避けるため
        member_ids は対象アイテム群をまとめて 1 クエリで引く。

        ``since`` は ``last_reported_at`` の下限 (事象そのものの新しさ。記事側の
        ``since_hours`` とは別物)。``order_by`` は "recency" (既定: 新着順) か
        "corroboration" (独立媒体数の多い順 → 同数なら新しい順)。

        ⚠ ``corroboration`` は **表示順のためだけ** に使うこと。重要性の背骨は
        PIR → importance → channel であり、収集量 (何媒体が報じたか) で重要性を
        決めてはいけない (tests/unit/test_burst_boundary.py と同じ趣旨)。ここは
        既に importance で絞られた集合の **並べ替え**なので線の内側。

        ``member_categories`` は構成記事のカテゴリでの絞り込み (公開面のカテゴリ別
        一覧)。記事側 facet を走査してから持ち上げる経路と違い上限が無く、
        **LIMIT より前**に効く。

        ``exclude_duplicate_only`` は「全メンバーが dedup で重複判定 かつ 生成本文なし」
        の事象を落とす (公開面用)。**必ず LIMIT より前に効かせる** — 取得後に間引くと
        「新着 N 件のうち公開できるもの」になり「公開できる新着 N 件」にならない。

        ``min_independent_sources`` / ``has_news`` は **事象固有の軸** (記事側には
        存在しない)。単独報が全体の 9 割を占めるため、記事から持ち上げた facet の
        どれよりも母集団を大きく動かす。既定はどちらも「絞らない」— 単独記事を
        既定で落とすと読む場所が 2 つに戻る (§14b 案 A)。

        ⚠ **絞り込みは LIMIT より前に効かせること**。呼び手が取得後に filter すると
        「新着 N 件のうち該当するもの」しか出ず、「該当するものの新着 N 件」に
        ならない (遡及構築でアイテムが 2,000 件規模になり顕在化した)。
        """
        clauses: list[str] = []
        params: list[object] = []
        if origin is not None:
            clauses.append("origin = ?")
            params.append(origin)
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            clauses.append(f"status IN ({placeholders})")
            params.extend(statuses)
        if importances:
            # 重要度ごとに条件が違いうる (``importance_rules``)。条件のあるものは
            # その重要度と AND で括り、全体を OR で束ねる。
            parts: list[str] = []
            for imp in importances:
                rule = (importance_rules or {}).get(imp)
                if rule is None or rule.is_open:
                    parts.append("importance = ?")
                    params.append(imp)
                    continue
                sub = ["importance = ?"]
                params.append(imp)
                if rule.min_independent_sources > 0:
                    sub.append("independent_sources >= ?")
                    params.append(int(rule.min_independent_sources))
                if rule.requires_news:
                    sub.append("current_version > 0")
                parts.append("(" + " AND ".join(sub) + ")")
            clauses.append("(" + " OR ".join(parts) + ")")
        if exclude_merged:
            clauses.append("(merged_into IS NULL OR merged_into = '')")
        if since is not None:
            # ⚠ `datetime(col)` を使わない。dual backend の翻訳は `datetime(col)` を
            # 「col は TIMESTAMPTZ」と見なすが、この列は **TEXT** なので PG で
            # `text >= timestamptz` になり落ちる。ISO 文字列どうしの比較にする
            # (書き込みは常に `_to_iso` なので辞書順 = 時系列順。ORDER BY も同じ前提)。
            clauses.append("last_reported_at >= ?")
            params.append(_to_iso(since))
        if min_independent_sources > 0:
            clauses.append("independent_sources >= ?")
            params.append(int(min_independent_sources))
        if has_news is not None:
            # 生成の有無は current_version が SSoT (版が 1 本でもあれば生成済み)
            clauses.append("current_version > 0" if has_news else "current_version = 0")
        if member_categories:
            ph = ",".join("?" for _ in member_categories)
            clauses.append(
                f"EXISTS (SELECT 1 FROM event_item_members cm"  # noqa: S608 — placeholders のみ
                f" JOIN articles ca ON ca.article_id = cm.article_id"
                f" WHERE cm.item_id = event_items.id AND ca.category IN ({ph}))"
            )
            params.extend(member_categories)
        if member_country:
            clauses.append(
                "EXISTS (SELECT 1 FROM event_item_members km"
                " JOIN articles ka ON ka.article_id = km.article_id"
                " WHERE km.item_id = event_items.id AND UPPER(ka.victim_country_iso) = ?)"
            )
            params.append(member_country.upper())
        if exclude_duplicate_only:
            clauses.append(
                "(current_version > 0 OR EXISTS ("
                " SELECT 1 FROM event_item_members dm"
                " JOIN articles da ON da.article_id = dm.article_id"
                " WHERE dm.item_id = event_items.id AND da.status <> 'skipped_duplicate'))"
            )
        if member_article_ids is not None:
            # 記事側の絞り込み (pivot / category / 検索 等) を事象へ持ち上げる。
            # **1 件でも該当メンバーを含む事象**を返す (事象は記事の集合なので、
            # 「どのメンバーが該当したか」ではなく「事象が該当するか」で数える)。
            if not member_article_ids:
                return []
            ph = ",".join("?" for _ in member_article_ids)
            clauses.append(
                f"EXISTS (SELECT 1 FROM event_item_members m"  # noqa: S608 — placeholders のみ
                f" WHERE m.item_id = event_items.id AND m.article_id IN ({ph}))"
            )
            params.extend(member_article_ids)
        if search_item_ids is not None or search_member_article_ids is not None:
            # 検索語は「生成本文に含む」**または**「構成記事に含む」で一致とする。
            # 一覧で見えているのは生成された見出しなので、そこに見える語で引けないと事故。
            items = list(search_item_ids or [])
            arts = list(search_member_article_ids or [])
            if not items and not arts:
                return []
            ors: list[str] = []
            if items:
                ors.append(f"id IN ({','.join('?' for _ in items)})")
                params.extend(items)
            if arts:
                ors.append(
                    "EXISTS (SELECT 1 FROM event_item_members sm"
                    f" WHERE sm.item_id = event_items.id"
                    f" AND sm.article_id IN ({','.join('?' for _ in arts)}))"
                )
                params.extend(arts)
            clauses.append(f"({' OR '.join(ors)})")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(int(limit))
        params.append(max(0, int(offset)))
        with self._connect() as conn:
            order_sql = (
                "independent_sources DESC, datetime(last_reported_at) DESC"
                if order_by == "corroboration"
                else "datetime(last_reported_at) DESC"
            )
            rows = conn.execute(
                f"SELECT * FROM event_items {where} "  # noqa: S608 — where句/order は固定
                f"ORDER BY {order_sql} LIMIT ? OFFSET ?",
                params,
            ).fetchall()
            if not rows:
                return []
            ids = [str(r["id"]) for r in rows]
            id_placeholders = ",".join("?" for _ in ids)
            member_rows = conn.execute(
                "SELECT item_id, article_id FROM event_item_members "  # noqa: S608
                f"WHERE item_id IN ({id_placeholders}) ORDER BY joined_at ASC",
                ids,
            ).fetchall()
        members_by_item: dict[str, list[str]] = {}
        for r in member_rows:
            members_by_item.setdefault(str(r["item_id"]), []).append(str(r["article_id"]))
        return [_row_to_event_item(r, tuple(members_by_item.get(str(r["id"]), ()))) for r in rows]

    def update_event_item(self, item_id: str, fields: dict[str, object]) -> int:
        """event_items の部分更新 (allowlist 制御、update_article_enrichment と同型)。

        datetime 値は ISO 文字列へ変換する。``updated_at`` を fields に含めない場合は
        現在時刻を自動付与する。戻り値は更新行数。
        """
        cols = [c for c in fields if c in _EVENT_ITEM_UPDATABLE_COLUMNS]
        if not cols:
            return 0
        values: list[object] = []
        for c in cols:
            v = fields[c]
            values.append(_to_iso(v) if isinstance(v, datetime) else v)
        if "updated_at" not in cols:
            cols.append("updated_at")
            values.append(_to_iso(datetime.now(UTC)))
        set_clause = ", ".join(f"{c}=?" for c in cols)
        values.append(item_id)
        with self._connect() as conn:
            cur = conn.execute(
                f"UPDATE event_items SET {set_clause} WHERE id=?",  # noqa: S608 — cols allowlisted
                values,
            )
            return int(cur.rowcount or 0)

    # ---------- event_item_members ----------

    def add_event_member(
        self,
        *,
        item_id: str,
        article_id: str,
        joined_at: datetime,
        contributed_new_facts: int,
        join_signal: str,
    ) -> None:
        """群のメンバー記事を追加する (INSERT OR IGNORE 相当、冪等)。"""
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO event_item_members"
                " (item_id, article_id, joined_at, contributed_new_facts, join_signal)"
                " VALUES (?, ?, ?, ?, ?)",
                (item_id, article_id, _to_iso(joined_at), contributed_new_facts, join_signal),
            )

    def record_pair_shadow(
        self,
        *,
        observed_at: datetime,
        left_id: str,
        right_id: str,
        features_json: str,
        llm_same: bool | None,
        rule_joined: bool,
        cos: float,
        ml_proba: float | None = None,
        ml_joined: bool | None = None,
    ) -> None:
        """群化のシャドー記録を 1 行残す (本番の挙動は変えない)。

        ``llm_same`` は **None を保つ** — 「別と判定した」と「判定できなかった」は
        別の情報なので、False へ倒さない。
        """
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO event_pair_shadow"
                " (observed_at, left_id, right_id, features_json, llm_same, rule_joined,"
                "  cos, ml_proba, ml_joined)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _to_iso(observed_at),
                    left_id,
                    right_id,
                    features_json,
                    None if llm_same is None else int(llm_same),
                    int(rule_joined),
                    float(cos),
                    None if ml_proba is None else float(ml_proba),
                    None if ml_joined is None else int(ml_joined),
                ),
            )

    def list_related_events(self, item_id: str) -> list[EventItemRecord]:
        """この事象を親 (``related_to``) として指す生存事象 — 分割の子など。

        「別事象だが関連」の逆引き。親→子のリストを親側に持たせると更新が
        二重になるため、常にこの逆引きで出す (2026-09-03)。
        """
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT * FROM event_items WHERE related_to = ? AND merged_into IS NULL"
                " ORDER BY last_reported_at DESC",
                (item_id,),
            )
            rows = cur.fetchall()
        out: list[EventItemRecord] = []
        for row in rows:
            members = self.list_event_members(str(row["id"]))
            out.append(_row_to_event_item(row, tuple(m.article_id for m in members)))
        return out

    def move_event_member(
        self, *, article_id: str, from_item: str, to_item: str, join_signal: str
    ) -> int:
        """メンバー記事を別の事象へ移す (遡及分割用)。戻り値は移した行数。

        ⚠ 行を消して作り直さない — joined_at / contributed_new_facts を保つ。
        """
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE event_item_members SET item_id=?, join_signal=?"
                " WHERE item_id=? AND article_id=?",
                (to_item, join_signal, from_item, article_id),
            )
            return int(cur.rowcount or 0)

    def get_version_prompt(self, item_id: str, version: int) -> str:
        """版の基底プロンプト (SFT/DPO 用)。Record には載せない — 一覧が太るため。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT prompt_text FROM event_item_versions WHERE item_id=? AND version=?",
                (item_id, version),
            ).fetchone()
        return str(row[0]) if row and row[0] else ""

    def record_draft_reject(
        self, *, item_id: str, version: int, model: str, hints: str, draft_json: str
    ) -> None:
        """関門に落ちた草稿を残す (DPO の rejected 側)。採用版と対で読む。"""
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO event_draft_rejects"
                " (item_id, version, model, hints, draft_json, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (item_id, version, model, hints, draft_json, _to_iso(datetime.now(UTC))),
            )

    def list_draft_rejects(self, *, limit: int = 500) -> list[dict[str, object]]:
        """棄却草稿を新しい順に読む (学習エクスポータ用の読み口)。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT item_id, version, model, hints, draft_json, created_at"
                " FROM event_draft_rejects ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        cols = ("item_id", "version", "model", "hints", "draft_json", "created_at")
        return [dict(zip(cols, tuple(r), strict=True)) for r in rows]

    def get_article_kinds(self, article_ids: Sequence[str]) -> dict[str, str]:
        """記事の種別キャッシュを一括で引く (無い記事は返さない)。"""
        if not article_ids:
            return {}
        placeholders = ",".join("?" for _ in article_ids)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT article_id, kind FROM article_kinds WHERE article_id IN ({placeholders})",  # noqa: S608
                list(article_ids),
            ).fetchall()
        return {str(r[0]): str(r[1]) for r in rows}

    def set_article_kind(self, article_id: str, kind: str, model: str) -> None:
        """種別を記録する (冪等 — 既存は上書きしない。分類は記事ごとに 1 回)。"""
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO article_kinds (article_id, kind, model, created_at)"
                " VALUES (?, ?, ?, ?)",
                (article_id, kind, model, _to_iso(datetime.now(UTC))),
            )

    def list_event_members(self, item_id: str) -> list[EventMemberRecord]:
        """アイテムの構成記事を参加順 (joined_at ASC) で返す。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT article_id, joined_at, contributed_new_facts, join_signal"
                " FROM event_item_members WHERE item_id=? ORDER BY joined_at ASC",
                (item_id,),
            ).fetchall()
        out: list[EventMemberRecord] = []
        for r in rows:
            joined = _from_iso(r["joined_at"])
            assert joined is not None
            out.append(
                EventMemberRecord(
                    article_id=str(r["article_id"]),
                    joined_at=joined,
                    contributed_new_facts=int(r["contributed_new_facts"] or 0),
                    join_signal=str(r["join_signal"] or ""),
                )
            )
        return out

    # ---------- event_item_versions ----------

    def record_event_version(
        self,
        *,
        item_id: str,
        version: int,
        generated_at: datetime,
        model: str,
        prompt_version: str,
        headline: str,
        body_json: str,
        new_facts_json: str,
        verified_at: datetime | None,
        dropped_lines: int,
        repaired_ids: int,
        prompt_text: str = "",
    ) -> None:
        """版を 1 件記録する (§6/§9)。

        ``prompt_text`` は SFT 教師データ用の基底プロンプト (書き直しヒント抜き)。
        メンバー記事が後から合流して動くため、事後の再構成では正確な対にならない —
        生成時に対で残すのが唯一の方法 (2026-08-27)。公開 API には出さない。

        同一 (item_id, version) の再投入は上書き (生成リトライの冪等性)。挿入後、
        保持上限 (``VERSION_CAP``、version=1 は常に保持) を超えていれば古い版から
        剪定する。**current_version の更新はこのメソッドの責務外** — 呼び出し側が
        ``update_event_item(item_id, {"current_version": version})`` で別途行う
        (状態機械の判定と版の記録を分離するため)。
        """
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO event_item_versions
                  (item_id, version, generated_at, model, prompt_version, headline,
                   body_json, new_facts_json, verified_at, dropped_lines, repaired_ids,
                   prompt_text)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(item_id, version) DO UPDATE SET
                  generated_at   = excluded.generated_at,
                  model          = excluded.model,
                  prompt_version = excluded.prompt_version,
                  headline       = excluded.headline,
                  body_json      = excluded.body_json,
                  new_facts_json = excluded.new_facts_json,
                  verified_at    = excluded.verified_at,
                  dropped_lines  = excluded.dropped_lines,
                  repaired_ids   = excluded.repaired_ids,
                  prompt_text    = excluded.prompt_text
                """,
                (
                    item_id,
                    version,
                    _to_iso(generated_at),
                    model,
                    prompt_version,
                    headline,
                    body_json,
                    new_facts_json,
                    _to_iso(verified_at) if verified_at is not None else None,
                    dropped_lines,
                    repaired_ids,
                    prompt_text,
                ),
            )
            self._prune_event_versions(conn, item_id)

    def _prune_event_versions(self, conn: Any, item_id: str) -> None:
        """保持上限 VERSION_CAP を超えたら version=1 を除く最古から削除する (§6)。"""
        rows = conn.execute(
            "SELECT version FROM event_item_versions WHERE item_id=? ORDER BY version ASC",
            (item_id,),
        ).fetchall()
        versions = [int(r["version"]) for r in rows]
        if len(versions) <= VERSION_CAP:
            return
        excess = len(versions) - VERSION_CAP
        deletable = [v for v in versions if v != 1]
        to_delete = deletable[:excess]
        if not to_delete:
            return
        placeholders = ",".join("?" for _ in to_delete)
        conn.execute(
            "DELETE FROM event_item_versions "  # noqa: S608 — placeholders は int のみ
            f"WHERE item_id=? AND version IN ({placeholders})",
            (item_id, *to_delete),
        )

    def list_event_versions(self, item_id: str) -> list[EventVersionRecord]:
        """アイテムの版履歴を新しい順 (version DESC) で返す。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM event_item_versions WHERE item_id=? ORDER BY version DESC",
                (item_id,),
            ).fetchall()
        return [_row_to_event_version(r) for r in rows]

    def latest_event_versions(self, item_ids: Sequence[str]) -> dict[str, EventVersionRecord]:
        """複数アイテムの **最新版のみ** を 1 クエリで返す (一覧の N+1 回避)。

        dual-backend 可搬形は ROW_NUMBER のみ (bare GROUP BY は PG で落ちる)。
        """
        ids = list(dict.fromkeys(item_ids))
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM ("  # noqa: S608 — placeholders のみ
                " SELECT *, ROW_NUMBER() OVER (PARTITION BY item_id ORDER BY version DESC) AS rn"
                f" FROM event_item_versions WHERE item_id IN ({placeholders})"
                ") t WHERE rn = 1",
                ids,
            ).fetchall()
        return {str(r["item_id"]): _row_to_event_version(r) for r in rows}

    def event_update_marks(self, item_ids: Sequence[str]) -> dict[str, EventUpdateMark]:
        """事象ごとの「続報の有無」を 2 クエリで返す (一覧の N+1 回避)。

        読み手には **初報と続報の区別が付かない** (日付だけ動いて再浮上する) ため、
        表示側でバッジに使う (2026-08-27 利用者指摘)。合流の種類は 2 つある:

        - ``rewritten_at``: 新事実を持ち込んだ合流 → 本文を作り直した版が残る。
          版 2 以降で ``new_facts_json`` が非空のものだけを数える。**空のものは
          遡及再生成** (プロンプト改訂で全件作り直した分) であって続報ではない。
        - ``follow_up_sources``: 内容は変えないが後から報じた媒体数。生成時の
          founding member は全員 ``contributed_new_facts=1`` で入るので、
          0 の member は必ず後着の裏取り (reinforced) になる。

        ``last_reported_at > first_reported_at`` は続報の指標に**ならない** —
        同時に群化した複数媒体でも報道時刻はばらつくため (実測 460 件中 280 件が
        該当したが、実際に続報だったのは 103 件)。
        """
        ids = list(dict.fromkeys(item_ids))
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        rewritten: dict[str, tuple[datetime | None, int]] = {}
        follow_up: dict[str, int] = {}
        with self._connect() as conn:
            for row in conn.execute(
                "SELECT item_id, MAX(generated_at) AS at, COUNT(*) AS n"  # noqa: S608
                " FROM event_item_versions"
                f" WHERE item_id IN ({placeholders}) AND version > 1"
                "   AND new_facts_json NOT IN ('[]', '{}', '', 'null')"
                " GROUP BY item_id",
                ids,
            ).fetchall():
                rewritten[str(row["item_id"])] = (_from_iso(str(row["at"])), int(row["n"]))
            for row in conn.execute(
                "SELECT item_id, COUNT(*) AS n FROM event_item_members"  # noqa: S608
                f" WHERE item_id IN ({placeholders}) AND contributed_new_facts = 0"
                " GROUP BY item_id",
                ids,
            ).fetchall():
                follow_up[str(row["item_id"])] = int(row["n"])
        return {
            item_id: EventUpdateMark(
                rewritten_at=rewritten.get(item_id, (None, 0))[0],
                rewrite_count=rewritten.get(item_id, (None, 0))[1],
                follow_up_sources=follow_up.get(item_id, 0),
            )
            for item_id in ids
            if item_id in rewritten or item_id in follow_up
        }

    # ---------- dedup_semantic_skips (§8b) ----------

    def record_semantic_skips(self, rows: Sequence[Any]) -> int:
        """意味 dedup の skip 記録を一括挿入する (§8b — 記録のみ、裏取り算入はしない)。

        ``rows`` は ``src.pipeline.filters.SemanticSkip`` のインスタンス列。遅延 import
        (storage → pipeline の循環 import を避けるため、upsert_pir_spotlight /
        upsert_forecast_indicator と同型)。空入力は 0 を返す。
        """
        from src.pipeline.filters import SemanticSkip

        if not rows:
            return 0
        now_iso = _to_iso(datetime.now(UTC))
        values: list[tuple[object, ...]] = []
        for r in rows:
            if not isinstance(r, SemanticSkip):
                raise TypeError(f"expected SemanticSkip, got {type(r).__name__}")
            values.append(
                (
                    r.skipped_url,
                    r.skipped_title,
                    r.skipped_host,
                    r.feed_title,
                    r.feed_url,
                    r.tier,
                    r.matched_kind,
                    r.matched_key,
                    now_iso,
                )
            )
        with self._connect() as conn:
            conn.executemany(
                "INSERT INTO dedup_semantic_skips"
                " (skipped_url, skipped_title, skipped_host, feed_title, feed_url,"
                "  tier, matched_kind, matched_key, ts)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                values,
            )
        return len(values)

    def purge_semantic_skips(self, days: int = 90) -> int:
        """N 日より古い dedup_semantic_skips を削除する (§11 retention、dedup と連動)。"""
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM dedup_semantic_skips WHERE ts < datetime('now', ?)",
                (f"-{days} days",),
            )
            return int(cur.rowcount or 0)


def _row_to_event_note(row: object) -> EventNoteRecord:
    import json as _json

    def col(name: str) -> object:
        return row[name]  # type: ignore[index]

    raw_tags = str(col("tags") or "[]")
    try:
        tags = [str(t) for t in _json.loads(raw_tags)]
    except ValueError:
        tags = []
    return EventNoteRecord(
        item_id=str(col("item_id")),
        bookmarked=bool(col("bookmarked")),
        note=str(col("note") or ""),
        tags=tags,
        judgment=str(col("judgment") or ""),
        # 列は NOT NULL DEFAULT なので通常は必ず値がある。欠損時は now() で埋める
        # (Optional にすると呼び手全員が None 分岐を持つことになる)。
        created_at=_from_iso(str(col("created_at"))) or datetime.now(UTC),
        updated_at=_from_iso(str(col("updated_at"))) or datetime.now(UTC),
    )
