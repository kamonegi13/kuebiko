"""重要度の再設計の記録 (2026-10-03、``src/cti/importance_v2.py``)。

材料 (記事の種類・深刻度の軸・CVE・関与国・SIR の該当) を保存済みの値から集め、導出結果を
``article_importance_v2`` に 1 記事 1 行で残す。いまの ``articles.importance`` は触らない。
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from src.cti.importance_v2 import ImportanceInputs, ImportanceV2

_CHUNK = 400
_ENTITY_TYPES = ("cve", "involved_country", "mentioned_country", "pir")

#: 記録の対象 = 深刻度の軸が付いた記事のうち、未記録か古い版の記録のもの。
#: articles は同じ article_id が複数行ありうるので最新の 1 行に絞る (ROW_NUMBER が両 DB で可搬)
_PENDING_SQL = """
SELECT article_id, category, article_type, victim_country_iso FROM (
    SELECT a.article_id, a.category, a.article_type, a.victim_country_iso,
           ROW_NUMBER() OVER (PARTITION BY a.article_id ORDER BY a.created_at DESC) AS rn
    FROM articles a
    JOIN article_severity_axes s ON s.article_id = a.article_id
    LEFT JOIN article_importance_v2 v ON v.article_id = a.article_id
    WHERE a.status = 'posted' AND a.created_at >= ?
      AND (v.article_id IS NULL OR v.rule_version <> ?)
) t WHERE rn = 1
LIMIT ?
"""


class ImportanceV2Mixin:
    """``RunHistoryRepository`` に混ぜる読み書き。"""

    def pending_importance_inputs(
        self: Any, *, since: str, rule_version: str, limit: int
    ) -> dict[str, ImportanceInputs]:
        """記録すべき記事の材料を集める (記事 id → 材料)。"""
        from src.tools.kev_client import get_kev_cve_set
        from src.tools.nvd_client import max_cvss

        with self._connect() as conn:
            rows = conn.execute(_PENDING_SQL, (since, rule_version, limit)).fetchall()
        if not rows:
            return {}
        ids = [str(r["article_id"]) for r in rows]
        axes = self.get_severity_axes(ids)
        ents = self._importance_entities(ids)
        kev = get_kev_cve_set()
        out: dict[str, ImportanceInputs] = {}
        for r in rows:
            aid = str(r["article_id"])
            e = ents.get(aid, {})
            cves = sorted(e.get("cve", set()))
            out[aid] = ImportanceInputs(
                category=str(r["category"] or ""),
                article_type=str(r["article_type"] or ""),
                axes=axes.get(aid, {}),
                on_kev=any(c in kev for c in cves),
                max_cvss=max_cvss(cves),
                victim_country=str(r["victim_country_iso"] or "").upper(),
                involved_countries=frozenset(e.get("involved_country", set())),
                mentioned_countries=frozenset(e.get("mentioned_country", set())),
                sir_ids=frozenset(e.get("pir", set())),
            )
        return out

    def _importance_entities(
        self: Any, article_ids: Sequence[str]
    ) -> dict[str, dict[str, set[str]]]:
        out: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
        types_ph = ",".join("?" * len(_ENTITY_TYPES))
        with self._connect() as conn:
            for i in range(0, len(article_ids), _CHUNK):
                chunk = list(article_ids[i : i + _CHUNK])
                ph = ",".join("?" * len(chunk))
                for r in conn.execute(
                    "SELECT article_id, entity_type, value FROM article_entities "  # noqa: S608
                    f"WHERE entity_type IN ({types_ph}) AND article_id IN ({ph})",
                    (*_ENTITY_TYPES, *chunk),
                ).fetchall():
                    et, v = str(r["entity_type"]), str(r["value"])
                    out[str(r["article_id"])][et].add(v if et == "pir" else v.upper())
        return out

    def save_importance_v2(self: Any, article_id: str, rec: ImportanceV2) -> None:
        """記録する (同じ記事は最新の版で上書き)。"""
        values = (
            article_id,
            rec.severity,
            rec.severity_basis,
            rec.strategic_weight,
            rec.jp,
            ",".join(rec.nations),
            ",".join(rec.sir_ids),
            1 if rec.relevant else 0,
            rec.rule_version,
            datetime.now(UTC).isoformat(),
        )
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO article_importance_v2 (article_id, severity, severity_basis,"
                " strategic_weight, jp, nations, sir_ids, relevant, rule_version, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(article_id) DO UPDATE SET severity=excluded.severity,"
                " severity_basis=excluded.severity_basis,"
                " strategic_weight=excluded.strategic_weight, jp=excluded.jp,"
                " nations=excluded.nations, sir_ids=excluded.sir_ids,"
                " relevant=excluded.relevant, rule_version=excluded.rule_version,"
                " created_at=excluded.created_at",
                values,
            )

    def importance_v2_crosstab(self: Any, *, since: str) -> list[dict[str, Any]]:
        """いまの重要度 × 新しい値の件数 (比較の画面用)。"""
        sql = """
        SELECT importance, severity, strategic_weight, relevant, COUNT(*) AS n FROM (
            SELECT a.importance, v.severity, v.strategic_weight, v.relevant,
                   ROW_NUMBER() OVER (PARTITION BY a.article_id ORDER BY a.created_at DESC) AS rn
            FROM articles a JOIN article_importance_v2 v ON v.article_id = a.article_id
            WHERE a.status = 'posted' AND a.created_at >= ?
        ) t WHERE rn = 1
        GROUP BY importance, severity, strategic_weight, relevant
        """
        with self._connect() as conn:
            rows = conn.execute(sql, (since,)).fetchall()
        return [
            {
                "importance": str(r["importance"] or ""),
                "severity": r["severity"],
                "strategic_weight": r["strategic_weight"],
                "relevant": bool(r["relevant"]),
                "count": int(r["n"]),
            }
            for r in rows
        ]
