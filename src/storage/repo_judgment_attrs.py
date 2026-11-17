"""状況総括の「パターン」まとめ用に、判定 (= 状況) ごとの属性を 1 回の SQL で引く (2026-11-17)。

設計: docs/synthesis_pattern_redesign.md §3.1。属性は **評価済み (``assessed_at``) で weak でなく、
期間内に足された** 根拠記事から取る。新しく開いた状況には、まとめ記事が seed で一括紐付けされる
(属性が混ざる) ため、種別が roundup の記事は属性にも事象の根にも使わない。

使う列 (SSoT): ``articles.victim_country_iso`` / ``article_kinds.kind`` /
``article_importance_v2.severity`` /
``event_item_members`` → 根 (``COALESCE(event_items.merged_into, item_id)``、live の事象だけ)。
SQLite (dev / tests) と PostgreSQL の両方で動くよう、DISTINCT ON などは使わず Python 側で畳む。
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

_CHUNK = 400
#: 属性に使わない種別 (まとめ記事は複数の独立した事案の束で、属性が混ざる)
EXCLUDED_KIND = "roundup"
_LIVE_ORIGIN = "live"

_SQL = """
SELECT se.situation_id, se.article_id, ar.victim_country_iso, k.kind, v.severity,
       COALESCE(i.merged_into, m.item_id) AS root, i.origin
FROM situation_evidence se
LEFT JOIN articles ar ON ar.article_id = se.article_id
LEFT JOIN article_kinds k ON k.article_id = se.article_id
LEFT JOIN article_importance_v2 v ON v.article_id = se.article_id
LEFT JOIN event_item_members m ON m.article_id = se.article_id
LEFT JOIN event_items i ON i.id = m.item_id
WHERE se.situation_id IN ({ph})
  AND se.assessed_at IS NOT NULL AND se.weak_at IS NULL AND se.added_at >= ? AND se.added_at <= ?
"""  # noqa: S608


@dataclass(frozen=True)
class JudgmentAttrs:
    """1 判定の属性 (根拠記事の属性の和集合)。空の集合 = 属性なし。"""

    countries: frozenset[str] = frozenset()
    kinds: frozenset[str] = frozenset()
    severities: frozenset[str] = frozenset()
    event_roots: frozenset[str] = frozenset()


def since_iso(ts: datetime) -> str:
    """``situation_evidence.added_at`` (テキストの ISO) と文字列で比べる境界 (UTC・マイクロ秒)。

    新しい状況の根拠は総括の実行時刻 (= ``period_end``) ちょうどに足されるので、上限は秒に
    丸めず、小数部まで含めて比べる (丸めると同じ秒の ``…:02.538+00:00`` が落ちる)。
    """
    return ts.astimezone(UTC).isoformat(timespec="microseconds")


def _fold(rows: Sequence[Any]) -> dict[str, JudgmentAttrs]:
    acc: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for r in rows:
        sid, _article, country, kind, severity, root, origin = tuple(r)
        if kind == EXCLUDED_KIND:
            continue
        slots = acc[str(sid)]
        for name, value in (
            ("countries", country),
            ("kinds", kind),
            ("severities", severity),
        ):
            if value:
                slots[name].add(str(value))
        if root and origin == _LIVE_ORIGIN:
            slots["event_roots"].add(str(root))
    return {
        sid: JudgmentAttrs(**{k: frozenset(v) for k, v in slots.items()})
        for sid, slots in acc.items()
    }


class JudgmentAttrsMixin:
    """``RunHistoryRepository`` に混ぜる読み取り。"""

    def get_judgment_attrs(
        self: Any, situation_ids: Sequence[str], *, since: datetime, until: datetime
    ) -> dict[str, JudgmentAttrs]:
        """状況 id → 属性 (``since`` 以上 ``until`` 以下に足された根拠から)。

        根拠が 1 件も無い状況は返さない (呼び手は空の属性として扱う)。
        """
        ids = list(dict.fromkeys(situation_ids))
        lower, upper = since_iso(since), since_iso(until)
        rows: list[Any] = []
        with self._connect() as conn:
            for i in range(0, len(ids), _CHUNK):
                chunk = ids[i : i + _CHUNK]
                sql = _SQL.format(ph=",".join("?" * len(chunk)))
                rows.extend(conn.execute(sql, (*chunk, lower, upper)).fetchall())
        return _fold(rows)
