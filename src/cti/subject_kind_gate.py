"""種別が勧告・統計・まとめの記事に付いた信頼できる主題の関門 (SUBJECT-KIND-SHADOW, 2026-11-14)。

背景: GraphRAG E6 の規則 C4 (記事の種別が advisory / stats / roundup なのに信頼できる主題が
付いている) は、Opus の確認で 8 件中 6 件が誤り、28 日で 41 件。種別 (event_kind) は主題の付与
(永続化直前) より後、毎時の事象ニュースの段で付くので、関門は種別が決まる時点で当てる。

旗 ``SUBJECT_KIND_GATE``: ``0`` = 従来 / ``shadow`` (既定) = 当たる記事をログ ``subject_kind_gate``
(記事 id・種別・主題の数、本文なし) に残すだけ / ``1`` = 事象の指標 (relation_features) の
信頼できる主題から外す。SQL 集計 (``trusted_subject_clause``) は種別を持たないため 1 でも不変。
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from typing import Any, Final

from src.logging_config import get_logger

_log = get_logger(__name__)

GATE_ENV: Final = "SUBJECT_KIND_GATE"
MODE_OFF: Final = "0"
MODE_SHADOW: Final = "shadow"
MODE_ON: Final = "1"

#: 信頼できる主題を持つべきでない記事の種別 (E6 の C4 と同じ。scripts/graphrag_eval/e6_logic.py)
NON_ACTOR_KINDS: Final[frozenset[str]] = frozenset({"advisory", "stats", "roundup"})
_CHUNK: Final = 500


def gate_mode() -> str:
    """未設定・不明値は shadow (記録のみ)。"""
    raw = os.environ.get(GATE_ENV, MODE_SHADOW).strip().lower()
    if raw in (MODE_OFF, "off"):
        return MODE_OFF
    if raw in (MODE_ON, "on"):
        return MODE_ON
    return MODE_SHADOW


def is_hit(kind: str, trusted_subjects: Iterable[str]) -> bool:
    return kind in NON_ACTOR_KINDS and bool(set(trusted_subjects))


def effective_subjects(kind: str, subjects: set[str], *, mode: str | None = None) -> set[str]:
    """mode=1 で当たる記事は主題を空にする。それ以外は入力のまま。"""
    if (mode or gate_mode()) == MODE_ON and is_hit(kind, subjects):
        return set()
    return subjects


def _load_rows(repo: Any, ids: list[str]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    with repo._connect() as conn:  # noqa: SLF001
        for i in range(0, len(ids), _CHUNK):
            chunk = ids[i : i + _CHUNK]
            ph = ",".join("?" for _ in chunk)
            for r in conn.execute(
                "SELECT article_id, MIN(subject_actor_ids) AS subject_actor_ids,"  # noqa: S608
                " MIN(subject_actor_source) AS subject_actor_source,"
                " MIN(subject_actor_confidence) AS subject_actor_confidence"
                f" FROM articles WHERE article_id IN ({ph}) GROUP BY article_id",
                chunk,
            ).fetchall():
                out[str(r["article_id"])] = dict(r)
    return out


def record_hits(repo: Any, kinds: Mapping[str, str]) -> int:
    """種別が決まった記事のうち C4 に当たるものをログに残す (shadow / 1)。件数を返す。"""
    if gate_mode() == MODE_OFF:
        return 0
    cand = [a for a, k in kinds.items() if k in NON_ACTOR_KINDS]
    if not cand:
        return 0
    from src.assessment.situation_track import registry_is_group
    from src.eventnews.relation_features import _trusted_subjects  # noqa: PLC2701

    is_group = registry_is_group()
    rows = _load_rows(repo, cand)
    hits = 0
    for aid in cand:
        subj = _trusted_subjects(rows.get(aid, {}), is_group)
        if is_hit(kinds[aid], subj):
            hits += 1
            _log.info(
                "subject_kind_gate",
                article_id=aid,
                kind=kinds[aid],
                subjects=len(subj),
                mode=gate_mode(),
            )
    return hits
