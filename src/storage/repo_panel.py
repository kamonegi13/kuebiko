"""シャドーパネル裁定 (panel_verdicts) と人間裁定 (panel_resolutions) の読み書き。

較正格子 P3/P4 (run_history 分割の一部、repo_tuning_labels から 2026-08-22 に分離)。
観測 (panel_verdicts — シャドー計測、本番は読まない) と判断 (panel_resolutions —
人間の裁定) は別状態 (evidence_state_separation の原則)。

裁定キューは導出値: 「パネル全員一致 or 分裂 × E1 ラベルと不一致」の verdict 行のうち
resolution が無いもの。90 日 backfill の実測 (§10.2b) でこの不一致が E1 ラベルノイズを
誤検出ゼロで指した — C5 人間裁定の正しい着弾物。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from src.storage.repo_base import RunHistoryRepositoryBase
from src.storage.row_mappers import _to_iso

# 裁定対象の合意パターン (unanimous_correct = E1 と一致 = 係争なし)
_CASE_AGREEMENTS = ("unanimous_wrong", "split")


class PanelMixin(RunHistoryRepositoryBase):
    """panel_verdicts / panel_resolutions テーブルの読み書き。"""

    def record_panel_verdict(
        self,
        *,
        case_key: str,
        field: str,
        verdicts: str,
        agreement: str,
        is_dispute: bool,
        article_id: str | None = None,
        truth_value: str | None = None,
        production_value: str | None = None,
        prompt_chars: int = 0,
        when: datetime | None = None,
        rubric_version: int | None = None,
    ) -> int | None:
        """パネル裁定を 1 行追記する。case_key 既出なら None (冪等)。

        ``rubric_version`` = 裁定時の judgment_rubric 版 (§13-17 — 版横断の regime 混合を
        C3 較正の層別で扱えるようにする)。
        """
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO panel_verdicts"
                " (case_key, article_id, field, truth_value, production_value,"
                "  verdicts, agreement, is_dispute, prompt_chars, created_at, rubric_version)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    case_key,
                    article_id,
                    field,
                    truth_value,
                    production_value,
                    verdicts,
                    agreement,
                    1 if is_dispute else 0,
                    int(prompt_chars),
                    _to_iso(when or datetime.now(UTC)),
                    rubric_version,
                ),
            )
            if (cur.rowcount or 0) == 0:
                return None
            row = conn.execute(
                "SELECT id FROM panel_verdicts WHERE case_key = ?", (case_key,)
            ).fetchone()
            return int(row["id"])

    def has_panel_verdict(self, case_key: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM panel_verdicts WHERE case_key = ?", (case_key,)
            ).fetchone()
        return row is not None

    def summarize_panel_verdicts(self) -> dict[str, Any]:
        """累計の分裂率・正解率 (較正曲線 C3 の原資、運用タブ表示用)。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT agreement, COUNT(*) AS n, AVG(prompt_chars) AS avg_chars"
                " FROM panel_verdicts GROUP BY agreement",
            ).fetchall()
        by_agreement = {
            str(r["agreement"]): {
                "n": int(r["n"]),
                "avg_prompt_chars": int(float(r["avg_chars"] or 0)),
            }
            for r in rows
        }
        judged = sum(v["n"] for k, v in by_agreement.items() if k != "error")
        splits = by_agreement.get("split", {}).get("n", 0)
        return {
            "by_agreement": by_agreement,
            "judged": judged,
            "split_rate": round(splits / judged, 3) if judged else None,
        }

    def fetch_subject_panel_cases(
        self, since_iso: str, *, confirmed_only: bool = False
    ) -> list[dict[str, Any]]:
        """E1 主体ラベル付き記事 (パネルの入力候補)。正誤の仕分けは呼び出し側で行う。

        ``confirmed_only=True`` はパネルが unanimous_correct で確認済みのラベルに限定する
        (few-shot プール §13-6 — 設計 C8 の「高確信ラベル」の実装。パネル未確認の
        ラベルを教材にしない)。

        不変条件14 (§5 ソース独立性、2026-08-22): 突合の両辺が同一ソース由来
        (``strength='self_source'``) のラベルはパネル・few-shot プールの入力候補から
        構造的に除外する — 自己突合は遠隔監督ラベルとして数えない。
        """
        sql = (
            "SELECT tl.article_id, tl.label_value AS truth,"
            " a.title, a.body, a.category, a.published_at,"
            " COALESCE(a.subject_actor_ids, '') AS production"
            " FROM tuning_labels tl JOIN articles a ON a.article_id = tl.article_id"
            " WHERE tl.field = 'subject_actor' AND tl.source = 'E1'"
            "   AND tl.superseded_by IS NULL AND tl.arrived_at >= ?"
            "   AND LENGTH(COALESCE(a.body, '')) >= 500"
            "   AND (tl.strength IS NULL OR tl.strength <> 'self_source')"
        )
        if confirmed_only:
            sql += (
                " AND EXISTS (SELECT 1 FROM panel_verdicts pv"
                "  WHERE pv.article_id = tl.article_id AND pv.field = tl.field"
                "    AND pv.truth_value = tl.label_value"
                "    AND pv.agreement = 'unanimous_correct')"
            )
        with self._connect() as conn:
            rows = conn.execute(sql, (since_iso,)).fetchall()
        return [
            {
                "article_id": str(r["article_id"]),
                "truth": str(r["truth"]),
                "title": str(r["title"] or ""),
                "body": str(r["body"] or ""),
                "category": str(r["category"] or ""),
                "published": str(r["published_at"]) if r["published_at"] else None,
                "production": str(r["production"]),
            }
            for r in rows
        ]

    def export_panel_verdicts(self) -> list[dict[str, Any]]:
        """全裁定の schema 非依存 dump (恒久資産エクスポート §13-3 用)。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, case_key, article_id, field, truth_value, production_value,"
                " verdicts, agreement, is_dispute, prompt_chars, created_at, rubric_version"
                " FROM panel_verdicts ORDER BY id",
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------- P4: 裁定キュー (導出) + 人間裁定 ----------

    def get_panel_verdict_case(self, case_key: str) -> dict[str, Any] | None:
        """1 係争の全容 (裁定サービス用)。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT case_key, article_id, field, truth_value, production_value,"
                " verdicts, agreement, is_dispute, created_at"
                " FROM panel_verdicts WHERE case_key = ?",
                (case_key,),
            ).fetchone()
        return self._case_row(row) if row is not None else None

    def list_pending_adjudications(self) -> list[dict[str, Any]]:
        """裁定待ち = 係争パターンの verdict で resolution が無いもの (古い順)。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT pv.case_key, pv.article_id, pv.field, pv.truth_value,"
                " pv.production_value, pv.verdicts, pv.agreement, pv.is_dispute,"
                " pv.created_at, a.title"
                " FROM panel_verdicts pv"
                " LEFT JOIN panel_resolutions pr ON pr.case_key = pv.case_key"
                " LEFT JOIN articles a ON a.article_id = pv.article_id"
                " WHERE pv.agreement IN (?, ?) AND pr.case_key IS NULL"
                " ORDER BY pv.created_at ASC",
                _CASE_AGREEMENTS,
            ).fetchall()
        return [self._case_row(r, with_title=True) for r in rows]

    def insert_panel_resolution(
        self,
        *,
        case_key: str,
        resolution: str,
        resolved_by: str = "manual",
        when: datetime | None = None,
    ) -> bool:
        """裁定を 1 件記録する (1 係争 1 裁定 — 既出なら False)。"""
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO panel_resolutions"
                " (case_key, resolution, resolved_by, created_at) VALUES (?, ?, ?, ?)",
                (case_key, resolution, resolved_by, _to_iso(when or datetime.now(UTC))),
            )
            return (cur.rowcount or 0) > 0

    def list_recent_resolutions(self, limit: int = 10) -> list[dict[str, Any]]:
        """裁定履歴 (新しい順、運用カード表示用)。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT pr.case_key, pr.resolution, pr.resolved_by, pr.created_at,"
                " pv.article_id, pv.field, pv.truth_value, pv.agreement"
                " FROM panel_resolutions pr"
                " JOIN panel_verdicts pv ON pv.case_key = pr.case_key"
                " ORDER BY pr.created_at DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [
            {
                "case_key": str(r["case_key"]),
                "resolution": str(r["resolution"]),
                "resolved_by": str(r["resolved_by"]),
                "created_at": str(r["created_at"]),
                "article_id": str(r["article_id"]) if r["article_id"] is not None else None,
                "field": str(r["field"]),
                "truth_value": str(r["truth_value"] or ""),
                "agreement": str(r["agreement"]),
            }
            for r in rows
        ]

    @staticmethod
    def _case_row(r: Any, *, with_title: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {
            "case_key": str(r["case_key"]),
            "article_id": str(r["article_id"]) if r["article_id"] is not None else None,
            "field": str(r["field"]),
            "truth_value": str(r["truth_value"] or ""),
            "production_value": str(r["production_value"] or ""),
            "verdicts": str(r["verdicts"]),
            "agreement": str(r["agreement"]),
            "is_dispute": bool(r["is_dispute"]),
            "created_at": str(r["created_at"]),
        }
        if with_title:
            out["title"] = str(r["title"] or "")
        return out
