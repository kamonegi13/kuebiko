"""横断 assessment context (国家相関 / forecast 指標 / 鮮度) の builder 群。

これらは元々 ``src/synthesis/generator.py`` の ``_build_*`` だったものを、複数の
synthesis ファミリーサーフェス (synthesis / spotlight) が**同一の見立て**を共有できるよう
抽出したもの。ロジックは behavior-preserving に移設している。

各 builder は障害時に空 ([] / {}) を返し、呼び出し側 (synthesis / spotlight) を止めない。
報告ストリーム (published/created 時刻) 基準であり事象の実発生時刻ではない。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.logging_config import get_logger

_log = get_logger(__name__)


def build_nation_correlation(
    *,
    window_days: int,
    db_path: Path,
    top_n: int = 7,
) -> list[dict[str, Any]]:
    """国家横断 サイバー↔地政学 相関を集約 (#1 nation correlation)。

    国家情勢サービス (situation) を再利用し、各国を 4 レーンに **分離** して返す:
    帰属サイバー (actor.nation) / サイバー言及 (i_cyber×当事国×actor無し、政策中心) /
    標的 (victim) / 地政学 (involved_country)。検証 (2026-06-27) で「言及」レーンは
    ~65% が政策・態勢の言及なので、prompt 側で実作戦と混同させない (姿勢シグナル扱い)。

    報告ストリーム (published/created 時刻) 基準であり事象の実発生時刻ではない。
    PIR 同様 障害時は [] を返し呼び出し側を止めない。
    """
    try:
        from src.ui.services.situation import list_nations, situation_by_nation

        nations = list_nations(window_days=window_days, db_path=db_path)
    except Exception:  # noqa: BLE001 — 相関集計の障害で呼び出し側を止めない
        return []

    home = [n for n in nations if n["role"] == "home"]
    adversary = [n for n in nations if n["role"] == "adversary"]
    # その他は サイバー×地政学が両立する国のみ (ハイブリッド結節点の候補) を補完
    others = [
        n
        for n in nations
        if n["role"] not in ("home", "adversary")
        and n["geopol"] > 0
        and (n["cyber"] > 0 or n["cyber_target"] > 0)
    ]
    picked = (home + adversary + others)[:top_n]

    out: list[dict[str, Any]] = []
    for n in picked:
        try:
            s = situation_by_nation(n["iso"], window_days=window_days, db_path=db_path)
        except Exception:  # noqa: BLE001 — 1 国の障害で全体を止めない
            continue
        attributed = int(s["cyber"]["total"])
        mention = int(s["cyber_mention"]["total"])
        target = int(s["cyber_target"]["total"])
        geo = int(s["geopolitical"]["total"])
        if attributed + mention + target + geo == 0:
            continue
        out.append(
            {
                "iso": n["iso"],
                "label": n["label"],
                "role": n["role"],
                "attributed_cyber": attributed,
                "cyber_mention": mention,
                "cyber_target": target,
                "geopol": geo,
                "top_actors": [a["label"] for a in (s["cyber"].get("actors") or [])[:3]],
                "cyber_intents": [i["intent"] for i in s["cyber"]["intents"][:3]],
                "geopol_intents": [i["intent"] for i in s["geopolitical"]["intents"][:3]],
            }
        )
    return out


def build_forecast_indicators(
    *, db_path: Path, limit: int = 10, period_type: str = "weekly"
) -> list[dict[str, Any]]:
    """B(1): 未検証 (open) の FC2 構造予測 (z-score スパイク) を注入用に返す。

    forecast pipeline が出す定量予測 (scope=actor/intent 等、direction、z_score、件数、rationale)
    の最新・高 z を渡し、spillover の叙述を構造予測に裏打ちさせる。障害時は [] (legacy)。

    ⚠ period_type で必ず絞る (2026-08-22 独立レビュー P0): 日次バースト
    (period_type='daily'、当日件数・z 2.5+) が週次 FC2 (週間件数) と同じテーブルに
    載るため、無フィルタだと z 順で週次予測を押し出し、LLM が「当日 5 件」と
    「週 59 件」を同一軸で読む時間軸混在が起きる。消費者 (Spotlight=週次) の
    分析期間と一致させる。
    """
    import sqlite3
    from datetime import UTC, datetime, timedelta

    from src.storage.db_backend import connect as backend_connect

    since = datetime.now(UTC) - timedelta(days=21)  # 直近 3 週の open 予測のみ
    con = backend_connect(db_path)
    if hasattr(con, "row_factory"):
        con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            """
            SELECT scope, target_value, direction, z_score, latest_count, rationale
              FROM forecast_indicators
             WHERE verified_at IS NULL AND period_type = ?
               AND datetime(period_start) >= datetime(?)
             ORDER BY z_score DESC
             LIMIT ?
            """,
            (period_type, since.isoformat(), limit),
        ).fetchall()
    except Exception:  # noqa: BLE001 — forecast 不在/障害で呼び出し側を止めない
        return []
    finally:
        con.close()
    return [
        {
            "scope": r["scope"],
            "target": r["target_value"],
            "direction": r["direction"],
            "z": round(float(r["z_score"]), 1),
            "count": int(r["latest_count"]),
        }
        for r in rows
    ]


def build_freshness(*, lookback_hours: int, db_path: Path) -> dict[str, Any]:
    """C: 当期報道の「振り返り率」(報道時刻 vs 発生時刻)。dated subset で算出。

    報道日 - 発生日 (lag) を Python で計算し、fresh(≤3d)/retrospective(>30d) を数える。
    現況の緊張激化と「古い campaign の再報道」を区別させる注釈。coverage 蓄積前は dated 少。
    """
    import sqlite3
    from datetime import UTC, date, datetime, timedelta

    from src.storage.db_backend import connect as backend_connect

    since = datetime.now(UTC) - timedelta(hours=lookback_hours)
    con = backend_connect(db_path)
    if hasattr(con, "row_factory"):
        con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT published_at, created_at, event_date FROM articles "  # noqa: S608
            "WHERE status='posted' AND event_date IS NOT NULL "
            "AND datetime(created_at) >= datetime(?)",
            (since.isoformat(),),
        ).fetchall()
    except Exception:  # noqa: BLE001
        return {"dated": 0, "fresh": 0, "retrospective": 0, "retrospective_pct": 0}
    finally:
        con.close()

    def _to_date(v: Any) -> date | None:
        if v is None:
            return None
        if hasattr(v, "date"):
            return v.date()  # type: ignore[no-any-return]
        s = str(v)[:10]
        try:
            return date.fromisoformat(s)
        except ValueError:
            return None

    fresh = retro = 0
    for r in rows:
        pub = _to_date(r["published_at"] or r["created_at"])
        ev = _to_date(r["event_date"])
        if pub is None or ev is None:
            continue
        lag = (pub - ev).days
        if lag <= 3:
            fresh += 1
        elif lag > 30:
            retro += 1
    dated = len(rows)
    return {
        "dated": dated,
        "fresh": fresh,
        "retrospective": retro,
        "retrospective_pct": (retro * 100 // dated) if dated else 0,
    }


@dataclass(frozen=True)
class AssessmentContext:
    """synthesis ファミリーが共有する「現在の見立て」の横断状態。

    出力中心 (各サーフェスが独立に再導出) から状態中心への第一歩。一度計算して
    synthesis / spotlight が射影として読む。すべて報告ストリーム基準 (実発生時刻でない)。
    """

    nation_correlation: list[dict[str, Any]]
    forecast_indicators: list[dict[str, Any]]
    freshness: dict[str, Any]


def build_assessment_context(
    *,
    nation_window_days: int,
    freshness_lookback_hours: int,
    db_path: Path,
) -> AssessmentContext:
    """横断状態を一度だけ計算する。各 builder は障害時に空を返し全体を止めない。

    Args:
        nation_window_days: 国家相関の集計窓 (分析対象期間と一致させる)。
        freshness_lookback_hours: 鮮度算出の lookback (分析対象期間と一致させる)。
        db_path: SQLite fallback 用 path (PG では無視され DATABASE_URL を使う)。
    """
    return AssessmentContext(
        nation_correlation=build_nation_correlation(
            window_days=nation_window_days, db_path=db_path
        ),
        forecast_indicators=build_forecast_indicators(db_path=db_path),
        freshness=build_freshness(lookback_hours=freshness_lookback_hours, db_path=db_path),
    )
