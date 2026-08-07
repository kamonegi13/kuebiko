"""外部 LLM 呼出のトークン消費記録 (llm_usage テーブル)。

接続先レジストリ拡張 (2026-07-24): anthropic / OpenAI 互換接続先の呼出ごとに
トークン数を記録し、UI のモデルタブで消費 (5h窓/今日/7日) を表示する。

消費台帳の一本化 (2026-07-26): claudecode (サブスク) も本テーブルに記録する。
従来は bridge 側 JSONL (7日保持) が claudecode の表示 SSoT だったが、保持期間・
フィールド・格納先が他 provider と非対称で drift 源になるため DB に統合した。
bridge の JSONL 自己観測は内部 debug 用として残るが、UI 表示はここから読む。
記録は fail-safe (呼出側が例外を握る) — 消費記録の失敗で LLM 呼出を壊さない。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from src.storage.repo_base import RunHistoryRepositoryBase

_JST = ZoneInfo("Asia/Tokyo")
# 表示ウィンドウ: bridge の自己観測 (5h窓=レート律速単位 / 今日 / 7日) と同じ 3 区分
_RETENTION_DAYS = 90


class LlmUsageMixin(RunHistoryRepositoryBase):
    """llm_usage テーブルの読み書き。"""

    def record_llm_usage(
        self,
        *,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        duration_ms: int,
        cache_read_tokens: int = 0,
        cost_usd: float = 0.0,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO llm_usage (ts, provider, model, input_tokens, output_tokens,"
                " duration_ms, cache_read_tokens, cost_usd) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    datetime.now(UTC).isoformat(),
                    provider[:40],
                    model[:80],
                    max(0, int(input_tokens)),
                    max(0, int(output_tokens)),
                    max(0, int(duration_ms)),
                    max(0, int(cache_read_tokens)),
                    max(0.0, float(cost_usd)),
                ),
            )

    def llm_usage_summary(self) -> dict[str, dict[str, Any]]:
        """provider 別の消費集計 {provider: {window: {...}, "last_call_at": iso|None}}。

        window = "window_5h" (レート律速の単位) / "today" (JST) / "days7"。
        各 window は calls / input_tokens / output_tokens / cache_read_tokens /
        cost_usd_equivalent を持つ — 旧 bridge usage payload と同形にして
        UI (モデルタブ Claude Code カード / 接続先パネル) の表示を共通化する。
        """
        now = datetime.now(UTC)
        windows = {
            "window_5h": (now - timedelta(hours=5)).isoformat(),
            "today": datetime.now(_JST)
            .replace(hour=0, minute=0, second=0, microsecond=0)
            .astimezone(UTC)
            .isoformat(),
            "days7": (now - timedelta(days=7)).isoformat(),
        }
        out: dict[str, dict[str, Any]] = {}
        with self._connect() as conn:
            for wname, since in windows.items():
                rows: list[Any] = conn.execute(
                    "SELECT provider, COUNT(*) AS calls, SUM(input_tokens) AS itok,"
                    " SUM(output_tokens) AS otok, SUM(cache_read_tokens) AS ctok,"
                    " SUM(cost_usd) AS cost FROM llm_usage WHERE ts >= ?"
                    " GROUP BY provider",
                    (since,),
                ).fetchall()
                for r in rows:
                    prov = str(r["provider"])
                    out.setdefault(prov, {})[wname] = {
                        "calls": int(r["calls"] or 0),
                        "input_tokens": int(r["itok"] or 0),
                        "output_tokens": int(r["otok"] or 0),
                        "cache_read_tokens": int(r["ctok"] or 0),
                        "cost_usd_equivalent": round(float(r["cost"] or 0.0), 4),
                    }
            last_rows: list[Any] = conn.execute(
                "SELECT provider, MAX(ts) AS last_ts FROM llm_usage GROUP BY provider",
            ).fetchall()
            for r in last_rows:
                prov = str(r["provider"])
                if prov in out and r["last_ts"]:
                    try:
                        out[prov]["last_call_at"] = (
                            datetime.fromisoformat(str(r["last_ts"])).astimezone(_JST).isoformat()
                        )
                    except ValueError:
                        out[prov]["last_call_at"] = None
        return out

    def purge_old_llm_usage(self, retention_days: int = _RETENTION_DAYS) -> int:
        """保持期間を超えた記録を削除する (起動時ハウスキーピング)。返り値=削除行数。"""
        cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).isoformat()
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM llm_usage WHERE ts < ?", (cutoff,))
            return int(cur.rowcount or 0)
