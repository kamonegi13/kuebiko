"""PIR LLM 主題判定 verdict の永続化 (docs/pir_concept_llm_judge_design.md §4.3)。

verdict は「取込 LLM の category/intent と同格の永続化された意味的事実」。
負の verdict も保存する — 不適合候補を毎日再判定しないための要。
pir_rev (PIR description+question ハッシュ) が変わった行は stale として再判定対象になる。
"""

from __future__ import annotations

from datetime import UTC, datetime

from src.storage.repo_base import RunHistoryRepositoryBase


class PirJudgmentsMixin(RunHistoryRepositoryBase):
    """pir_llm_judgments テーブルの読み書き。"""

    def upsert_pir_llm_judgment(
        self,
        article_id: str,
        pir_id: str,
        *,
        matched: bool,
        reason: str,
        pir_rev: str,
    ) -> None:
        """verdict を upsert する (正負とも保存、再判定で上書き)。"""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO pir_llm_judgments
                  (article_id, pir_id, matched, reason, pir_rev, judged_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(article_id, pir_id) DO UPDATE SET
                  matched=excluded.matched,
                  reason=excluded.reason,
                  pir_rev=excluded.pir_rev,
                  judged_at=excluded.judged_at
                """,
                (
                    article_id,
                    pir_id,
                    1 if matched else 0,
                    (reason or "")[:200],
                    pir_rev,
                    datetime.now(UTC).isoformat(),
                ),
            )

    def fetch_pir_llm_state(self, pir_id: str) -> dict[str, str]:
        """指定 PIR の判定済み article_id → pir_rev (正負とも)。

        judge バッチが「未判定 or stale」を絞り込むための状態。
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT article_id, pir_rev FROM pir_llm_judgments WHERE pir_id = ?",
                (pir_id,),
            ).fetchall()
        return {str(r["article_id"]): str(r["pir_rev"]) for r in rows}

    def pir_judgment_rates_since(self, since_iso: str) -> dict[str, tuple[int, int]]:
        """pir_id → (判定数, 適合数) を judged_at >= since で集計する (週次監査用)。

        監査 2026-08-01 ③: apt_leak が match 率 1.4% で 10 日 ~800 回 LLM を空振り
        させても、backlog しか報告しない既存監査からは見えなかった穴の閉鎖。
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT pir_id, COUNT(*) AS judged,"
                " COUNT(CASE WHEN matched = 1 THEN 1 END) AS matched"
                " FROM pir_llm_judgments WHERE judged_at >= ? GROUP BY pir_id",
                (since_iso,),
            ).fetchall()
        return {str(r["pir_id"]): (int(r["judged"]), int(r["matched"])) for r in rows}

    def fetch_pir_llm_confirmed(self) -> dict[str, frozenset[str]]:
        """適合 verdict (matched=1) の article_id → pir_id 集合 (全 PIR 分)。

        evaluator が match 合成に使う。テーブルは候補ゲート通過分のみで小さい
        (数百〜数千行) ため全件ロードで足りる。stale (pir_rev 旧) の verdict も
        再判定までは有効として返す (照合を空白にしない eventual consistency)。
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT article_id, pir_id FROM pir_llm_judgments WHERE matched = 1",
            ).fetchall()
        out: dict[str, set[str]] = {}
        for r in rows:
            out.setdefault(str(r["article_id"]), set()).add(str(r["pir_id"]))
        return {aid: frozenset(pids) for aid, pids in out.items()}
