"""情勢台帳の読み書きのうち 2026-09-27 に足したもの (situation_store.py の 800 行超過を増やさない)。

- 弱い証拠の印 (weak_at) と、印を付けるための規則割当の読み出し
- 台帳の型 (track) と、型の判定に使う記事の主題
- 記事ごとの情勢 (記事画面から台帳への導線)

``SituationStore`` が継承する mixin。``self._repo`` (RunHistoryRepository) を前提にする。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.storage.run_history import RunHistoryRepository

#: 評価 (ACH・総括・深掘り) の読み取りから弱い証拠を外す条件
NOT_WEAK = "weak_at IS NULL"


class SituationMarksMixin:
    """弱い証拠・台帳の型・記事ごとの情勢。"""

    _repo: RunHistoryRepository

    def situations_for_article(self, article_id: str) -> list[dict[str, str]]:
        """この記事を証拠にしている情勢 (弱い証拠・統合済みの墓標は除く)。記事画面の導線用。"""
        with self._repo._connect() as conn:  # noqa: SLF001
            rows = conn.execute(
                "SELECT s.situation_id, s.title, s.status, s.track, e.polarity, e.assessed_at"
                " FROM situation_evidence e JOIN situations s ON s.situation_id = e.situation_id"
                " WHERE e.article_id=? AND e.weak_at IS NULL"
                " AND (s.merged_into IS NULL OR s.merged_into = '')"
                " ORDER BY s.last_evidence_at DESC",
                (article_id,),
            ).fetchall()
        return [
            {
                "situation_id": str(r["situation_id"]),
                "title": str(r["title"]),
                "status": str(r["status"]),
                "track": str(r["track"] or ""),
                # 評価済み (ACH が引用) の行だけ向きが意味を持つ。未評価は空
                "polarity": str(r["polarity"]) if r["assessed_at"] else "",
            }
            for r in rows
        ]

    def set_track(self, situation_id: str, track: str) -> None:
        """台帳の型を記録する ('actor' / 'campaign')。"""
        with self._repo._connect() as conn:  # noqa: SLF001
            conn.execute(
                "UPDATE situations SET track=? WHERE situation_id=?", (track, situation_id)
            )

    def subjects_for_articles(self, article_ids: list[str]) -> dict[str, tuple[str, str, str]]:
        """記事ごとの主題 (subject_actor_ids, source, confidence)。台帳の型の判定用。"""
        if not article_ids:
            return {}
        ph = ",".join("?" for _ in article_ids)
        with self._repo._connect() as conn:  # noqa: SLF001
            rows = conn.execute(
                "SELECT article_id, subject_actor_ids, subject_actor_source,"  # noqa: S608
                f" subject_actor_confidence FROM articles WHERE article_id IN ({ph})",
                list(article_ids),
            ).fetchall()
        return {
            str(r["article_id"]): (
                str(r["subject_actor_ids"] or ""),
                str(r["subject_actor_source"] or ""),
                str(r["subject_actor_confidence"] or ""),
            )
            for r in rows
        }

    def unmarked_evidence_by_rule(
        self, situation_ids: list[str], *, rules: Sequence[str]
    ) -> dict[str, list[str]]:
        """規則 (assigned_by) で入った未印の証拠 ({situation_id: [article_id]})。弱い印の採点用。"""
        if not situation_ids or not rules:
            return {}
        ph = ",".join("?" for _ in situation_ids)
        rph = ",".join("?" for _ in rules)
        with self._repo._connect() as conn:  # noqa: SLF001
            rows = conn.execute(
                "SELECT situation_id, article_id FROM situation_evidence"  # noqa: S608 — ph は ? 固定
                f" WHERE situation_id IN ({ph}) AND assigned_by IN ({rph}) AND {NOT_WEAK}"
                " ORDER BY situation_id, article_id",
                [*situation_ids, *rules],
            ).fetchall()
        out: dict[str, list[str]] = {}
        for r in rows:
            out.setdefault(str(r["situation_id"]), []).append(str(r["article_id"]))
        return out

    def mark_weak(self, pairs: list[tuple[str, str]], *, weak_at: str) -> int:
        """(situation_id, article_id) の証拠に弱い印を刻む (未印の行のみ・冪等)。件数を返す。"""
        n = 0
        with self._repo._connect() as conn:  # noqa: SLF001
            for sid, aid in pairs:
                cur = conn.execute(
                    "UPDATE situation_evidence SET weak_at=?"
                    " WHERE situation_id=? AND article_id=? AND weak_at IS NULL",
                    (weak_at, sid, aid),
                )
                n += max(0, int(cur.rowcount or 0))
        return n
