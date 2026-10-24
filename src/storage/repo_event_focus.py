"""事象の焦点 (``event_item_focus`` / ``event_item_focus_log``) の読み書き (2026-10-24 F0)。

焦点は ``eventnews/identity/focus.py:compute_focus`` で作り直せるキャッシュ。ここは入れ物だけ。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from src.storage.repo_base import RunHistoryRepositoryBase
from src.storage.row_mappers import _from_iso, _to_iso

_CHUNK = 400


@dataclass(frozen=True)
class StoredFocus:
    item_id: str
    rule_version: str
    anchor_article: str
    names_json: str
    kind: str
    supporters: int
    outsiders: int
    updated_at: datetime | None


def _row_to_focus(r: Any) -> StoredFocus:
    return StoredFocus(
        item_id=str(r["item_id"]),
        rule_version=str(r["rule_version"]),
        anchor_article=str(r["anchor_article"] or ""),
        names_json=str(r["names_json"]),
        kind=str(r["kind"] or ""),
        supporters=int(r["supporters"] or 0),
        outsiders=int(r["outsiders"] or 0),
        updated_at=_from_iso(r["updated_at"]),
    )


class EventFocusMixin(RunHistoryRepositoryBase):
    """``RunHistoryRepository`` に混ぜる焦点の読み書き。"""

    def list_focus_candidates(
        self: Any, *, rule_version: str, limit: int, min_members: int = 2
    ) -> list[str]:
        """焦点を (作り直して) 記録すべき生きた事象。

        メンバーが ``min_members`` 以上で、焦点が無い / 規則の版が違う /
        事象の更新が焦点より新しい。
        新しく動いた事象から (``last_reported_at`` の新しい順) ``limit`` 件まで。
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT i.id AS id FROM event_items i"
                " LEFT JOIN event_item_focus f ON f.item_id = i.id"
                " WHERE i.origin = 'live' AND i.merged_into IS NULL"
                " AND (SELECT COUNT(*) FROM event_item_members m WHERE m.item_id = i.id) >= ?"
                " AND (f.item_id IS NULL OR f.rule_version <> ? OR i.updated_at > f.updated_at)"
                " ORDER BY i.last_reported_at DESC LIMIT ?",
                (int(min_members), rule_version, int(limit)),
            ).fetchall()
        return [str(r["id"]) for r in rows]

    def list_members_in_join_order(
        self: Any, item_ids: Sequence[str]
    ) -> dict[str, list[tuple[str, str]]]:
        """事象 → [(article_id, joined_at)] (参加順)。同時刻の並びは呼び手が決める。"""
        out: dict[str, list[tuple[str, str]]] = {}
        ids = list(dict.fromkeys(item_ids))
        with self._connect() as conn:
            for i in range(0, len(ids), _CHUNK):
                chunk = ids[i : i + _CHUNK]
                ph = ",".join("?" for _ in chunk)
                for r in conn.execute(
                    "SELECT item_id, article_id, joined_at FROM event_item_members"  # noqa: S608
                    f" WHERE item_id IN ({ph}) ORDER BY joined_at ASC",
                    chunk,
                ).fetchall():
                    out.setdefault(str(r["item_id"]), []).append(
                        (str(r["article_id"]), str(r["joined_at"]))
                    )
        return out

    def get_item_focuses(self: Any, item_ids: Sequence[str]) -> dict[str, StoredFocus]:
        out: dict[str, StoredFocus] = {}
        ids = list(dict.fromkeys(item_ids))
        with self._connect() as conn:
            for i in range(0, len(ids), _CHUNK):
                chunk = ids[i : i + _CHUNK]
                ph = ",".join("?" for _ in chunk)
                for r in conn.execute(
                    "SELECT item_id, rule_version, anchor_article, names_json, kind,"  # noqa: S608
                    " supporters, outsiders, updated_at FROM event_item_focus"
                    f" WHERE item_id IN ({ph})",
                    chunk,
                ).fetchall():
                    out[str(r["item_id"])] = _row_to_focus(r)
        return out

    def upsert_item_focus(self: Any, row: StoredFocus, *, updated_at: datetime) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO event_item_focus (item_id, rule_version, anchor_article, names_json,"
                " kind, supporters, outsiders, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(item_id) DO UPDATE SET rule_version=excluded.rule_version,"
                " anchor_article=excluded.anchor_article, names_json=excluded.names_json,"
                " kind=excluded.kind, supporters=excluded.supporters,"
                " outsiders=excluded.outsiders, updated_at=excluded.updated_at",
                (
                    row.item_id,
                    row.rule_version,
                    row.anchor_article,
                    row.names_json,
                    row.kind,
                    int(row.supporters),
                    int(row.outsiders),
                    _to_iso(updated_at),
                ),
            )

    def append_focus_log(
        self: Any,
        *,
        item_id: str,
        changed_at: datetime,
        cause: str,
        decision_id: int | None,
        added_json: str,
        removed_json: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO event_item_focus_log (item_id, changed_at, cause, decision_id,"
                " added_json, removed_json) VALUES (?, ?, ?, ?, ?, ?)",
                (item_id, _to_iso(changed_at), cause, decision_id, added_json, removed_json),
            )

    def latest_merge_decision_id(
        self: Any, target_id: str, *, since: datetime
    ) -> tuple[int | None, bool]:
        """``since`` 以降に ``target_id`` を統合先とした最新の判断 (id, 戻したか)。

        無ければ (None, False)。
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, undone_at FROM event_merge_decisions"
                " WHERE target_id = ? AND (decided_at >= ? OR undone_at >= ?)"
                " ORDER BY id DESC LIMIT 1",
                (target_id, _to_iso(since), _to_iso(since)),
            ).fetchone()
        if row is None:
            return None, False
        return int(row["id"]), row["undone_at"] is not None

    def delete_focus_of_merged(self: Any) -> int:
        """吸収された事象の焦点を消す (戻して生き返れば次の回で作り直される)。"""
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM event_item_focus WHERE item_id IN"
                " (SELECT id FROM event_items WHERE merged_into IS NOT NULL)"
            )
            return int(cur.rowcount or 0)

    def purge_focus_log(self: Any, *, before: datetime) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM event_item_focus_log WHERE changed_at < ?", (_to_iso(before),)
            )
            return int(cur.rowcount or 0)

    def count_focus_audit(self: Any, *, outsider_ratio: float) -> tuple[int, int, int]:
        """(複数記事の生きた事象数, 焦点が空でないもの, 外れが ``outsider_ratio`` 以上のもの)。"""
        with self._connect() as conn:
            total = conn.execute(
                "SELECT COUNT(*) AS n FROM event_items i WHERE i.origin = 'live'"
                " AND i.merged_into IS NULL AND"
                " (SELECT COUNT(*) FROM event_item_members m WHERE m.item_id = i.id) >= 2"
            ).fetchone()["n"]
            rows = conn.execute(
                "SELECT f.names_json AS names_json, f.supporters AS s, f.outsiders AS o"
                " FROM event_item_focus f JOIN event_items i ON i.id = f.item_id"
                " WHERE i.merged_into IS NULL"
            ).fetchall()
        non_empty = sum(1 for r in rows if r["names_json"] and '"value"' in str(r["names_json"]))
        heavy = sum(
            1
            for r in rows
            if (int(r["s"]) + int(r["o"])) > 0
            and int(r["o"]) / (int(r["s"]) + int(r["o"])) >= outsider_ratio
        )
        return int(total), non_empty, heavy
