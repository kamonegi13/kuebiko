"""統合の判断記録 (``event_merge_decisions``) と、統合を戻すための読み書き (2026-10-22)。

統合 (``eventnews/merge.py``) は吸収側の行を残す (``merged_into`` を立てるだけ) ので、
データ上は戻せる。だが **どの辺・どの確率・どの関門の状態で結んだか** が残らず、誤りの型を
調べるたびに再現スクリプトを作り直していた。ここはその記録と、戻す操作に要る最小の読み書き。

- 1 行 = 吸収された事象 1 つ。``edges_json`` はその事象を結んだ記事対 (cos・ML 確率つき)。
- 戻したら ``undone_at`` を立てる (行は消さない = 戻した事実も監査できる)。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from src.storage.repo_base import RunHistoryRepositoryBase
from src.storage.row_mappers import _from_iso, _to_iso


class _VetoLike(Protocol):
    """``eventnews.merge.VetoedLink`` の形 (storage 層から eventnews を import しない)。"""

    @property
    def item_a(self) -> str: ...
    @property
    def item_b(self) -> str: ...
    @property
    def klass(self) -> str: ...
    @property
    def detail(self) -> str: ...
    @property
    def edge_count(self) -> int: ...
    @property
    def enforced(self) -> bool: ...


_CHUNK = 400
#: 照合で止めた組の記録 (event_merge_vetoes) の action
ACTION_BLOCKED = "blocked"
ACTION_SHADOW = "shadow"
ACTION_RELATION_CANDIDATE = "relation_candidate"
#: 吸収の連鎖 (A→B→C…) をたどる上限。循環の保険でもある
MAX_ROOT_HOPS = 8


@dataclass(frozen=True)
class MergeDecisionRow:
    """統合の判断 1 件 (吸収された事象 1 つ分)。"""

    target_id: str
    absorbed_id: str
    run_kind: str  # "hourly" | "manual"
    edges_json: str
    edge_count: int
    rescued: bool
    flags_json: str
    decided_at: datetime | None = None
    undone_at: datetime | None = None
    undone_reason: str = ""
    #: 焦点の照合 (support / weak / conflict / lineage、空 = 未計算) と両側の焦点の写し。
    #: F0 では記録だけで判断には使わない (docs/eventnews_focus_redesign.md §3.4)
    focus_class: str = ""
    focus_json: str = ""


@dataclass(frozen=True)
class MergeVetoRow:
    """焦点の照合で止めた (または止める対象だった) 事象の組 1 つ。"""

    item_a: str
    item_b: str
    klass: str
    detail: str
    edge_count: int
    action: str  # blocked | shadow | relation_candidate
    mode: str  # その時の EVENTNEWS_MERGE_FOCUS_GATE
    first_seen_at: datetime | None
    last_seen_at: datetime | None
    times_seen: int
    sampled_at: datetime | None


def _veto_action(veto: _VetoLike) -> str:
    if veto.enforced:
        return ACTION_BLOCKED
    return ACTION_SHADOW if veto.klass == "conflict" else ACTION_RELATION_CANDIDATE


def _row_to_veto(r: Any) -> MergeVetoRow:
    return MergeVetoRow(
        item_a=str(r["item_a"]),
        item_b=str(r["item_b"]),
        klass=str(r["klass"]),
        detail=str(r["detail"] or ""),
        edge_count=int(r["edge_count"] or 0),
        action=str(r["action"]),
        mode=str(r["mode"] or ""),
        first_seen_at=_from_iso(r["first_seen_at"]),
        last_seen_at=_from_iso(r["last_seen_at"]),
        times_seen=int(r["times_seen"] or 0),
        sampled_at=_from_iso(r["sampled_at"]),
    )


def _row_to_decision(r: Any) -> MergeDecisionRow:
    return MergeDecisionRow(
        target_id=str(r["target_id"]),
        absorbed_id=str(r["absorbed_id"]),
        run_kind=str(r["run_kind"]),
        edges_json=str(r["edges_json"]),
        edge_count=int(r["edge_count"] or 0),
        rescued=bool(r["rescued"]),
        flags_json=str(r["flags_json"]),
        decided_at=_from_iso(r["decided_at"]),
        undone_at=_from_iso(r["undone_at"]),
        undone_reason=str(r["undone_reason"] or ""),
        focus_class=str(r["focus_class"] or ""),
        focus_json=str(r["focus_json"] or ""),
    )


class MergeDecisionsMixin(RunHistoryRepositoryBase):
    """``event_merge_decisions`` と、統合の後始末に使う event_item_members の操作。"""

    def record_merge_decisions(
        self: Any, rows: Sequence[MergeDecisionRow], *, decided_at: datetime
    ) -> int:
        """判断を追記する。戻り値は書いた行数。"""
        if not rows:
            return 0
        with self._connect() as conn:
            for r in rows:
                conn.execute(
                    "INSERT INTO event_merge_decisions (decided_at, run_kind, target_id,"
                    " absorbed_id, edges_json, edge_count, rescued, flags_json,"
                    " focus_class, focus_json)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        _to_iso(decided_at),
                        r.run_kind,
                        r.target_id,
                        r.absorbed_id,
                        r.edges_json,
                        int(r.edge_count),
                        1 if r.rescued else 0,
                        r.flags_json,
                        r.focus_class,
                        r.focus_json,
                    ),
                )
        return len(rows)

    def record_merge_vetoes(
        self: Any, vetoes: Sequence[_VetoLike], *, seen_at: datetime, mode: str
    ) -> int:
        """照合で止めた組を残す。同じ組は毎時の再検出で行を増やさず ``times_seen`` を進める。"""
        if not vetoes:
            return 0
        now = _to_iso(seen_at)
        with self._connect() as conn:
            for v in vetoes:
                a, b = (v.item_a, v.item_b) if v.item_a <= v.item_b else (v.item_b, v.item_a)
                conn.execute(
                    "INSERT INTO event_merge_vetoes (item_a, item_b, klass, detail, edge_count,"
                    " action, mode, first_seen_at, last_seen_at, times_seen)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)"
                    " ON CONFLICT(item_a, item_b) DO UPDATE SET klass=excluded.klass,"
                    " detail=excluded.detail, edge_count=excluded.edge_count,"
                    " action=excluded.action, mode=excluded.mode,"
                    " last_seen_at=excluded.last_seen_at,"
                    " times_seen=event_merge_vetoes.times_seen + 1",
                    (a, b, v.klass, v.detail, int(v.edge_count), _veto_action(v), mode, now, now),
                )
        return len(vetoes)

    def list_merge_vetoes(
        self: Any,
        *,
        since: datetime,
        actions: Sequence[str] | None = None,
        unsampled_only: bool = False,
        limit: int = 5000,
    ) -> list[MergeVetoRow]:
        """``since`` 以降に見た組 (新しい順)。``actions`` で絞り、抽出済みを除ける。"""
        where = ["last_seen_at >= ?"]
        params: list[object] = [_to_iso(since)]
        if actions:
            where.append(f"action IN ({', '.join('?' for _ in actions)})")
            params.extend(actions)
        if unsampled_only:
            where.append("sampled_at IS NULL")
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT item_a, item_b, klass, detail, edge_count, action, mode,"  # noqa: S608
                " first_seen_at, last_seen_at, times_seen, sampled_at FROM event_merge_vetoes"
                f" WHERE {' AND '.join(where)} ORDER BY last_seen_at DESC, item_a, item_b LIMIT ?",
                (*params, int(limit)),
            ).fetchall()
        return [_row_to_veto(r) for r in rows]

    def mark_merge_vetoes_sampled(
        self: Any, pairs: Sequence[tuple[str, str]], *, sampled_at: datetime
    ) -> int:
        """裁定に回した組に印を付ける (次の抽出で重ならない)。"""
        done = 0
        with self._connect() as conn:
            for x, y in pairs:
                a, b = (x, y) if x <= y else (y, x)
                cur = conn.execute(
                    "UPDATE event_merge_vetoes SET sampled_at=?"
                    " WHERE item_a=? AND item_b=? AND sampled_at IS NULL",
                    (_to_iso(sampled_at), a, b),
                )
                done += int(cur.rowcount or 0)
        return done

    def list_merge_decisions(
        self: Any,
        *,
        target_id: str | None = None,
        absorbed_id: str | None = None,
        active_only: bool = False,
        limit: int = 1000,
    ) -> list[MergeDecisionRow]:
        """新しい順。``active_only`` は戻していないものだけ。"""
        where: list[str] = []
        params: list[object] = []
        if target_id is not None:
            where.append("target_id = ?")
            params.append(target_id)
        if absorbed_id is not None:
            where.append("absorbed_id = ?")
            params.append(absorbed_id)
        if active_only:
            where.append("undone_at IS NULL")
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT target_id, absorbed_id, run_kind, edges_json, edge_count, rescued,"  # noqa: S608
                " flags_json, decided_at, undone_at, undone_reason, focus_class, focus_json"
                " FROM event_merge_decisions"
                f"{clause} ORDER BY decided_at DESC, id DESC LIMIT ?",
                (*params, int(limit)),
            ).fetchall()
        return [_row_to_decision(r) for r in rows]

    def mark_merge_decisions_undone(
        self: Any, absorbed_id: str, *, reason: str, undone_at: datetime
    ) -> int:
        """吸収された事象の、まだ戻していない判断を「戻した」にする。"""
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE event_merge_decisions SET undone_at=?, undone_reason=?"
                " WHERE absorbed_id=? AND undone_at IS NULL",
                (_to_iso(undone_at), reason, absorbed_id),
            )
            return int(cur.rowcount or 0)

    def count_merge_decisions_since(self: Any, since: datetime) -> dict[str, int]:
        """``since`` 以降の判断の件数 (戻していない / 戻した)。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT undone_at FROM event_merge_decisions WHERE decided_at >= ?",
                (_to_iso(since),),
            ).fetchall()
        undone = sum(1 for r in rows if r["undone_at"] is not None)
        return {"active": len(rows) - undone, "undone": undone}

    def delete_event_members(
        self: Any, item_id: str, article_ids: Sequence[str], *, only_join_signal: str
    ) -> int:
        """事象のメンバー行を消す。``only_join_signal`` の行だけ (元からのメンバーは守る)。"""
        ids = list(dict.fromkeys(article_ids))
        removed = 0
        with self._connect() as conn:
            for i in range(0, len(ids), _CHUNK):
                chunk = ids[i : i + _CHUNK]
                ph = ",".join("?" for _ in chunk)
                cur = conn.execute(
                    "DELETE FROM event_item_members WHERE item_id=? AND join_signal=?"  # noqa: S608
                    f" AND article_id IN ({ph})",
                    (item_id, only_join_signal, *chunk),
                )
                removed += int(cur.rowcount or 0)
        return removed

    def event_roots_by_article(self: Any, article_ids: Sequence[str]) -> dict[str, str]:
        """記事 → 属する事象の根 (``merged_into`` をたどった先)。事象に入っていない記事は欠ける。

        同じ記事が複数の行 (吸収側と統合先) に載るが、根へたどれば同じ事象になる。
        食い違うときは ID の小さい方 (決定論)。
        """
        ids = list(dict.fromkeys(article_ids))
        if not ids:
            return {}
        item_of: dict[str, set[str]] = {}
        with self._connect() as conn:
            for i in range(0, len(ids), _CHUNK):
                chunk = ids[i : i + _CHUNK]
                ph = ",".join("?" for _ in chunk)
                for r in conn.execute(
                    "SELECT article_id, item_id FROM event_item_members"  # noqa: S608
                    f" WHERE article_id IN ({ph})",
                    chunk,
                ).fetchall():
                    item_of.setdefault(str(r["article_id"]), set()).add(str(r["item_id"]))
            parent = _merged_into_map(conn, {i for s in item_of.values() for i in s})
        return {aid: min(_follow(parent, it) for it in items) for aid, items in item_of.items()}


def _merged_into_map(conn: Any, start: set[str]) -> dict[str, str]:
    """``start`` から ``merged_into`` を ``MAX_ROOT_HOPS`` 段まで引いた item → 親の対応。"""
    parent: dict[str, str] = {}
    frontier = set(start)
    for _ in range(MAX_ROOT_HOPS):
        todo = sorted(frontier - parent.keys())
        if not todo:
            break
        nxt: set[str] = set()
        for i in range(0, len(todo), _CHUNK):
            chunk = todo[i : i + _CHUNK]
            ph = ",".join("?" for _ in chunk)
            for r in conn.execute(
                f"SELECT id, merged_into FROM event_items WHERE id IN ({ph})",  # noqa: S608
                chunk,
            ).fetchall():
                if r["merged_into"]:
                    parent[str(r["id"])] = str(r["merged_into"])
                    nxt.add(str(r["merged_into"]))
                else:
                    parent.setdefault(str(r["id"]), "")
        frontier = nxt
    return parent


def _follow(parent: dict[str, str], item_id: str) -> str:
    cur = item_id
    for _ in range(MAX_ROOT_HOPS):
        nxt = parent.get(cur, "")
        if not nxt:
            return cur
        cur = nxt
    return cur


__all__ = ["MAX_ROOT_HOPS", "MergeDecisionRow", "MergeDecisionsMixin"]
